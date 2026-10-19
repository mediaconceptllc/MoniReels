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
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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


@contextmanager
def _use(db: Session | None) -> Iterator[Session]:
    """The caller's session when it has one, else a short one of our own.

    An API request holds one pooled connection for its whole life (its
    `get_db` session, opened by `current_user`). Every function here used to
    open a SECOND one, so ten concurrent requests each held one connection
    and waited for another: the pool (5 + 5) deadlocked for its 30 s timeout
    and the API answered 500. Routes now pass their own session in.

    With a caller's session, functions that write commit it themselves — a
    job that was enqueued but never committed is worse than no job at all.
    """
    if db is not None:
        yield db
        return
    with session_scope() as own:
        yield own


def _active_with_key(db: Session, dedupe_key: str) -> str | None:
    return db.scalar(
        select(Job.id).where(Job.dedupe_key == dedupe_key, Job.state.in_(ACTIVE_STATES))
    )


def active_with_key(db: Session, dedupe_key: str) -> str | None:
    """The live job holding `dedupe_key`, if any — what `enqueue` would hand
    back instead of queueing (app.quotas admits such a click for free)."""
    return _active_with_key(db, dedupe_key)


# ---------------------------------------------------------------------------
# Enqueue / read
# ---------------------------------------------------------------------------


def enqueue(
    kind: str,
    *,
    project_id: str | None = None,
    payload: dict | None = None,
    dedupe_key: str | None = None,
    db: Session | None = None,
) -> str:
    """Queue one job and return its id.

    When `dedupe_key` matches a job that is still queued or running, that
    job's id is returned instead of a second row: two clicks on "Transcribe"
    must not run the same work twice against the same output keys.

    `db` is the caller's session (see `_use`); it is committed here.
    """
    with _use(db) as session:
        if dedupe_key:
            existing = _active_with_key(session, dedupe_key)
            if existing is not None:
                return existing

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
        try:
            # A savepoint, so losing the race below leaves the caller's
            # transaction usable.
            with session.begin_nested():
                session.add(job)
        except IntegrityError:
            # Two clicks both passed the check above and the other INSERT won
            # the UNIQUE dedupe key. That job is the answer — this used to
            # surface as a 500 to whichever click lost.
            existing = _active_with_key(session, dedupe_key) if dedupe_key else None
            if existing is None:
                raise
            return existing
        if db is not None:
            db.commit()
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


def list_for_project(project_id: str, limit: int = 50, db: Session | None = None) -> list[dict]:
    with _use(db) as session:
        rows = session.scalars(
            select(Job)
            .where(Job.project_id == project_id)
            .order_by(Job.created_at.desc())
            .limit(limit)
        ).all()
        return [to_dict(j) for j in rows]


def queue_overview(db: Session | None = None) -> dict:
    """What the queue page shows. A queue with waiting work and no genuinely
    live worker means the worker service is down — the single most common
    cause of "my job never starts"."""
    with _use(db) as session:
        counts = dict(
            session.execute(select(Job.state, func.count()).group_by(Job.state)).all()  # type: ignore[arg-type]
        )
        stale_cutoff = time.time() - _stale_sec()
        live = session.scalar(
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


@dataclass(frozen=True)
class Reaped:
    """What a reaping pass did — as TWO numbers, because it does two things.

    A requeued job runs again on its own. A failed one never will, and the
    person reading the log is the only one who can start it. Reporting the
    total under one verb told them the opposite of what happened: the log
    said "Requeued 1 job" for a `suggest` corpse that had in fact been
    failed, so the run they were waiting for was never coming.
    """

    requeued: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        return self.requeued + self.failed

    def __bool__(self) -> bool:
        return bool(self.total)


def reap_stale() -> Reaped:
    """Return jobs whose worker stopped heartbeating to the queue.

    A row sitting in `running` is NOT evidence of life: a worker killed by
    OOM or a redeploy leaves a corpse there forever, and that corpse holds
    its lane slot shut so nothing else in that lane can ever start. Only a
    moving heartbeat counts as alive.

    Retryable work goes back on the queue. Work that bills per attempt is
    failed instead, saying so — a redeploy must not quietly buy a second
    transcription.
    """
    cutoff = time.time() - _stale_sec()
    with session_scope() as db:
        stale = db.scalars(
            select(Job)
            .where(
                Job.state == "running",
                or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff),
            )
            # A row a heartbeat is writing right now is alive by definition;
            # it is skipped this pass rather than requeued under its worker.
            .with_for_update(skip_locked=True)
        ).all()
        requeued = failed = 0
        for job in stale:
            job.attempts += 1
            if job.no_retry or job.attempts >= MAX_ATTEMPTS:
                failed += 1
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
                requeued += 1
                job.state = "queued"
                job.claimed_by = None
                job.heartbeat_at = None
                job.progress = 0.0
                job.stage = ""
                job.message = "Requeued after the previous worker stopped responding"
            job.updated_at = time.time()
        return Reaped(requeued=requeued, failed=failed)


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


def _ours(job: Job | None, attempt: int | None) -> bool:
    """Whether a write from the attempt that claimed `job` may land.

    `attempts` moves on every claim, so it is a fencing token: once the
    reaper has handed a job back to the queue (its heartbeat stopped — one
    database error used to end the heartbeat for good) and another worker has
    claimed it, the first worker's heartbeats and its final verdict must not
    overwrite the row the second one now owns. None = unfenced (a caller that
    holds no claim, such as a test handle).
    """
    if job is None:
        return False
    if attempt is None:
        return True
    return job.state == "running" and job.attempts == attempt


def heartbeat(
    job_id: str,
    *,
    progress: float | None,
    stage: str | None,
    message: str | None,
    attempt: int | None = None,
) -> bool:
    """Refresh liveness and flush progress. Returns False when the job has
    been asked to cancel — or is no longer this attempt's — which the handle
    turns into a JobCancelled."""
    with session_scope() as db:
        job = db.get(Job, job_id)
        if not _ours(job, attempt):
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


def finish(
    job_id: str,
    *,
    state: str,
    output: Any = None,
    error: str | None = None,
    attempt: int | None = None,
) -> bool:
    """Settle a job — or, with state "queued", hand it back for a retry.

    Returns False when nothing was written because the row is no longer this
    attempt's (see `_ours`): its verdict belongs to whoever holds it now.
    """
    with session_scope() as db:
        job = db.get(Job, job_id)
        if not _ours(job, attempt):
            if job is not None:
                logger.warning(
                    "Job %s: attempt %s settled as %s after the job was taken over; not recorded",
                    job_id, attempt, state,
                )
            return False
        now = time.time()
        job.state = state
        job.updated_at = now
        job.error = error
        payload = payload_of(job)
        job.result = {"payload": payload, "output": output}
        if state == "queued":
            # A retry is the same job, still active: it keeps its dedupe key
            # (dropping it let a second click create a duplicate) and has not
            # finished. Released, so any worker may claim it.
            job.claimed_by = None
            job.heartbeat_at = None
            job.progress = 0.0
            job.stage = ""
            return True
        job.finished_at = now
        if state == "done":
            job.progress = 1.0
            job.stage = "done"
        # Freeing the dedupe key is what lets the same action run again.
        job.dedupe_key = None
        return True


def abandon(job_id: str, *, requeue: bool, reason: str, attempt: int | None = None) -> bool:
    """Settle a job the worker is walking away from, and say why.

    A worker stopped by a deploy used to leave its row `running` with a
    heartbeat that had stopped moving. Nothing said so for JOB_STALE_SEC, and
    what the producer eventually saw was the reaper's generic verdict — for a
    kind billed per attempt, five minutes after the money was already spent.

    Only an ACTIVE row is touched. The abandoned task is still running as this
    is called and may settle the job itself a moment later; if it did, its
    answer is the true one and this must not overwrite it.
    """
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None or job.state not in ACTIVE_STATES or not _ours(job, attempt):
            return False
        job.state = "queued" if requeue else "failed"
        job.error = reason
        if requeue:
            job.claimed_by = None
            job.heartbeat_at = None
        job.updated_at = time.time()
        if not requeue:
            job.finished_at = job.updated_at
            # Same rule as `finish`: a settled job releases its dedupe key,
            # without which the same action could never be started again.
            job.dedupe_key = None
        return True


def request_cancel(job_id: str, db: Session | None = None) -> bool:
    with _use(db) as session:
        job = session.get(Job, job_id)
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
        if db is not None:
            db.commit()
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


def drop_project_jobs(project_id: str, db: Session | None = None) -> int:
    """Cancel a deleted project's queued work so a worker does not pick up a
    job whose project no longer exists."""
    with _use(db) as session:
        result = session.execute(
            update(Job)
            .where(Job.project_id == project_id, Job.state == "queued")
            .values(state="canceled", finished_at=time.time(), dedupe_key=None, message="Project deleted")
        )
        if db is not None:
            db.commit()
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

    def __init__(
        self,
        job_id: str,
        kind: str,
        project_id: str | None,
        payload: dict,
        attempt: int | None = None,
    ) -> None:
        self.job_id = job_id
        self.kind = kind
        self.project_id = project_id
        self.payload = payload
        # The claim's fencing token (see `_ours`).
        self.attempt = attempt
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
            try:
                await self.flush()
            except Exception:  # noqa: BLE001 - see below
                # Progress is a report, not the work. A database blip here
                # used to fail the handler — and with it a paid run that was
                # going fine. The heartbeat loop keeps retrying the write.
                logger.warning("Job %s: progress not saved; will retry", self.job_id, exc_info=True)

    async def flush(self) -> None:
        pending, self._pending = self._pending, {}
        self._last_flush = time.time()
        try:
            alive = await asyncio.to_thread(
                heartbeat,
                self.job_id,
                progress=pending.get("progress"),
                stage=pending.get("stage"),
                message=pending.get("message"),
                attempt=self.attempt,
            )
        except Exception:
            # Not lost: the next flush carries it, unless newer values came in.
            self._pending = {**pending, **self._pending}
            raise
        if not alive and not self.cancel_requested:
            self.cancel_requested = True
            if self._cancel_hook:
                await self._cancel_hook()

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise JobCancelled()
