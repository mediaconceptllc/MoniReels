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

    def claim(_worker_id):
        nonlocal claims
        claims += 1
        if claims >= 3:
            worker._shutdown.set()
        return None

    monkeypatch.setattr(worker.queue, "reap_stale", never_returns)
    monkeypatch.setattr(worker.queue, "purge_old", lambda *_: 0)
    monkeypatch.setattr(worker.queue, "claim", claim)
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
