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
from collections.abc import Awaitable, Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeGuard

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import worker_report
from app.db import session_scope
from app.dbmodels import Job, Project
from app.jobs.cancel import JobCancelled
from app.jobs.kinds import LANE_LIMITS, MAX_ATTEMPTS, kinds_in, lane_of, no_retry, priority_of
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Progress is flushed at most this often. An ffmpeg run reports several times
# a second; writing each tick would make the queue table the bottleneck.
PROGRESS_FLUSH_SEC = 1.0
HEARTBEAT_SEC = 5.0

ACTIVE_STATES = ("queued", "running")

# JobCancelled lives in app.jobs.cancel, so the provider clients can raise it
# without importing the queue; handlers and the export pipeline import it
# from here, as they always have.

# How long a job handed back for another attempt waits before it may be
# claimed: RETRY_BACKOFF_SEC after its first attempt, doubling, capped. With no
# wait at all a render that fails on a transient (a full disk being swept, an
# R2 blip) used to be claimed again in the same poll and fail the same way, all
# its attempts gone inside a few seconds.
RETRY_BACKOFF_SEC = 30.0
RETRY_BACKOFF_MAX_SEC = 600.0
# Attempts kept on a row (`Job.history`). A job has at most MAX_ATTEMPTS plus
# whatever an admin retries; the cap only stops a row from growing forever.
HISTORY_MAX = 20
HISTORY_ERROR_CHARS = 500


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


# A worker speaks every minute (app.worker); three missed rounds is a worker
# that is gone, not one that is busy. One number, kept where it is read.
WORKER_REPORT_STALE_SEC = worker_report.STALE_SEC


def queue_overview(db: Session | None = None, *, owner_id: str | None = None) -> dict:
    """What the queue page shows. A queue with waiting work and no genuinely
    live worker means the worker service is down — the single most common
    cause of "my job never starts".

    `owner_id` narrows the counts, and the stalled verdict, to the jobs of
    that account's own projects (routes_jobs.queue_status for an editor).
    Whether a worker is alive stays a system-wide fact: any live worker
    would take the job.

    "Live" used to mean "running a job with a fresh heartbeat", so an IDLE
    worker counted as dead: any waiting job it was not allowed to take yet
    (a lane already busy, a retry still backing off) read as "the worker is
    down". The worker's own report is the second sign of life — published
    on a timer whether or not it has work.

    Per lane, too. With a render pool and an I/O pool (#64) one of them can
    be down while the other is healthy, and "is any worker alive" says yes
    while every export waits for a worker that is not coming. `lanes` says,
    for each lane, how much runnable work waits in it and whether any live
    worker serves it; `served` is null when that cannot be known — no
    worker running this code is alive, or an older build that does not say
    its lanes still is — never a guess that raises an alarm. With
    `owner_id` the lanes count that account's runnable work only; who
    serves a lane is, like liveness, the same for everyone.
    """

    def mine(query: Select[Any]) -> Select[Any]:
        if owner_id is None:
            return query
        return query.join(Project, Project.id == Job.project_id).where(Project.owner_id == owner_id)

    with _use(db) as session:
        counts: dict[str, int] = {
            state: n
            for state, n in session.execute(mine(select(Job.state, func.count())).group_by(Job.state))
        }
        now = time.time()
        stale_cutoff = now - _stale_sec()
        live = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.state == "running", Job.heartbeat_at.is_not(None), Job.heartbeat_at > stale_cutoff)
        )
        waiting = counts.get("queued", 0)
        # A retry waiting out its backoff is not work a live worker would have
        # taken: counted as waiting, it reported a healthy idle worker as
        # down for up to RETRY_BACKOFF_MAX_SEC after every failure.
        by_kind: dict[str, int] = {
            kind: n
            for kind, n in session.execute(
                mine(select(Job.kind, func.count()))
                .where(
                    Job.state == "queued",
                    or_(Job.available_at.is_(None), Job.available_at <= now),
                )
                .group_by(Job.kind)
            )
        } if waiting else {}
        runnable = sum(by_kind.values())
        per_lane: dict[str, int] = {}
        for kind, n in by_kind.items():
            per_lane[lane_of(kind)] = per_lane.get(lane_of(kind), 0) + n

        beats = worker_report.heartbeats(session)
        alive = [b for b in beats if b.live(now)]
        legacy = worker_report.read_legacy(session)
        legacy_fresh = legacy is not None and now - legacy.at < WORKER_REPORT_STALE_SEC
        # A fresh single row that no heartbeat wrote: an older build is still
        # running, and its lanes are unknown.
        unseen = legacy_fresh and legacy is not None and legacy.worker is None
        reported = bool(alive) or legacy_fresh

        lanes = []
        for lane in LANE_LIMITS:
            serving = sum(1 for b in alive if b.serves(lane))
            served = True if serving else (None if not alive or unseen else False)
            lanes.append(
                {"lane": lane, "runnable": per_lane.get(lane, 0), "workers": serving, "served": served}
            )
        seen = [b.seen_at for b in beats] + ([legacy.at] if legacy is not None else [])
        return {
            "counts": counts,
            "waiting": waiting,
            "live_workers": live or 0,
            # When a worker last said it was alive, whatever it was doing.
            "worker_seen_at": max(seen) if seen else None,
            # Runnable work with nothing actually alive to do it: a retry
            # still backing off is not runnable, and an idle worker that
            # reported recently is alive.
            "stalled": bool(runnable and not live and not reported),
            "lanes": lanes,
            # Lanes with runnable work and, as far as can be known, nobody to
            # run it — the warning, already decided here so every page that
            # shows it says the same thing.
            "unserved_lanes": [row["lane"] for row in lanes if row["runnable"] and row["served"] is False],
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
            now = time.time()
            # Recorded as the attempt that died, before the count moves on.
            gives_up = job.no_retry or job.attempts + 1 >= MAX_ATTEMPTS
            _close_attempt(
                job, "failed" if gives_up else "requeued", "Worker stopped responding", now
            )
            job.attempts += 1
            if gives_up:
                failed += 1
                error = (
                    "Worker stopped responding. This job type is not retried automatically "
                    "because each attempt is billed separately."
                    if job.no_retry
                    else f"Worker stopped responding after {job.attempts} attempt(s)."
                )
                job.state = "failed"
                job.error = error
                job.finished_at = now
                job.dedupe_key = None
                job.updated_at = now
            else:
                requeued += 1
                # Backed off like any other failed attempt: a job that takes
                # its worker down with it (out of memory, say) must not be
                # claimed again in the very next poll and take the next one.
                _hand_back(
                    job,
                    error=job.error,
                    delay=backoff_sec(job.attempts),
                    now=now,
                    message="Requeued after the previous worker stopped responding",
                )
        return Reaped(requeued=requeued, failed=failed)


def claim(worker_id: str, lanes: Collection[str] | None = None) -> Job | None:
    """Take the next runnable job in one of `lanes` (default: every lane),
    or None.

    Which lanes have room is the CALLING worker's question, answered from
    the jobs it is running itself (app.jobs.kinds.open_lanes). This used to
    count live rows per lane across every worker, which made each cap
    system-wide (audit ARCH-2) — and racy besides: two workers could both
    count a free slot and both take it. Counting in-process has neither
    problem, and no dead worker's `running` corpse can hold a lane shut,
    because nobody counts it.

    Filtered by KIND, not by the stored `lane`: a row queued before a lane
    was renamed or split still carries the old name, and is claimed by what
    its kind is now.
    """
    kinds = kinds_in(LANE_LIMITS if lanes is None else lanes)
    if not kinds:
        return None
    with session_scope() as db:
        now = time.time()
        job = db.scalars(
            select(Job)
            .where(
                Job.state == "queued",
                Job.kind.in_(kinds),
                # A retry waiting out its backoff is not runnable yet.
                or_(Job.available_at.is_(None), Job.available_at <= now),
            )
            .order_by(Job.priority.asc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if job is None:
            return None

        job.lane = lane_of(job.kind)
        job.state = "running"
        job.claimed_by = worker_id
        job.heartbeat_at = now
        job.updated_at = now
        job.started_at = now
        job.available_at = None
        job.stage = "starting"
        job.attempts += 1
        db.flush()
        db.expunge(job)
        return job


def _ours(job: Job | None, attempt: int | None) -> TypeGuard[Job]:
    """Whether a write from the attempt that claimed `job` may land.

    `attempts` moves on every claim, so it is a fencing token: once the
    reaper has handed a job back to the queue (its heartbeat stopped — one
    database error used to end the heartbeat for good) and another worker has
    claimed it, the first worker's heartbeats and its final verdict must not
    overwrite the row the second one now owns. None = unfenced (a caller that
    holds no claim, such as a test handle). A TypeGuard: past a true answer
    the row exists.
    """
    if job is None:
        return False
    if attempt is None:
        return True
    return job.state == "running" and job.attempts == attempt


def backoff_sec(attempts: int) -> float:
    """How long a job waits after its `attempts`-th try before the next."""
    return min(RETRY_BACKOFF_MAX_SEC, RETRY_BACKOFF_SEC * 2 ** max(0, attempts - 1))


def _close_attempt(job: Job, outcome: str, error: str | None, now: float) -> None:
    """Record the attempt that just ended in the row's own history.

    `error` and `finished_at` describe only the LATEST attempt, and the next
    one overwrites them: a render that failed twice on a full disk and then
    succeeded read as clean, its two failures recorded nowhere (ARCH-5).
    """
    entry: dict[str, Any] = {
        "attempt": job.attempts,
        "started_at": job.started_at,
        "ended_at": now,
        "outcome": outcome,
        "worker": job.claimed_by,
    }
    if error:
        entry["error"] = error[:HISTORY_ERROR_CHARS]
    # Reassigned, never appended in place: SQLAlchemy only sees a JSONB
    # column change when the attribute itself is set.
    job.history = [*(job.history or []), entry][-HISTORY_MAX:]


def _hand_back(job: Job, *, error: str | None, delay: float, now: float, message: str) -> None:
    """Return a job to the queue as the SAME job.

    Still active, so it keeps its dedupe key — a second click while it waits
    finds it rather than starting a twin — and has no `finished_at`.
    Released, so any worker may claim it once `delay` has passed.
    """
    job.state = "queued"
    job.error = error
    job.claimed_by = None
    job.heartbeat_at = None
    job.progress = 0.0
    job.stage = ""
    job.message = message
    job.available_at = now + delay if delay > 0 else None
    job.updated_at = now


def _requeue_row(job: Job, *, error: str | None, output: Any, delay: float | None, now: float) -> None:
    _close_attempt(job, "requeued", error, now)
    wait = backoff_sec(job.attempts) if delay is None else delay
    _hand_back(
        job,
        error=error,
        delay=wait,
        now=now,
        message=f"Attempt {job.attempts} failed; trying again in {round(wait)}s" if wait else "",
    )
    job.result = {"payload": payload_of(job), "output": output}


def requeue(
    job_id: str,
    *,
    error: str | None,
    attempt: int | None = None,
    output: Any = None,
    delay: float | None = None,
) -> bool:
    """Hand a failed attempt back for another, after a backoff.

    The one way a job goes back to the queue from a worker. It keeps the
    dedupe key and gets no `finished_at` (see `_hand_back`), waits
    `backoff_sec` unless told otherwise (`delay=0` for an attempt that did
    nothing wrong — the worker was stopped by a deploy), and keeps the
    attempt in `history`, where the next attempt cannot overwrite it.

    Returns False when nothing was written because the row is no longer this
    attempt's (see `_ours`).
    """
    with session_scope() as db:
        job = db.get(Job, job_id)
        if not _ours(job, attempt):
            if job is not None:
                logger.warning(
                    "Job %s: attempt %s asked for a retry after the job was taken over; not recorded",
                    job_id, attempt,
                )
            return False
        _requeue_row(job, error=error, output=output, delay=delay, now=time.time())
        return True


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
        if state == "queued":
            # The same hand-back as `requeue`, so a retry means one thing
            # however it was asked for.
            _requeue_row(job, error=error, output=output, delay=None, now=now)
            return True
        _close_attempt(job, state, error, now)
        job.state = state
        job.updated_at = now
        job.error = error
        job.result = {"payload": payload_of(job), "output": output}
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
        now = time.time()
        if requeue:
            # No backoff: the attempt did nothing wrong, its worker was
            # stopped, and the next worker should take it the moment it is up.
            _close_attempt(job, "requeued", reason, now)
            _hand_back(job, error=reason, delay=0, now=now, message=job.message)
            return True
        _close_attempt(job, "failed", reason, now)
        job.state = "failed"
        job.error = reason
        job.updated_at = now
        job.finished_at = now
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
            delete(Job)
            .where(
                and_(Job.state.notin_(ACTIVE_STATES), Job.finished_at.is_not(None), Job.finished_at < cutoff)
            )
            # A plain bulk DELETE: no rows are loaded in this session to keep in sync.
            .execution_options(synchronize_session=False)
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
