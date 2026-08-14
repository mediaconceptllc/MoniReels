"""JobManager.has_active_jobs() — the idle-shutdown watcher in app/main.py
depends on this to avoid killing the process mid-job (see main.py's
_idle_shutdown_watcher for the real incident this guards against: a real
transcription reached its last chunk right as the idle timer was about to
fire, since outbound Chimege calls never touch _last_request_time).
"""
from __future__ import annotations

import asyncio

import pytest

from app.jobs.manager import JobManager


@pytest.mark.asyncio
async def test_has_active_jobs_true_while_worker_is_running():
    manager = JobManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker(handle):
        started.set()
        await release.wait()
        return "done"

    job = manager.start(worker)
    await started.wait()

    assert manager.has_active_jobs() is True

    release.set()
    await asyncio.sleep(0)  # let _run's finally clause pop the task
    await asyncio.sleep(0)
    assert manager.get(job.id).state.value == "done"
    assert manager.has_active_jobs() is False


@pytest.mark.asyncio
async def test_has_active_jobs_false_with_no_jobs():
    manager = JobManager()
    assert manager.has_active_jobs() is False


@pytest.mark.asyncio
async def test_has_active_jobs_false_after_job_fails():
    manager = JobManager()

    async def worker(handle):
        raise RuntimeError("boom")

    manager.start(worker)
    for _ in range(5):
        await asyncio.sleep(0)

    assert manager.has_active_jobs() is False
