"""Project persistence: JSON files on disk, no database.

Save is atomic (write project.json.tmp -> os.replace) so a crash mid-write
never leaves a corrupt project.json behind.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from app.models import Project, migrate_project_dict
from app.utils.logging import get_logger
from app.utils.paths import atomic_write_text, project_dir, projects_dir

logger = get_logger(__name__)


class ProjectNotFound(Exception):
    pass


def project_json_path(project_id: str) -> Path:
    return project_dir(project_id) / "project.json"


def save_project(project: Project) -> None:
    project.updated_at = time.time()
    path = project_json_path(project.id)
    atomic_write_text(path, project.model_dump_json(indent=2))


def load_project(project_id: str) -> Project:
    path = project_json_path(project_id)
    if not path.is_file():
        raise ProjectNotFound(project_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data = migrate_project_dict(data)
    return Project.model_validate(data)


def list_projects() -> list[Project]:
    result = []
    for entry in projects_dir().iterdir():
        if not entry.is_dir():
            continue
        candidate = entry / "project.json"
        if not candidate.is_file():
            continue
        try:
            result.append(load_project(entry.name))
        except Exception:  # noqa: BLE001 - a corrupt project shouldn't break the whole list
            logger.exception("Failed to load project %s while listing", entry.name)
    result.sort(key=lambda p: p.updated_at, reverse=True)
    return result


def delete_project(project_id: str) -> None:
    d = project_dir(project_id)
    if not (d / "project.json").is_file():
        raise ProjectNotFound(project_id)
    shutil.rmtree(d)
