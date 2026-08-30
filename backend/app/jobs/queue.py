"""Durable, Postgres-backed job queue.

Replaces the desktop build's in-memory registry. That version was correct
for one process that owned the whole machine and wrong for anything else:
a redeploy loses every in-flight job, and a second instance cannot see the
first one's work at all.

Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`, which is the standard way
to hand each row to exactly one worker without a lock table or a broker.

`JobHandle` keeps the exact interface the old one had (`set_progress`,
`raise_if_cancelled`, `set_cancel_hook`) so the export pipeline — the largest
piece of code that consumes it — needs no changes at all.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from app.db import session_scope
from app.dbmodels import Job
from app.jobs.kinds import LANE_LIMITS, MAX_ATTEMPTS, lane_of, no_retry, priority_of
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Progress is flushed at most this often. An ffmpeg run reports several times
# a second; writing each tick would make the queue table the bottleneck.
PROGRESS_FLUSH_SEC = 1.0
HEARTBEAT_SEC = 5.0

ACTIVE_STATES = ("queued", "running")


class JobCancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Enqueue / read
# ---------------------------------------------------------------------------


def enqueue(
    kind: str,
    *,
    project_id: str | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
) -> str:
    """Queue one job and return its id.

    When `dedupe_key` matches a job that is still queued or running, that
    job's id is returned instead of a second row: two clicks on "Transcribe"
    must not run the same work twice against the same output keys.
    """
    with session_scope() as db:
        if dedupe_key:
            existing = db.scalar(
                select(Job).where(Job.dedupe_key == dedupe_key, Job.state.in_(ACTIVE_STATES))
            )
            if existing is not None:
                return existing.id

        job = Job(
            id=uuid.uuid4().hex,
            project_id=project_id,
            kind=kind,
            state="queued",
            priority=priority_of(kind),
            lane=lane_of(kind),
            no_retry=no_retry(kind),
            # The dedupe column is UNIQUE, so it can only ever hold the key
            # of a live job. Clearing it on completion (see `finish`) is what
            # lets the same action be run again later.
            dedupe_key=dedupe_key,
            result=None,
            message="",
        )
        job.result = {"payload": payload or {}}
        db.add(job)
    return job.id


def get(job_id: str) -> dict | None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        return to_dict(job) if job else None


def payload_of(job: Job) -> dict:
    return (job.result or {}).get("payload", {}) if isinstance(job.result, dict) else {}


def to_dict(job: Job) -> dict:
    result = job.result if isinstance(job.result, dict) else {}
    return {
        "job_id": job.id,
        "kind": job.kind,
        "project_id": job.project_id,
        "state": job.state,
        "progress": round(job.progress, 4),
        "stage": job.stage,
        "message": job.message,
        # `payload` is the queue's own bookkeeping, not something a client
        # asked for — only the handler's return value is surfaced.
        "result": result.get("output"),
        "error": job.error,
        "attempts": job.attempts,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }


def list_for_project(project_id: str, limit: int = 50) -> list[dict]:
    with session_scope() as db:
        rows = db.scalars(
            select(Job)
            .where(Job.project_id == project_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        ).all()
        return [to_dict(j) for j in rows]


def queue_overview() -> dict:
    """What the queue page shows. A queue with waiting work and no genuinely
    live worker means the worker service is down — the single most common
    cause of "my job never starts"."""
    with session_scope() as db:
        counts = dict(
            db.execute(select(Job.state, func.count()).group_by(Job.state)).all()  # type: ignore[arg-type]
        )
        stale_cutoff = time.time() - _stale_sec()
        live = db.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.state == "running", Job.heartbeat_at.is_not(None), Job.heartbeat_at > stale_cutoff)
        )
        waiting = counts.get("queued", 0)
        return {
            "counts": counts,
            "waiting": waiting,
            "live_workers": live or 0,
            # Waiting work with nothing actually alive to do it.
            "stalled": bool(waiting and not live),
        }


def _stale_sec() -> int:
    from app.config import get_settings

    return get_settings().job_stale_sec


# ---------------------------------------------------------------------------
# Claim / heartbeat / finish  (worker side)
# ---------------------------------------------------------------------------


def reap_stale() -> int:
    """Return jobs whose worker stopped heartbeating to the queue.

    A row sitting in `running` is NOT evidence of life: a worker killed by
    OOM or a redeploy leaves a corpse there forever, and that corpse holds
    its lane slot shut so nothing else in that lane can ever start. Only a
    moving heartbeat counts as alive.
    """
    cutoff = time.time() - _stale_sec()
    with session_scope() as db:
        stale = db.scalars(
            select(Job).where(
                Job.state == "running",
                or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
            )
        ).all()
        for job in stale:
            job.attempts += 1
            if job.no_retry or job.attempts >= MAX_ATTEMPTS:
                job.state = "failed"
                job.error = (
                    "Worker stopped responding. This job type is not retried automatically "
                    "because each attempt is billed separately."
                    if job.no_retry
                    else f"Worker stopped responding after {job.attempts} attempt(s)."
                )
                job.finished_at = time.time()
                job.dedupe_key = None
            else:
                job.state = "queued"
                job.claimed_by = None
                job.heartbeat_at = None
                job.progress = 0.0
                job.stage = ""
                job.message = "Requeued after the previous worker stopped responding"
            job.updated_at = time.time()
        return len(stale)


def claim(worker_id: str) -> Job | None:
    """Take the next runnable job, or None.

    Lane capacity is checked against rows that are actually alive (running
    AND heartbeating), never against the raw `running` count — otherwise a
    dead worker's corpse permanently blocks its lane.
    """
    cutoff = time.time() - _stale_sec()
    with session_scope() as db:
        live_by_lane = dict(
            db.execute(
                select(Job.lane, func.count())
                .where(Job.state == "running", Job.heartbeat_at.is_not(None), Job.heartbeat_at > cutoff)
                .group_by(Job.lane)
            ).all()  # type: ignore[arg-type]
        )
        open_lanes = [lane for lane, limit in LANE_LIMITS.items() if live_by_lane.get(lane, 0) < limit]
        if not open_lanes:
            return None

        job = db.scalars(
            select(Job)
            .where(Job.state == "queued", Job.lane.in_(open_lanes))
            .order_by(Job.priority.asc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if job is None:
            return None

        job.state = "running"
        job.claimed_by = worker_id
        job.heartbeat_at = time.time()
        job.updated_at = time.time()
        job.stage = "starting"
        job.attempts += 1
        db.flush()
        db.expunge(job)
        return job


def heartbeat(job_id: str, *, progress: float | None, stage: str | None, message: str | None) -> bool:
    """Refresh liveness and flush progress. Returns False when the job has
    been asked to cancel, which the handle turns into a JobCancelled."""
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return False
        job.heartbeat_at = time.time()
        job.updated_at = job.heartbeat_at
        if progress is not None:
            job.progress = progress
        if stage is not None:
            job.stage = stage
        if message is not None:
            job.message = message
        return not job.cancel_requested


def finish(job_id: str, *, state: str, output: Any = None, error: str | None = None) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.state = state
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        job.error = error
        if state == "done":
            job.progress = 1.0
            job.stage = "done"
        payload = payload_of(job)
        job.result = {"payload": payload, "output": output}
        # Freeing the dedupe key is what lets the same action run again.
        job.dedupe_key = None


def request_cancel(job_id: str) -> bool:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None or job.state not in ACTIVE_STATES:
            return False
        job.cancel_requested = True
        job.updated_at = time.time()
        # A job that never started can be settled immediately; a running one
        # has to be told, then noticed by its own handle.
        if job.state == "queued":
            job.state = "canceled"
            job.finished_at = time.time()
            job.message = "Canceled before it started"
            job.dedupe_key = None
        return True


def purge_old(keep_days: int) -> int:
    cutoff = time.time() - keep_days * 86400
    with session_scope() as db:
        result = db.execute(
            Job.__table__.delete().where(
                and_(Job.state.notin_(ACTIVE_STATES), Job.finished_at.is_not(None), Job.finished_at < cutoff)
            )
        )
        return result.rowcount or 0


def drop_project_jobs(project_id: str) -> int:
    """Cancel a deleted project's queued work so a worker does not pick up a
    job whose project no longer exists."""
    with session_scope() as db:
        result = db.execute(
            update(Job)
            .where(Job.project_id == project_id, Job.state == "queued")
            .values(state="canceled", finished_at=time.time(), dedupe_key=None, message="Project deleted")
        )
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# Handle passed into handlers
# ---------------------------------------------------------------------------


class JobHandle:
    """Same surface as the desktop build's handle, so handler code that only
    reports progress and checks cancellation ports over unchanged.

    Progress is buffered and flushed by the worker's heartbeat rather than
    written per call: an ffmpeg run reports several times a second and each
    write would be a round trip to Postgres.
    """

    def __init__(self, job_id: str, kind: str, project_id: str | None, payload: dict) -> None:
        self.job_id = job_id
        self.kind = kind
        self.project_id = project_id
        self.payload = payload
        self.cancel_requested = False

        self._pending: dict[str, Any] = {}
        self._last_flush = 0.0
        self._cancel_hook: Callable[[], Awaitable[None]] | None = None

    def set_cancel_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Registered by long subprocess runs (FfmpegRun.cancel) so a cancel
        kills the child process instead of waiting for it to finish."""
        self._cancel_hook = hook

    async def set_progress(
        self, progress: float, stage: str | None = None, message: str | None = None
    ) -> None:
        self._pending["progress"] = max(0.0, min(1.0, progress))
        if stage is not None:
            self._pending["stage"] = stage[:48]
        if message is not None:
            self._pending["message"] = message
        if time.time() - self._last_flush >= PROGRESS_FLUSH_SEC:
            await self.flush()

    async def flush(self) -> None:
        pending, self._pending = self._pending, {}
        self._last_flush = time.time()
        alive = await asyncio.to_thread(
            heartbeat,
            self.job_id,
            progress=pending.get("progress"),
            stage=pending.get("stage"),
            message=pending.get("message"),
        )
        if not alive and not self.cancel_requested:
            self.cancel_requested = True
            if self._cancel_hook:
                await self._cancel_hook()

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelled()
