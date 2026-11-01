"""Worker process. Run as its own Railway service: `python -m app.worker`.

Why a separate process rather than a background task inside the API:

ffmpeg and Demucs saturate the box. When they share a container with uvicorn,
the platform's health check stops being answered in time and the service is
restarted — killing the very job that caused the load, and cutting any R2
upload in flight (which then leaves billed multipart garbage behind). Splitting
them means a render can use the whole worker without the API ever going quiet.

Concurrency is bounded by lane (app.jobs.kinds.LANES), not by a single number:
"how many jobs at once" is the wrong question when one job is an HTTP wait and
the next is a full-CPU encode.

This module is the loop: claiming, lanes, heartbeats, settling, housekeeping,
the drain at shutdown, the watchdog and the wait for the schema. What each job
kind does is in app.handlers, one module per kind; the registry that maps a
kind to its handler is HANDLERS, here.
"""
from __future__ import annotations

import asyncio
import functools
import os
import signal
import socket
import threading
import time
import uuid
from typing import Any

from app import metering, migrate, observability, provider_settings, worker_report
from app.ai import usage as llm_usage
from app.audio import dub_bed
from app.config import get_settings, heavy_threads
from app.db import session_scope
from app.handlers.export import handle_export, handle_export_all
from app.handlers.import_video import handle_import_video
from app.handlers.suggest import handle_suggest
from app.handlers.transcribe import handle_transcribe
from app.handlers.translate import handle_translate
from app.jobs import cancel, queue
from app.jobs.kinds import MAX_ATTEMPTS, lane_of, open_lanes, parse_lanes, validate_registry
from app.jobs.queue import JobCancelled, JobHandle
from app.store import ProjectNotFound, get_row
from app.stt.pipeline import separation_available
from app.tts.elevenlabs import TtsError
from app.utils.logging import get_logger, setup_logging
from app.utils.logging import job_id as job_id_var
from app.utils.logging import job_kind as job_kind_var
from app.utils.paths import (
    NeverFits,
    OutOfSpace,
    clear_all_workdirs,
    clear_job_workdir,
    ensure_free,
    purge_trash,
)

logger = get_logger(__name__)

POLL_IDLE_SEC = 2.0
REAP_INTERVAL_SEC = 60.0
# A housekeeping step slower than this is worth a line: the calls it makes
# reach a database and a third-party API, and both can stall for minutes.
SLOW_CHORE_SEC = 10.0
HEARTBEAT_SEC = 5.0
# How long a clean shutdown waits for a report round still writing before it
# removes this worker's row (see _sign_off).
SIGN_OFF_WAIT_SEC = 5.0

_GiB = 1024**3

# Scratch a job needs, checked before it starts rather than discovered
# halfway through as [Errno 28] — from the size of the source it works on:
# a fixed overhead, and how many copies of the source it holds at once
# (audit REL-19: a fixed 8 GB for an export of a 20 GB upload let it start
# and run out halfway).
DISK_BUDGET = {
    "import_video": (1 * _GiB, 1),  # the download; its 16 kHz WAV and thumbnail are small
    "transcribe": (1 * _GiB, 1),    # the download, when the stored WAV cannot serve (_disk_need)
    "export": (2 * _GiB, 2),        # the download, and the clips and render made from it
    "export_all": (3 * _GiB, 3),    # the download, and up to six renders
}
# Demucs's stems are float WAVs: they grow with the length of the audio, not
# the size of the file. ~30 GB for two hours, as the audit measured.
SEPARATION_BYTES_PER_SEC = 4 * 1024 * 1024
# The old fixed budget, for a source whose size is not known (a project from
# before sizes were kept).
DISK_NEED_BYTES = {
    "import_video": 4 * _GiB,
    "transcribe": 6 * _GiB,
    "export": 8 * _GiB,
    "export_all": 12 * _GiB,
    "suggest": 0,
}
# How long a job without room waits before its next look: long enough for a
# render beside it to finish and clear its scratch.
DISK_RETRY_DELAY_S = 300.0

_shutdown = asyncio.Event()

# The loop's own pulse (audit OPS-7). Railway restarts a worker only when its
# process exits — a healthcheck is for deploys, and only for a service with a
# port — so a loop wedged for good (a claim stuck on a dead TCP connection, a
# handler blocking the event loop) sat "running" with every job queued
# behind it. A loop that has not turned for this long is wedged: the claim
# is bounded by the database's statement timeout and every wait in it is
# seconds. Running jobs whose heartbeats stopped with it are requeued by the
# reaper of the restarted worker.
LOOP_STALL_EXIT_SEC = 900.0
WATCHDOG_EVERY_SEC = 30.0
_last_turn = time.monotonic()
_exit = os._exit  # a seam for the tests; os._exit because the loop that would run cleanup is the stuck part


def _turned() -> None:
    global _last_turn
    _last_turn = time.monotonic()


def _watchdog(stop: threading.Event) -> None:
    while not stop.wait(WATCHDOG_EVERY_SEC):
        idle = time.monotonic() - _last_turn
        if idle > LOOP_STALL_EXIT_SEC:
            logger.critical(
                "The worker loop has not turned for %.0fs; exiting so the platform restarts it", idle
            )
            _exit(1)
            return

# Before the first claim: how often to look at the schema, and how often to
# say, while it is behind, that the worker is still waiting.
SCHEMA_POLL_SEC = 3.0
SCHEMA_NOTE_SEC = 60.0


# ---------------------------------------------------------------------------
# Handlers: one module per kind in app.handlers. The registry stays here,
# beside the loop that checks it (main) and runs it (_run_job).
# ---------------------------------------------------------------------------


HANDLERS = {
    "import_video": handle_import_video,
    "transcribe": handle_transcribe,
    "translate": handle_translate,
    "suggest": handle_suggest,
    "export_all": handle_export_all,
    "export": handle_export,
}


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------


async def _heartbeat_loop(handle: JobHandle) -> None:
    """Keeps the row's liveness moving and notices a cancel request.

    A job that reports no progress for minutes — one long LLM call, say — is
    still alive; without this its row would look stale and be handed to
    another worker while it is still running.
    """
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_SEC)
            try:
                await handle.flush()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - see below
                # One failed write must not end liveness for good. It used
                # to: the loop died on the first database error, the row went
                # stale, and the reaper handed a job that was still running
                # to another worker — two renders in one folder, or a paid
                # run failed and bought again.
                logger.warning("Heartbeat for job %s failed; retrying", handle.job_id, exc_info=True)
    except asyncio.CancelledError:
        pass


def _elapsed(started: float) -> float:
    return round(time.time() - started, 1)


def _disk_need(job) -> int:
    """The scratch space `job` needs, from the size of its project's source
    (see DISK_BUDGET). Unknown size: the old fixed budget."""
    if job.kind not in DISK_BUDGET or not job.project_id:
        return DISK_NEED_BYTES.get(job.kind, 0)
    with session_scope() as db:
        try:
            doc = get_row(db, job.project_id).doc or {}
        except ProjectNotFound:
            return 0  # the handler says so, in its own words
        size = doc.get("upload_size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return DISK_NEED_BYTES.get(job.kind, 0)
        base, copies = DISK_BUDGET[job.kind]
        if job.kind != "transcribe":
            return base + copies * size
        # The same choice handle_transcribe makes: the stored 16 kHz WAV
        # (small) unless separation wants the source's own rate.
        video = doc.get("video") or {}
        separating = separation_available(provider_settings.effective(db))
    if video.get("audio_key") and not separating:
        return base
    need = base + copies * size
    if separating:
        need += int(float(video.get("duration_sec") or 0) * SEPARATION_BYTES_PER_SEC)
    return need


def _retry_cannot_help(error: Exception) -> bool:
    """A failure the next attempt would meet exactly as this one did.

    A spent character quota or a revoked key at the voice provider: every
    retry of the export claims the render slot and downloads the source again
    to be refused the same way. Settled at once, like a job bigger than the
    whole disk, with the provider's reason as the error.
    """
    if isinstance(error, dub_bed.DubUnavailable):
        return True
    return isinstance(error, TtsError) and error.ends_the_run



async def _run_job(job) -> None:
    # Each job runs in its own task, so these belong to it alone: every log
    # line it writes — and every thread it hands work to — carries its id.
    job_id_var.set(job.id)
    job_kind_var.set(job.kind)
    handle = JobHandle(job.id, job.kind, job.project_id, queue.payload_of(job), attempt=job.attempts)
    settle = functools.partial(queue.finish, job.id, attempt=job.attempts)
    usage = llm_usage.start()
    meter = metering.start()
    # Every paid request of this job checks this first (app.jobs.cancel). Set
    # here, in the job's own task, so a cancel reaches only its own job.
    cancel.watch(handle.raise_if_cancelled)
    beat = asyncio.create_task(_heartbeat_loop(handle))
    started = time.time()
    try:
        need = await asyncio.to_thread(_disk_need, job)
        if need:
            ensure_free(need)

        result = await HANDLERS[job.kind](handle)

        if handle.cancel_requested:
            await asyncio.to_thread(settle, state="canceled")
            return

        output: dict[str, Any] = dict(result or {})
        output["elapsed_sec"] = _elapsed(started)
        # Only present when the job actually spent money — an absent field
        # says "no paid call", which a zero would not.
        if usage.calls:
            output["llm"] = usage.to_dict()
        await asyncio.to_thread(settle, state="done", output=output)

    except JobCancelled as e:
        output = {"elapsed_sec": _elapsed(started)}
        # How far it got before it stopped, when the stage knows: "7 of 40
        # chunks done, kept" decides whether to run it again.
        if e.detail:
            output["stopped"] = e.detail
        await asyncio.to_thread(settle, state="canceled", output=output)
    except OutOfSpace as e:
        # Raised before the handler ran, so nothing was spent: waiting is
        # safe even for a job that must never be retried once it has called
        # a paid provider.
        output = {"elapsed_sec": _elapsed(started)}
        if isinstance(e, NeverFits) or job.attempts >= MAX_ATTEMPTS:
            error = str(e)
            # Which of the two, by name: the page offers a retry for a full
            # disk (room may come back) and not for one too small in all.
            output["error_code"] = e.code
            if not isinstance(e, NeverFits):
                error += f" {job.attempts} удаа хүлээсэн ч зай гарсангүй — админд хандана уу."
            logger.warning("Job %s (%s) failed for lack of disk: %s", job.id, job.kind, e)
            await asyncio.to_thread(settle, state="failed", error=error, output=output)
        else:
            logger.warning(
                "Job %s (%s) deferred %ds for lack of disk: %s", job.id, job.kind, DISK_RETRY_DELAY_S, e
            )
            await asyncio.to_thread(
                queue.requeue, job.id, error=f"{e} Зай гармагц дахин оролдоно.",
                attempt=job.attempts, output=output, delay=DISK_RETRY_DELAY_S,
            )
    except ProjectNotFound:
        await asyncio.to_thread(
            settle,
            state="failed",
            error="The project was deleted",
            output={"elapsed_sec": _elapsed(started)},
        )
    except Exception as e:  # noqa: BLE001 - a failing job must be recorded, not crash the loop
        logger.exception("Job %s (%s) failed", job.id, job.kind)
        last_attempt = job.no_retry or job.attempts >= MAX_ATTEMPTS or _retry_cannot_help(e)
        error = f"{type(e).__name__}: {e}"
        # How long it ran before it broke. Recorded for every settlement, not
        # just the successful one: "it failed after four minutes" and "it
        # failed instantly" point at completely different causes, and the
        # history could tell them apart only for jobs that worked.
        output = {"elapsed_sec": _elapsed(started)}
        if last_attempt:
            await asyncio.to_thread(settle, state="failed", error=error, output=output)
        else:
            # The same job, back after a backoff, with this attempt kept in
            # its history (queue.requeue).
            await asyncio.to_thread(
                queue.requeue, job.id, error=error, attempt=job.attempts, output=output
            )
    finally:
        beat.cancel()
        # Before anything else here that could raise: this is the one record
        # of what a failed, canceled or abandoned attempt bought. The workdir
        # is cleared even if a second cancellation lands mid-write.
        try:
            await _record_spend(job, meter)
        finally:
            clear_job_workdir(job.id)


async def _record_spend(job, meter: metering.Meter) -> None:
    """Write the attempt's spend to the ledger, whatever became of the job.

    Never raises: a ledger that cannot be written must not turn a finished
    job into a crashed loop. The numbers then go to the log instead, which
    is worse than the table and better than nowhere.
    """
    try:
        await asyncio.to_thread(
            metering.flush, meter, job_id=job.id, job_kind=job.kind, project_id=job.project_id
        )
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception(
            "Could not write the usage ledger for job %s (%s); it spent: %s",
            job.id, job.kind, metering.describe(meter),
        )


async def _housekeeping(settings) -> None:
    """Requeue corpses, drop old rows, abort billed multipart leftovers.

    Never awaited from the loop — see the call site. Each step is timed
    because a stall here used to be invisible: the pass logs nothing unless
    it finds something, so a call blocked for six minutes and a pass with
    nothing to do looked identical from outside.
    """
    try:
        for label, work in (
            # What this worker can do is NOT a step here: it is its own task on
            # the same cadence (see main), because a pass stuck on a dead
            # worker's lock would hold it, and three missed rounds read as a
            # dead worker.
            ("reaping stale jobs", lambda: queue.reap_stale()),
            ("purging old jobs", lambda: queue.purge_old(settings.job_keep_days)),
            ("purging old audit rows", lambda: observability.purge_audit(settings.audit_keep_days)),
            ("deleting retired scratch", purge_trash),
            ("aborting stale uploads", _abort_stale_uploads if settings.r2_enabled else None),
            ("forgetting workers that stopped without saying so", _purge_workers),
        ):
            if work is None:
                continue
            started = time.time()
            count = await asyncio.to_thread(work)
            elapsed = time.time() - started
            if elapsed > SLOW_CHORE_SEC:
                logger.warning("Housekeeping: %s took %.0fs", label, elapsed)
            if isinstance(count, queue.Reaped):
                # Two numbers, never one total. A requeued job runs again by
                # itself; a failed one needs a person, and reporting both
                # under "requeued" leaves that person waiting for a run that
                # is not coming.
                if count.requeued:
                    logger.warning(
                        "Requeued %d job(s) from workers that stopped responding", count.requeued
                    )
                if count.failed:
                    logger.warning(
                        "Failed %d job(s) from workers that stopped responding — this job type "
                        "bills per attempt, so it will NOT run again until somebody starts it",
                        count.failed,
                    )
    except Exception:  # noqa: BLE001 - housekeeping must never kill the loop
        logger.exception("Housekeeping pass failed")


def _abort_stale_uploads() -> int:
    from app import r2

    return r2.abort_stale_uploads()


def _purge_workers() -> int:
    with session_scope() as db:
        return worker_report.purge(db)


def _publish_report(me: worker_report.Identity) -> int:
    """What this worker serves and can do, for the API to read
    (app.worker_report). Every round, not only at start: a worker
    redeployed without Demucs has said so within a minute, before a
    producer asks it for a dub — and a worker that stops saying anything is
    one the queue page can name as gone."""
    with session_scope() as db:
        worker_report.beat(db, me, worker_report.current(me.id))
    return 0


async def _report(me: worker_report.Identity) -> None:
    """One round of `_publish_report`, off the loop. Never raises: a missed
    round is the next round's to make up, not the worker's end."""
    try:
        await asyncio.to_thread(_publish_report, me)
    except Exception:  # noqa: BLE001 - see the docstring
        logger.exception("Could not report what this worker can do; retrying next round")


async def _sign_off(me: worker_report.Identity, report: asyncio.Task | None) -> None:
    """A clean shutdown takes this worker off the list at once. A round
    still writing finishes FIRST — deleting the row under it would leave the
    row behind, and a worker that left read as alive for three minutes."""
    try:
        if report is not None and not report.done():
            await asyncio.wait_for(report, timeout=SIGN_OFF_WAIT_SEC)
        await asyncio.to_thread(_forget, me.id)
    except Exception:  # noqa: BLE001 - leaving must not fail; the row ages out
        logger.exception("Could not remove this worker's heartbeat; it will age out")


def _forget(worker_id: str) -> None:
    with session_scope() as db:
        worker_report.forget(db, worker_id)


async def _drain(running: dict[asyncio.Task, Any], grace_s: int) -> None:
    """Wait for the running jobs, then SETTLE whatever did not make it.

    Bounded, and deliberately so. The wait itself used to be unbounded, which
    reads as the safer choice and is not: the platform's own SIGTERM-to-SIGKILL
    window ends first, and a process killed there settles nothing at all. A
    wait that ends just inside that window is the only one that can report.

    MEASURED: a deploy arrived 58 seconds into a 4-5 minute speech-to-text
    call. `transcribe` and `suggest` are billed per attempt and not retried,
    so the producer paid, saw nothing, and read a generic reaper verdict five
    minutes later.

    A job that still cannot finish is named on the way out — requeued when its
    kind allows a retry, failed when a retry would be a second bill. Same rule
    as `_run_job`'s own failure path, so a job means the same thing however it
    ended.
    """
    logger.info(
        "Shutting down; %d job(s) running, waiting up to %ds", len(running), grace_s
    )
    # `timeout=grace_s` directly, never `grace_s or None`: zero has to mean
    # "settle them now", the choice of an operator who would rather a deploy
    # never blocked. "Wait forever" is the behaviour being removed here and
    # must not be reachable from a setting at all.
    _, pending = await asyncio.wait(set(running), timeout=grace_s)
    if not pending:
        return

    for task in pending:
        job = running[task]
        # A retry is free for a render and a second charge for a paid call.
        requeue = not (job.no_retry or job.attempts >= MAX_ATTEMPTS)
        settled = await asyncio.to_thread(
            queue.abandon,
            job.id,
            requeue=requeue,
            reason="The worker was stopped by a deploy before this job finished.",
            attempt=job.attempts,
        )
        if not settled:
            # It finished in the moment between the timeout and this call.
            continue
        logger.warning(
            "Job %s (%s) did not finish within %ds; %s",
            job.id,
            job.kind,
            grace_s,
            "back in the queue" if requeue else "FAILED — this kind bills per "
            "attempt, so it will not run again until somebody starts it",
        )


async def _wait_for_schema() -> None:
    """Hold the first claim until the API has migrated the database.

    A deploy starts the worker and the API together, and only the API
    migrates (app.migrate). MEASURED (production, the deploy that added
    jobs.started_at): the new worker was up 11 s before the API had
    migrated, and every claim and the housekeeping pass failed with
    "column jobs.started_at does not exist" until it had. Nothing was lost
    that time — a failed claim takes nothing — but a migration that takes
    longer, or fails, left a worker spinning on errors that look like a
    broken database rather than "not migrated yet".

    Waits while the schema is "behind"; a database AHEAD of this code is a
    rollback and goes on with a warning (see migrate.schema_state). Says
    what it is waiting for at once and then once a minute, never per poll.
    """
    noted = -SCHEMA_NOTE_SEC
    while not _shutdown.is_set():
        try:
            state, current, heads = await asyncio.to_thread(migrate.schema_state)
        except Exception:  # noqa: BLE001 - an unreachable database is waited out too
            state, current, heads = "unknown", set(), set()
            if time.monotonic() - noted >= SCHEMA_NOTE_SEC:
                logger.exception("Could not read the database's migration revision; retrying")
                noted = time.monotonic()
        if state == "ready":
            return
        if state == "ahead":
            logger.warning(
                "The database is at %s, newer than this code's %s — running anyway (a rollback?)",
                ", ".join(sorted(current)), ", ".join(sorted(heads)),
            )
            return
        if state == "behind" and time.monotonic() - noted >= SCHEMA_NOTE_SEC:
            logger.warning(
                "Waiting for the API to migrate the database to %s (it is at %s) before claiming jobs",
                ", ".join(sorted(heads)), ", ".join(sorted(current)) or "nothing",
            )
            noted = time.monotonic()
        # A sleep, like the claim loop's: SIGTERM is seen within one poll.
        await asyncio.sleep(SCHEMA_POLL_SEC)


async def main() -> None:
    setup_logging()
    settings = get_settings()
    validate_registry(HANDLERS)
    observability.init_error_reports("worker")

    threads = heavy_threads()
    # Must be set BEFORE torch is imported anywhere: torch reads these at
    # import time and sizing its pool from the host's core count is what
    # gets the whole container throttled.
    os.environ.setdefault("OMP_NUM_THREADS", str(threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(threads))

    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
    # This worker's own capacity per lane (see app.jobs.kinds). A bad
    # WORKER_LANES stops it here, before it claims anything.
    lanes = parse_lanes(settings.worker_lanes)
    me = worker_report.Identity.of(worker_id, lanes)
    ceiling = settings.worker_concurrency
    logger.info(
        "Worker %s starting (threads=%d, lanes=%s, concurrency=%s, separation=%s, dub=%s)",
        worker_id, threads, " ".join(f"{lane}={cap}" for lane, cap in lanes.items()),
        ceiling or "lanes", settings.enable_separation, dub_bed.available(),
    )
    if ceiling and ceiling < sum(lanes.values()):
        logger.warning(
            "WORKER_CONCURRENCY=%d is below the lanes' %d, so work will wait while a lane "
            "still has room. Unset it to let the lanes alone bound this worker.",
            ceiling, sum(lanes.values()),
        )
    clear_all_workdirs()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown.set)

    # Keyed by task so an abandoned one can still be NAMED at shutdown; a
    # bare set of tasks cannot say which job it was.
    # Before housekeeping too: it reads the same tables the claim does.
    await _wait_for_schema()

    _turned()
    watchdog_stop = threading.Event()
    threading.Thread(target=_watchdog, args=(watchdog_stop,), name="watchdog", daemon=True).start()

    running: dict[asyncio.Task, Any] = {}
    chores: asyncio.Task | None = None
    report: asyncio.Task | None = None
    last_reap = last_report = 0.0

    while not _shutdown.is_set():
        _turned()
        # This worker's heartbeat row, on the housekeeping cadence but as its
        # own task: liveness must not wait behind a stuck chore.
        if (report is None or report.done()) and time.time() - last_report > REAP_INTERVAL_SEC:
            last_report = time.time()
            report = asyncio.create_task(_report(me))
        # Housekeeping runs BESIDE the loop, never inside it. Every call it
        # makes can block for minutes — a lock held by a killed worker's
        # connection until TCP gives up, an R2 request that never answers —
        # and inline it sits in front of the first claim, so the queue stops
        # dead with nothing in the log but the startup line. Off to the side,
        # a stuck pass costs housekeeping and nothing else.
        if (chores is None or chores.done()) and time.time() - last_reap > REAP_INTERVAL_SEC:
            last_reap = time.time()
            chores = asyncio.create_task(_housekeeping(settings))

        running = {t: j for t, j in running.items() if not t.done()}
        # Counted HERE, from this worker's own jobs: a lane bounds this box's
        # resources and its share of a provider, not the whole system's.
        room = open_lanes(lanes, [lane_of(j.kind) for j in running.values()], ceiling)
        if not room:
            await asyncio.sleep(POLL_IDLE_SEC)
            continue

        try:
            job = await asyncio.to_thread(queue.claim, worker_id, room)
        except Exception:  # noqa: BLE001 - a database blip must not end the worker
            logger.exception("Failed to claim a job")
            await asyncio.sleep(POLL_IDLE_SEC * 2)
            continue

        if job is None:
            await asyncio.sleep(POLL_IDLE_SEC)
            continue

        logger.info("Claimed job %s (%s, lane=%s)", job.id, job.kind, job.lane)
        running[asyncio.create_task(_run_job(job))] = job

    # Draining waits on its jobs, not on the loop: no pulse to watch.
    watchdog_stop.set()
    if chores and not chores.done():
        chores.cancel()
    if running:
        await _drain(running, settings.worker_shutdown_grace_s)
    # After the drain: until its jobs are settled this worker is still the one
    # running them.
    await _sign_off(me, report)
    logger.info("Worker %s stopped", worker_id)


if __name__ == "__main__":
    asyncio.run(main())
