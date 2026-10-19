"""What every /projects module needs: its router, the 404, the R2 check, the project."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Project
from app.security import Principal
from app.store import ProjectNotFound, load


def projects_router() -> APIRouter:
    """One module's share of /projects. app.api.routes_projects includes them
    all; the prefix and the tag are here once, not in every module."""
    return APIRouter(prefix="/projects", tags=["projects"])


@contextmanager
def project_404() -> Iterator[None]:
    """Turn the store's ProjectNotFound into the API's 404.

    Every route that reads a project goes through this, so "no such project"
    and "another account's project" stay one answer: the store checks
    ownership (app.store.get_row), and a reply that told the two apart would
    confirm that someone else's project id exists.
    """
    try:
        yield
    except ProjectNotFound as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Төсөл олдсонгүй.") from e


def require_r2() -> None:
    """Refuse the request while naming the variable that is wrong.

    The reason is safe to hand back: it names environment variables, never
    their values.
    """
    problem = get_settings().r2_config_error
    if problem:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Файлын сан (R2) энэ сервер дээр тохируулагдаагүй байна: {problem}",
        )


def require_project(db: Session, project_id: str, principal: Principal) -> Project:
    with project_404():
        return load(db, project_id, principal.id)
