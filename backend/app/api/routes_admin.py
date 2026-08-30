"""User administration. Admin only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.dbmodels import Job, Project, User
from app.schemas import CreateUserIn, Password
from app.security import Principal, hash_password, require_admin, stamp_password_change

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/users")
def list_users(db: Session = Depends(get_db)) -> list[dict]:  # noqa: B008
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    counts = dict(
        db.execute(select(Project.owner_id, func.count()).group_by(Project.owner_id)).all()  # type: ignore[arg-type]
    )
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "active": u.active,
            "created_at": u.created_at,
            "projects": counts.get(u.id, 0),
        }
        for u in rows
    ]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserIn, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail=f"User {body.username!r} already exists")
    salt, digest = hash_password(body.password)
    user = User(username=body.username, pw_salt=salt, pw_hash=digest, role=body.role)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    new_password: Password,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    salt, digest = hash_password(new_password)
    user.pw_salt, user.pw_hash = salt, digest
    # Same rule as a self-service change: an admin reset must end the
    # sessions it was performed to end.
    stamp_password_change(user)
    db.commit()
    return {"updated": True}


@router.post("/users/{user_id}/active")
def set_active(
    user_id: str,
    active: bool,
    principal: Principal = Depends(require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    # Locking yourself out leaves nobody able to unlock anyone.
    if user.id == principal.id and not active:
        raise HTTPException(status_code=400, detail="You cannot disable your own account")
    user.active = active
    db.commit()
    return {"active": user.active}


@router.get("/jobs")
def recent_jobs(limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:  # noqa: B008
    from app.jobs import queue

    rows = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500))).all()
    return [queue.to_dict(j) for j in rows]


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Return a failed job to the queue by hand.

    Automatic recovery runs inside the worker's own loop, so when a worker
    dies the recovery dies with it. This is the escape hatch that does not
    depend on the thing that broke.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.state in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job.state}")
    job.state = "queued"
    job.attempts = 0
    job.error = None
    job.progress = 0.0
    job.stage = ""
    job.message = "Requeued by an administrator"
    job.claimed_by = None
    job.heartbeat_at = None
    job.finished_at = None
    db.commit()
    return {"requeued": True}
