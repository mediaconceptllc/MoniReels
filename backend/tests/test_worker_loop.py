"""The worker's startup and housekeeping paths.

Both of these were found in production: a worker logged "starting" and then
went silent for six minutes with a job sitting queued, at 0% CPU and no
network. Nothing in the loop said which call it was stuck in, because the
two candidates ahead of the first `queue.claim` — clearing the last run's
scratch, and the first housekeeping pass — both log only when they find
something to report.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.utils.paths import TRASH_PREFIX, clear_all_workdirs, purge_trash, work_dir


@pytest.fixture(autouse=True)
def _scratch(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_clear_all_workdirs_leaves_a_usable_empty_jobs_dir():
    jobs = work_dir() / "jobs"
    (jobs / "old-job").mkdir(parents=True)
    (jobs / "old-job" / "source.mp4").write_bytes(b"x" * 1024)

    clear_all_workdirs()

    assert jobs.is_dir()
    assert list(jobs.iterdir()) == []


def test_clear_all_workdirs_does_not_delete_inline():
    # Deleting here is what put every queued job behind an rmtree over a
    # volume. The old tree must still exist afterwards, retired for the
    # background pass.
    jobs = work_dir() / "jobs"
    (jobs / "old-job").mkdir(parents=True)
    (jobs / "old-job" / "source.mp4").write_bytes(b"x" * 1024)

    clear_all_workdirs()

    retired = list(work_dir().glob(f"{TRASH_PREFIX}*"))
    assert len(retired) == 1
    assert (retired[0] / "old-job" / "source.mp4").exists()


def test_purge_trash_deletes_what_was_retired():
    jobs = work_dir() / "jobs"
    (jobs / "old-job").mkdir(parents=True)
    clear_all_workdirs()

    assert purge_trash() == 1
    assert list(work_dir().glob(f"{TRASH_PREFIX}*")) == []
    assert (work_dir() / "jobs").is_dir()  # the live dir is never touched


def test_purge_trash_is_a_no_op_when_there_is_nothing_to_delete():
    clear_all_workdirs()
    assert purge_trash() == 0


def test_repeated_startups_do_not_collide():
    # Two workers restarting in the same second must not fight over one name.
    for _ in range(3):
        (work_dir() / "jobs" / "j").mkdir(parents=True, exist_ok=True)
        clear_all_workdirs()
    assert len(list(work_dir().glob(f"{TRASH_PREFIX}*"))) == 3
    assert purge_trash() == 3


@pytest.mark.asyncio
async def test_the_loop_keeps_claiming_while_housekeeping_is_stuck(monkeypatch):
    """The production failure, reduced: one housekeeping call never returns.

    Awaited inline this sat in front of the very first `queue.claim`, so the
    queue stopped dead — a job queued, a worker at 0% CPU, and one startup
    line in the log. The loop must reach `claim` anyway.
    """
    from app import worker

    entered = asyncio.Event()

    def never_returns() -> int:
        entered.set()
        time.sleep(30)  # noqa: ASYNC101 - deliberately wedges a worker thread
        return 0

    claims = 0

    def claim(_worker_id, _lanes=None):
        nonlocal claims
        claims += 1
        if claims >= 3:
            worker._shutdown.set()
        return None

    monkeypatch.setattr(worker.queue, "reap_stale", never_returns)
    monkeypatch.setattr(worker.queue, "purge_old", lambda *_: 0)
    monkeypatch.setattr(worker.queue, "claim", claim)
    monkeypatch.setattr(worker.migrate, "schema_state", lambda: ("ready", set(), set()))
    monkeypatch.setattr(worker, "clear_all_workdirs", lambda: None)
    monkeypatch.setattr(worker, "validate_registry", lambda _h: None)
    monkeypatch.setattr(worker, "POLL_IDLE_SEC", 0.01)
    worker._shutdown.clear()

    try:
        await asyncio.wait_for(worker.main(), timeout=10)
    finally:
        worker._shutdown.clear()

    assert entered.is_set(), "housekeeping never started"
    assert claims >= 3, f"the loop only reached claim {claims} time(s) with housekeeping stuck"


@pytest.mark.asyncio
async def test_housekeeping_survives_a_failing_step(monkeypatch):
    # A blip in one chore must not kill the loop that calls it.
    from app import worker

    def boom() -> int:
        raise RuntimeError("database blip")

    monkeypatch.setattr(worker.queue, "reap_stale", boom)
    settings = type("S", (), {"job_keep_days": 30, "r2_enabled": False})()

    await worker._housekeeping(settings)  # returns, does not raise


@pytest.mark.asyncio
async def test_the_reaping_log_does_not_call_a_failed_job_requeued(monkeypatch, caplog):
    """The line an operator reads to know whether to do anything.

    A `suggest` corpse from a redeploy is FAILED, not requeued — it bills per
    attempt, so nothing will start it but a person. Logging one total under
    "Requeued" told them the run was on its way when it was not.
    """
    from app import worker

    monkeypatch.setattr(
        worker.queue, "reap_stale", lambda: worker.queue.Reaped(requeued=2, failed=1)
    )
    monkeypatch.setattr(worker.queue, "purge_old", lambda *_: 0)
    monkeypatch.setattr(worker, "purge_trash", lambda: 0)
    settings = type("S", (), {"job_keep_days": 30, "r2_enabled": False})()

    with caplog.at_level(logging.WARNING):
        await worker._housekeeping(settings)

    lines = [r.getMessage() for r in caplog.records]
    requeued = [line for line in lines if "Requeued" in line]
    failed = [line for line in lines if "Failed" in line]
    assert requeued and "2 job(s)" in requeued[0]
    assert failed and "1 job(s)" in failed[0]
    assert "will NOT run again" in failed[0], "the operator is not told to start it"


@pytest.mark.asyncio
async def test_a_pass_with_nothing_to_reap_says_nothing(monkeypatch, caplog):
    from app import worker

    monkeypatch.setattr(worker.queue, "reap_stale", lambda: worker.queue.Reaped())
    monkeypatch.setattr(worker.queue, "purge_old", lambda *_: 0)
    monkeypatch.setattr(worker, "purge_trash", lambda: 0)
    settings = type("S", (), {"job_keep_days": 30, "r2_enabled": False})()

    with caplog.at_level(logging.WARNING):
        await worker._housekeeping(settings)

    assert not [r for r in caplog.records if "stopped responding" in r.getMessage()]


def test_the_heartbeat_survives_a_database_error(monkeypatch):
    """One failed write used to end the heartbeat for good: the row went
    stale and the reaper handed a job that was still running to another
    worker. The loop now logs and keeps beating."""
    from app import worker

    monkeypatch.setattr(worker, "HEARTBEAT_SEC", 0.01)
    beats = {"n": 0}

    class Handle:
        job_id = "j1"

        async def flush(self):
            beats["n"] += 1
            if beats["n"] == 1:
                raise RuntimeError("server closed the connection unexpectedly")

    async def run():
        task = asyncio.create_task(worker._heartbeat_loop(Handle()))
        await asyncio.sleep(0.2)
        task.cancel()
        await task

    asyncio.run(run())
    assert beats["n"] >= 3


# --- liveness (audit OPS-7) -------------------------------------------------

def _watch(monkeypatch, worker, *, stale: bool) -> list[int]:
    """Runs the watchdog for a few of its checks; returns the exit codes it asked for."""
    import threading

    exits: list[int] = []
    stop = threading.Event()

    def fake_exit(code: int) -> None:
        exits.append(code)
        stop.set()

    monkeypatch.setattr(worker, "_exit", fake_exit)
    monkeypatch.setattr(worker, "WATCHDOG_EVERY_SEC", 0.01)
    monkeypatch.setattr(worker, "LOOP_STALL_EXIT_SEC", 5.0)
    monkeypatch.setattr(worker, "_last_turn", time.monotonic() - (6.0 if stale else 0.0))
    thread = threading.Thread(target=worker._watchdog, args=(stop,))
    thread.start()
    thread.join(timeout=0.3)
    stop.set()
    thread.join(timeout=1)
    return exits


def test_a_wedged_loop_ends_the_process_so_the_platform_restarts_it(monkeypatch):
    """Railway restarts a worker only when its process exits: a loop stuck
    for good used to sit "running" with every job queued behind it."""
    from app import worker

    assert _watch(monkeypatch, worker, stale=True) == [1]


def test_a_loop_that_turns_is_left_alone(monkeypatch):
    from app import worker

    assert _watch(monkeypatch, worker, stale=False) == []


@pytest.mark.asyncio
async def test_the_loop_keeps_its_pulse_and_stops_the_watchdog_on_the_way_out(monkeypatch):
    import threading

    from app import worker

    began = time.monotonic()
    pulses: list[float] = []

    def claim(*_args):
        pulses.append(worker._last_turn)
        time.sleep(0.005)
        if len(pulses) >= 3:
            worker._shutdown.set()
        return None

    monkeypatch.setattr(worker.queue, "reap_stale", lambda: worker.queue.Reaped())
    monkeypatch.setattr(worker.queue, "purge_old", lambda *_: 0)
    monkeypatch.setattr(worker.queue, "claim", claim)
    monkeypatch.setattr(worker.migrate, "schema_state", lambda: ("ready", set(), set()))
    monkeypatch.setattr(worker, "clear_all_workdirs", lambda: None)
    monkeypatch.setattr(worker, "validate_registry", lambda _h: None)
    monkeypatch.setattr(worker, "purge_trash", lambda: 0)
    monkeypatch.setattr(worker, "POLL_IDLE_SEC", 0.01)
    monkeypatch.setattr(worker, "_last_turn", began - 1000)
    worker._shutdown.clear()
    try:
        await asyncio.wait_for(worker.main(), timeout=10)
    finally:
        worker._shutdown.clear()

    assert pulses[0] >= began
    assert pulses == sorted(set(pulses)), "the pulse did not move with each turn of the loop"
    await asyncio.sleep(0)
    deadline = time.monotonic() + 2
    while any(t.name == "watchdog" and t.is_alive() for t in threading.enumerate()):
        assert time.monotonic() < deadline, "the watchdog outlived the loop"
        time.sleep(0.01)


def test_a_work_dir_this_user_cannot_write_falls_back_to_the_images_own(monkeypatch, tmp_path, caplog):
    """The image runs unprivileged and no service mounts a volume: a WORK_DIR
    set to a root-owned path must not take the worker down — it falls back to
    the image's default, and says so once."""
    import os
    from pathlib import Path

    from app import config
    from app.utils import paths

    blocked = tmp_path / "work"
    fallback = tmp_path / "fallback"
    monkeypatch.setenv("WORK_DIR", str(blocked))
    config.get_settings.cache_clear()
    monkeypatch.setattr(paths, "FALLBACK_WORK_DIR", fallback)
    monkeypatch.setattr(paths, "_fallback_noted", False)
    real_access = os.access
    monkeypatch.setattr(
        os, "access", lambda path, mode: False if Path(path) == blocked else real_access(path, mode)
    )
    with caplog.at_level(logging.WARNING, logger="app.utils.paths"):
        assert paths.work_dir() == fallback
        assert paths.job_workdir("j").parent.parent == fallback
    warnings = [r.getMessage() for r in caplog.records if "is not writable" in r.getMessage()]
    assert len(warnings) == 1, warnings


def test_a_work_dir_with_nowhere_writable_is_named(monkeypatch, tmp_path):
    """Neither WORK_DIR nor the image's default writable: say so, not Errno 13
    halfway through the first job."""
    import os

    from app import config
    from app.utils import paths

    monkeypatch.setenv("WORK_DIR", str(tmp_path / "work"))
    config.get_settings.cache_clear()
    monkeypatch.setattr(paths, "FALLBACK_WORK_DIR", tmp_path / "fallback")
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    with pytest.raises(PermissionError, match="WORK_DIR .* is not writable"):
        paths.work_dir()
