"""Job status, cancellation, and a live stream.

The desktop build's SSE endpoint subscribed to an in-process asyncio queue.
That cannot work now: the job runs in a different process — often on a
different machine — from the API answering this request. The stream polls
the row instead and emits only on change, which costs one cheap indexed read
per second per open stream.
"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.db import get_db
from app.dbmodels import Job, Project
from app.jobs import queue
from app.security import Principal, current_user
from app.utils.paths import disk_report

router = APIRouter(prefix="/jobs", tags=["jobs"])

POLL_SEC = 1.0
# A stream is closed after this long even if the job never settles, so a
# forgotten browser tab cannot hold a connection (and a database session)
# open indefinitely. The client reconnects if it still cares.
MAX_STREAM_SEC = 3600.0
TERMINAL = ("done", "failed", "canceled")


def _owned_job(db: Session, job_id: str, principal: Principal) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.project_id and not principal.is_admin:
        project = db.get(Project, job.project_id)
        # 404 rather than 403: whether someone else's job exists is not
        # information this caller is entitled to.
        if project is None or project.owner_id != principal.id:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


@router.get("/queue")
def queue_status(principal: Principal = Depends(current_user)) -> dict:  # noqa: B008
    """Queue depth plus free scratch space.

    Both answer questions that otherwise need a look at the server logs:
    "why has my job not started" is usually another project's long render,
    and "why did the export fail" is often a full disk.
    """
    overview = queue.queue_overview()
    overview["disk"] = disk_report()
    return overview


@router.get("/{job_id}")
def get_job(
    job_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    return queue.to_dict(_owned_job(db, job_id, principal))


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    job = _owned_job(db, job_id, principal)
    if not queue.request_cancel(job_id):
        return {"canceled": False, "reason": f"job already {job.state}"}
    # A running job is only *asked* to stop here — it notices at its next
    # heartbeat, which is also what kills the ffmpeg child process.
    return {"canceled": True, "immediate": job.state == "queued"}


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> EventSourceResponse:
    _owned_job(db, job_id, principal)

    async def generator():
        last: str | None = None
        elapsed = 0.0
        while elapsed < MAX_STREAM_SEC:
            snapshot = await asyncio.to_thread(queue.get, job_id)
            if snapshot is None:
                break
            payload = json.dumps(snapshot, ensure_ascii=False)
            if payload != last:
                last = payload
                yield {"event": "job", "data": payload}
            if snapshot["state"] in TERMINAL:
                break
            await asyncio.sleep(POLL_SEC)
            elapsed += POLL_SEC

    return EventSourceResponse(generator())
