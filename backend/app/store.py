"""Project persistence against Postgres.

The desktop build stored one `project.json` per project on disk. That is
gone: Railway's filesystem is ephemeral, so a redeploy would erase every
project.

The domain document (app.models.Project) survives intact, stored in the
`projects.doc` JSONB column. Keeping it as a document means
`migrate_project_dict` — a working, tested schema-version ladder — still
applies unchanged, and the AI and export pipelines keep receiving the exact
pydantic objects they already expect.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dbmodels import Project as ProjectRow
from app.models import SCHEMA_VERSION, Project, migrate_project_dict

# Columns, not document fields: everything else lives in `doc`.
_COLUMN_FIELDS = ("id", "name", "created_at", "updated_at", "schema_version")


class ProjectNotFound(Exception):
    pass


def to_domain(row: ProjectRow) -> Project:
    data = dict(row.doc or {})
    data.update(
        id=row.id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        schema_version=row.schema_version,
    )
    return Project.model_validate(migrate_project_dict(data))


def _doc_of(project: Project) -> dict:
    return {k: v for k, v in project.model_dump(mode="json").items() if k not in _COLUMN_FIELDS}


def get_row(db: Session, project_id: str, owner_id: str | None = None) -> ProjectRow:
    row = db.get(ProjectRow, project_id)
    if row is None:
        raise ProjectNotFound(project_id)
    # Ownership is enforced here rather than in each route, so a new route
    # cannot forget it and expose another account's project.
    if owner_id is not None and row.owner_id != owner_id:
        raise ProjectNotFound(project_id)
    return row


def load(db: Session, project_id: str, owner_id: str | None = None) -> Project:
    return to_domain(get_row(db, project_id, owner_id))


def save(db: Session, project: Project, owner_id: str | None = None) -> ProjectRow:
    row = db.get(ProjectRow, project.id)
    if row is None:
        if owner_id is None:
            raise ValueError("owner_id is required when creating a project")
        row = ProjectRow(id=project.id, owner_id=owner_id, created_at=project.created_at)
        db.add(row)
    project.updated_at = time.time()
    row.name = project.name
    row.schema_version = SCHEMA_VERSION
    row.doc = _doc_of(project)
    row.updated_at = project.updated_at
    return row


def list_for_owner(db: Session, owner_id: str, limit: int = 200) -> list[ProjectRow]:
    return list(
        db.scalars(
            select(ProjectRow)
            .where(ProjectRow.owner_id == owner_id)
            .order_by(ProjectRow.updated_at.desc())
            .limit(limit)
        ).all()
    )


def summary(row: ProjectRow) -> dict:
    """The list-view shape. Deliberately does NOT deserialize `doc`: a
    projects list would otherwise pay for every transcript and suggestion
    block it never shows."""
    doc = row.doc or {}
    video = doc.get("video") or {}
    transcript = doc.get("transcript") or {}
    suggestions = doc.get("suggestions") or {}
    return {
        "id": row.id,
        "name": row.name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "has_video": bool(row.video_key),
        "has_transcript": bool(transcript.get("segments")),
        "has_suggestions": bool(suggestions.get("shorts")),
        "duration_sec": video.get("duration_sec", 0.0),
        "n_outputs": len(row.outputs),
    }
