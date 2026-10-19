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
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.db import get_db, session_scope
from app.dbmodels import Job, Project
from app.jobs import queue
from app.jobs.kinds import KINDS, lane_of
from app.security import Principal, current_user
from app.utils.paths import disk_report

router = APIRouter(prefix="/jobs", tags=["jobs"])

POLL_SEC = 1.0
# A stream is closed after this long even if the job never settles, so a
# forgotten browser tab cannot hold a connection open indefinitely. The
# client reconnects if it still cares.
MAX_STREAM_SEC = 3600.0
TERMINAL = ("done", "failed", "canceled")


def _owned_job(db: Session, job_id: str, principal: Principal) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ажил олдсонгүй.")
    if job.project_id and not principal.is_admin:
        # The owner and nothing else. Loading the row loaded its document —
        # 4 MB for a 2-hour transcript, 141 ms against 5.6 ms (audit
        # PERF-11) — on the route every open job card polls.
        owner = db.scalar(select(Project.owner_id).where(Project.id == job.project_id))
        # 404 rather than 403: whether someone else's job exists is not
        # information this caller is entitled to.
        if owner is None or owner != principal.id:
            raise HTTPException(status_code=404, detail="Ажил олдсонгүй.")
    return job


def queue_ahead(db: Session, job: Job) -> int | None:
    """How many waiting jobs a worker takes before this one, or None when it
    is not waiting.

    "Ажлын дараалалд хүлээж байна" with nothing else said is the panel a
    producer stares at while someone else's two-hour render holds the only
    render slot; the number is what turns "is it stuck?" into "two to go".

    Counted the way `queue.claim` orders the queue — priority, then age —
    and only within the job's own lane, because a job in another lane does
    not hold this one back. A retry still waiting out its backoff is not
    counted: no worker would take it first. Running jobs are not counted
    either; the panel says separately that a slot has to come free.
    """
    if job.state != "queued":
        return None
    lane = lane_of(job.kind)
    same_lane = [kind for kind in KINDS if lane_of(kind) == lane]
    return db.scalar(
        select(func.count())
        .select_from(Job)
        .where(
            Job.state == "queued",
            Job.id != job.id,
            Job.kind.in_(same_lane),
            or_(Job.available_at.is_(None), Job.available_at <= time.time()),
            or_(
                Job.priority < job.priority,
                and_(Job.priority == job.priority, Job.created_at < job.created_at),
            ),
        )
    ) or 0


def _live(db: Session, job: Job) -> dict:
    """A job as its own panel follows it: the row, and its place in line."""
    out = queue.to_dict(job)
    out["queue_ahead"] = queue_ahead(db, job)
    return out


def _snapshot(job_id: str) -> dict | None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        return _live(db, job) if job else None


@router.get("/queue")
def queue_status(
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Queue depth plus free scratch space — for the admin; an editor's own
    jobs for everyone else.

    Both answer questions that otherwise need a look at the server logs:
    "why has my job not started" is usually another project's long render,
    and "why did the export fail" is often a full disk.

    Those are the operator's questions. Every editor used to read the whole
    system's job counts, its live workers and both machines' disks — how
    busy the service is and how full its disks are, which no editor can act
    on (audit INFO-1). An editor's question is about their own work: is it
    waiting, and is anything alive to do it. `scope` says which answer this
    is; the machine-level keys are absent from an editor's, not null, so
    nothing reads a missing disk as an empty one.

    The disk is named by MACHINE. This used to be one `disk`, measured here —
    on the API, which renders nothing and shares no filesystem with the
    worker that does. The worker's own figure arrives through its report
    (app.worker_report) with the time it was measured, and is null until a
    worker running this code has said it; the API's stays, under its own
    name, because a full API disk is a real failure too.
    """
    from app import worker_report

    if not principal.is_admin:
        own = queue.queue_overview(db=db, owner_id=principal.id)
        return {
            "scope": "own",
            "counts": own["counts"],
            "waiting": own["waiting"],
            "stalled": own["stalled"],
            # Lanes the editor's own waiting work is in with nobody to run
            # them: which kind of work is stuck, not which machine.
            "unserved_lanes": own["unserved_lanes"],
        }

    # The request's own session: `current_user` already holds its connection.
    overview = queue.queue_overview(db=db)
    overview["scope"] = "all"
    overview["api_disk"] = disk_report()
    report = worker_report.read(db)
    overview["worker_disk"] = (
        {**report.disk, "measured_at": report.at} if report and report.disk else None
    )
    return overview


@router.get("/latest")
def latest_jobs(
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """The newest job of each of the caller's projects.

    The project list said where a project had got to and nothing about what
    was happening to it: a transcription running, or one that had failed an
    hour ago, looked exactly like a project nobody had touched. One query —
    DISTINCT ON over the (project, created) index — rather than a request per
    card. Only what a card shows; a job's result and error stay on the
    project page.
    """
    rows = db.scalars(
        select(Job)
        .join(Project, Project.id == Job.project_id)
        .where(Project.owner_id == principal.id)
        .order_by(Job.project_id, Job.created_at.desc())
        .distinct(Job.project_id)
    ).all()
    return {
        "jobs": [
            {
                "project_id": job.project_id,
                "job_id": job.id,
                "kind": job.kind,
                "state": job.state,
                "progress": round(job.progress, 4),
                "finished_at": job.finished_at,
            }
            for job in rows
        ]
    }


@router.get("/{job_id}")
def get_job(
    job_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    return _live(db, _owned_job(db, job_id, principal))


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    job = _owned_job(db, job_id, principal)
    if not queue.request_cancel(job_id, db=db):
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
    # Give the connection back before the stream starts. FastAPI 0.118+ runs
    # a yield dependency's exit code after the response is SENT — here, when
    # the stream ends, up to MAX_STREAM_SEC later — so the request's session
    # (`current_user` shares it) would otherwise pin one pooled connection per
    # open job panel for an hour, and ten panels would empty the pool for
    # every other request. The stream reads through its own short sessions.
    db.close()

    async def generator():
        last: str | None = None
        elapsed = 0.0
        while elapsed < MAX_STREAM_SEC:
            snapshot = await asyncio.to_thread(_snapshot, job_id)
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
