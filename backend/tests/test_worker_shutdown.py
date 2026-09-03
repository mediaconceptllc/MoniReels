"""What happens to a running job when a deploy arrives.

MEASURED in production:

    14:16:04  POST .../speech-to-text          (a 4-5 minute call)
    14:17:02  Shutting down; waiting for 1 job(s) to finish
    14:17:12  Stopping Container               (ten seconds later)

`transcribe` and `suggest` are billed per attempt and are NOT retried, so that
is money spent for nothing. Worse, nothing said so: the row stayed `running`
with a heartbeat that had stopped moving, and what the producer eventually saw
was the reaper's generic verdict, five minutes after the fact.

The wait was unbounded (`asyncio.gather` with no timeout), which reads as the
safer choice and is not — the platform's SIGTERM-to-SIGKILL window ends first,
and a process killed there settles nothing at all.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from app import worker
from app.config import Settings
from app.dbmodels import Job, Project, User
from app.jobs import queue
from app.security import hash_password
from tests.conftest import must_finish, requires_db

pytestmark = requires_db

RAILWAY_WORKER = Path(__file__).resolve().parents[1] / "railway.worker.json"


# --------------------------------------------------------------------------
# The two numbers that have to agree.
# --------------------------------------------------------------------------


def _draining_seconds() -> int:
    config = json.loads(RAILWAY_WORKER.read_text())
    return int(config["deploy"]["drainingSeconds"])


def test_the_platform_window_is_configured_at_all():
    """Railway's default is measured in seconds, not minutes. Leaving it
    unset is what produced the ten seconds above."""
    assert _draining_seconds() >= 60


def test_shutdown_grace_fits_the_platform_window():
    """The whole mechanism rests on this.

    The app waits `worker_shutdown_grace_s`; the platform kills at
    `drainingSeconds`. Raise the first above the second and the worker is
    killed mid-wait, settling nothing — the wait would then be worse than
    useless, because it also delays the deploy. Two numbers in two files, so
    the agreement is checked rather than remembered.
    """
    grace = Settings().worker_shutdown_grace_s
    window = _draining_seconds()
    assert grace < window, (
        f"WORKER_SHUTDOWN_GRACE_S ({grace}s) must stay under railway.worker.json's "
        f"drainingSeconds ({window}s)"
    )
    # And with room to settle the jobs: that is several database round trips
    # after the wait ends, not an instant.
    assert window - grace >= 10


def test_the_window_covers_the_call_that_prompted_this():
    """A 4-5 minute speech-to-text call is the measured case. A window that
    does not cover it leaves the original bug in place with a longer number."""
    assert _draining_seconds() >= 300


# --------------------------------------------------------------------------
# queue.abandon
# --------------------------------------------------------------------------


@pytest.fixture
def project(db):
    salt, digest = hash_password("pw")
    user = User(username="shutdownowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name="shutdown", doc={})
    db.add(row)
    db.commit()
    return row


def _running(db, project_id: str, kind: str) -> Job:
    # With the dedupe key the routes really use: it is UNIQUE among live jobs,
    # so a job left holding one can never be started again.
    job_id = queue.enqueue(kind, project_id=project_id, dedupe_key=f"{kind}:{project_id}")
    db.expire_all()
    job = db.get(Job, job_id)
    job.state = "running"
    db.commit()
    db.refresh(job)
    return job


def test_an_abandoned_render_goes_back_to_the_queue(db, project):
    job = _running(db, project.id, "export")

    assert queue.abandon(job.id, requeue=True, reason="deploy") is True

    db.expire_all()
    settled = db.get(Job, job.id)
    assert settled.state == "queued"
    assert settled.error == "deploy"


def test_an_abandoned_paid_job_fails_rather_than_running_again(db, project):
    """A retry here is a second bill for work the first attempt may already
    have completed remotely."""
    job = _running(db, project.id, "suggest")

    queue.abandon(job.id, requeue=False, reason="deploy")

    db.expire_all()
    settled = db.get(Job, job.id)
    assert settled.state == "failed"
    assert settled.finished_at is not None
    # The dedupe key has to be released or the same action could never be
    # started again — the producer's only way out of this. Proved by starting
    # it again, which is what they would actually do.
    assert settled.dedupe_key is None
    again = queue.enqueue("suggest", project_id=project.id, dedupe_key=f"suggest:{project.id}")
    assert again != job.id, "the dedupe key was never released"


def test_a_job_that_finished_first_is_left_alone(db, project):
    """The abandoned task is still running when this is called and may settle
    the job a moment later. Its answer is the true one."""
    job = _running(db, project.id, "export")
    queue.finish(job.id, state="done", output={"outputs": []})

    assert queue.abandon(job.id, requeue=True, reason="deploy") is False

    db.expire_all()
    assert db.get(Job, job.id).state == "done"


def test_abandoning_a_job_that_no_longer_exists_is_not_an_error(db, project):
    assert queue.abandon("nosuchjob", requeue=True, reason="deploy") is False


# --------------------------------------------------------------------------
# The drain
# --------------------------------------------------------------------------


#: Every drain test runs under the alarm. A regression here does not fail the
#: suite, it STOPS one — an unbounded wait on a job that never ends is exactly
#: the bug being fixed, so the test for it must not reproduce the symptom.
DRAIN_ALARM_S = 15


def test_a_job_that_finishes_inside_the_window_is_left_to_finish(db, project):
    job = _running(db, project.id, "export")

    async def go():
        async def quick():
            await asyncio.sleep(0)
            queue.finish(job.id, state="done", output={})

        with must_finish("_drain", DRAIN_ALARM_S):
            await worker._drain({asyncio.create_task(quick()): job}, 5)

    asyncio.run(go())

    db.expire_all()
    assert db.get(Job, job.id).state == "done"


def test_a_job_still_running_at_the_deadline_is_named(db, project, caplog):
    """The point of bounding the wait: something to report before the kill."""
    job = _running(db, project.id, "export")

    async def go():
        async def forever():
            await asyncio.sleep(3600)

        with caplog.at_level(logging.WARNING), must_finish("_drain", DRAIN_ALARM_S):
            await worker._drain({asyncio.create_task(forever()): job}, 0)

    asyncio.run(go())

    db.expire_all()
    settled = db.get(Job, job.id)
    assert settled.state == "queued", "a render is retryable, so it comes back"
    assert "did not finish" in caplog.text
    assert job.id in caplog.text


def test_a_paid_job_still_running_at_the_deadline_is_failed_and_says_why(db, project, caplog):
    job = _running(db, project.id, "transcribe")

    async def go():
        async def forever():
            await asyncio.sleep(3600)

        with caplog.at_level(logging.WARNING), must_finish("_drain", DRAIN_ALARM_S):
            await worker._drain({asyncio.create_task(forever()): job}, 0)

    asyncio.run(go())

    db.expire_all()
    settled = db.get(Job, job.id)
    assert settled.state == "failed"
    assert "deploy" in (settled.error or "")
    assert "bills per attempt" in caplog.text


def test_the_wait_is_bounded_even_with_a_job_that_never_ends(db, project):
    """A worker that waits forever is killed by the platform instead, which
    settles nothing. This is the difference between the two."""
    job = _running(db, project.id, "export")

    async def go():
        async def forever():
            await asyncio.sleep(3600)

        with must_finish("_drain", DRAIN_ALARM_S):
            await worker._drain({asyncio.create_task(forever()): job}, 1)

    asyncio.run(go())  # must return, not hang
