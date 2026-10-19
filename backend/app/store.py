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

from sqlalchemy import Float, bindparam, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session, defer

from app.dbmodels import Output
from app.dbmodels import Project as ProjectRow
from app.models import SCHEMA_VERSION, Project, migrate_project_dict

# Columns, not document fields: everything else lives in `doc`.
_COLUMN_FIELDS = ("id", "name", "created_at", "updated_at", "schema_version")

#: The document sections a settings change writes (PATCH /projects/{id},
#: beside the `name` column). load_settings/save_settings read and write
#: only these, never the transcript.
SETTINGS_FIELDS = ("language", "export", "subtitle_style", "transition")
#: The sections a settings change never reads: most of the document's size.
_HEAVY_FIELDS = ("transcript", "suggestions", "clips")


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


def get_row(
    db: Session,
    project_id: str,
    owner_id: str | None = None,
    *,
    for_update: bool = False,
    doc: bool = True,
) -> ProjectRow:
    """The row, if this owner may see it.

    `doc=False` leaves the document in Postgres (loaded only if something
    then reads `row.doc`), for routes that need the row and not what is in
    it — the outputs list, a delete. The document is most of a project: a
    2-hour transcript is 4 MB, and loading it to check who owns the row cost
    ~100 ms of decoding on every such request. Not offered with
    `for_update`: a locked read is a read-modify-write of the document, and
    it must read the document under the lock.

    `for_update` locks it until the transaction ends. Every read-modify-write
    of the document needs that: the document is saved WHOLE, so two writers
    that both read before either saved — a PATCH and a transcript edit from
    two tabs, or a worker writing its result while the producer saves — had
    the later save silently undo the earlier one (measured: up to 7 of 20
    concurrent transcript edits lost, and a job's paid suggestions dropped
    while the job still read "done"). Locked, the second writer reads what
    the first one wrote. Reads that only look stay unlocked.
    """
    if for_update:
        if not doc:
            raise ValueError("a locked read reads the document")
        row = db.get(ProjectRow, project_id, with_for_update=True, populate_existing=True)
    elif doc:
        row = db.get(ProjectRow, project_id)
    else:
        row = db.get(ProjectRow, project_id, options=[defer(ProjectRow.doc)])
    if row is None:
        raise ProjectNotFound(project_id)
    # Ownership is enforced here rather than in each route, so a new route
    # cannot forget it and expose another account's project.
    if owner_id is not None and row.owner_id != owner_id:
        raise ProjectNotFound(project_id)
    return row


def load(
    db: Session, project_id: str, owner_id: str | None = None, *, for_update: bool = False
) -> Project:
    return to_domain(get_row(db, project_id, owner_id, for_update=for_update))


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


def load_settings(db: Session, project_id: str, owner_id: str | None = None) -> Project | None:
    """The project WITHOUT its transcript, suggestions and clips, locked for
    update — what a settings change reads. None when the stored document is
    from an older schema: the caller then takes the whole-document path
    (load + save), which migrates it.

    A settings save used to decode, validate, re-encode and rewrite the whole
    document to change one number: 1,048 ms for `crf` on a 2-hour project
    (audit PERF-10). The heavy sections are dropped in Postgres and never
    reach Python; `save_settings` writes back only what a settings change can
    alter, so they cannot be lost either.
    """
    light = ProjectRow.doc
    for field in _HEAVY_FIELDS:
        light = light.op("-", return_type=JSONB)(field)
    row = db.execute(
        select(
            ProjectRow.owner_id,
            ProjectRow.name,
            ProjectRow.created_at,
            ProjectRow.updated_at,
            ProjectRow.schema_version,
            light.label("settings"),
        )
        .where(ProjectRow.id == project_id)
        .with_for_update(of=ProjectRow)
    ).one_or_none()
    if row is None or (owner_id is not None and row.owner_id != owner_id):
        raise ProjectNotFound(project_id)
    if row.schema_version != SCHEMA_VERSION:
        return None
    data = dict(row.settings or {})
    data.update(
        id=project_id,
        name=row.name,
        created_at=row.created_at,
        updated_at=row.updated_at,
        schema_version=row.schema_version,
    )
    return Project.model_validate(data)


def save_settings(db: Session, project: Project) -> None:
    """Write a `load_settings` project's name and settings sections back,
    merged into the stored document — the rest of it is left as it is."""
    project.updated_at = time.time()
    sections = project.model_dump(mode="json", include=set(SETTINGS_FIELDS))
    db.execute(
        update(ProjectRow)
        .where(ProjectRow.id == project.id)
        .values(
            name=project.name,
            updated_at=project.updated_at,
            doc=ProjectRow.doc.op("||", return_type=JSONB)(bindparam("sections", sections, type_=JSONB)),
        )
        .execution_options(synchronize_session=False)
    )


def list_for_owner(db: Session, owner_id: str, limit: int = 200) -> list[Row]:
    """The projects list, in ONE query, with the documents left in Postgres.

    It used to load every row whole and count each one's outputs with its
    own query: for an account with 201 projects that was 202 queries and
    26 MB of JSON decoded to show a name and four flags — 993 ms (audit
    PERF-11). The flags are read in SQL from the paths they live at, the
    outputs counted by a subquery, and nothing else of the document leaves
    the database.
    """
    doc = ProjectRow.doc
    n_outputs = (
        select(func.count(Output.id))
        .where(Output.project_id == ProjectRow.id)
        .correlate(ProjectRow)
        .scalar_subquery()
    )
    return list(
        db.execute(
            select(
                ProjectRow.id,
                ProjectRow.name,
                ProjectRow.created_at,
                ProjectRow.updated_at,
                ProjectRow.video_key,
                ProjectRow.thumbnail_key,
                # "Is there a first segment / a first short" — what
                # bool(list) asked of the decoded document.
                doc[("transcript", "segments", 0)].is_not(None).label("has_transcript"),
                doc[("suggestions", "shorts", 0)].is_not(None).label("has_suggestions"),
                func.coalesce(doc[("video", "duration_sec")].astext.cast(Float), 0.0).label(
                    "duration_sec"
                ),
                n_outputs.label("n_outputs"),
            )
            .where(ProjectRow.owner_id == owner_id)
            .order_by(ProjectRow.updated_at.desc())
            .limit(limit)
        ).all()
    )


def summary(row: Row) -> dict:
    """The list-view shape of one `list_for_owner` row."""
    return {
        "id": row.id,
        "name": row.name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "has_video": bool(row.video_key),
        "has_transcript": bool(row.has_transcript),
        "has_suggestions": bool(row.has_suggestions),
        "duration_sec": float(row.duration_sec),
        "n_outputs": int(row.n_outputs),
    }
