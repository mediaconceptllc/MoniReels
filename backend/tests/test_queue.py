"""Durable job queue.

This is the piece with no desktop-era equivalent, and the one whose failures
are silent: a job that never starts looks identical to a job that is slow.
The tests below pin the four rules that make the difference.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select

from app.dbmodels import Job, Project, User
from app.jobs import queue
from app.jobs.kinds import (
    LANE_HEAVY,
    LANE_LIMITS,
    LANE_LLM,
    LANES,
    METERED_LANES,
    lane_of,
    no_retry,
    open_lanes,
    priority_of,
    validate_registry,
)
from app.security import hash_password
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def project(db):
    salt, digest = hash_password("pw")
    user = User(username="qowner", pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name="queue test", doc={})
    db.add(row)
    db.commit()
    return row


def test_registry_is_complete():
    """A kind missing from any one map is a job that queues and never runs."""
    from app.worker import HANDLERS

    validate_registry(HANDLERS)


def test_priority_comes_from_duration_not_urgency():
    """Ordering by importance is what puts a 3-second call behind a
    20-minute render."""
    assert priority_of("import_video") < priority_of("suggest") < priority_of("export_all")


def test_unknown_kind_sorts_last_and_gets_the_strictest_lane():
    """A mistake must never let unrecognised work jump the queue or assume a
    cheap resource."""
    assert priority_of("nonexistent") >= priority_of("export_all")
    assert lane_of("nonexistent") == LANE_HEAVY


def test_paid_kinds_are_not_retried():
    """One attempt is one bill: a retry charges again for work the first
    attempt may already have completed remotely."""
    assert no_retry("transcribe") and no_retry("suggest")
    assert not no_retry("export")


def test_every_kind_in_a_metered_lane_is_not_retried():
    """The rule, not a list. The metered lanes exist for services that bill
    per call, so a kind placed there and forgotten in NO_RETRY would retry —
    and charge — up to MAX_ATTEMPTS times. The list above was written by hand
    and did not grow when `translate` was added."""
    metered = {kind for kind, lane in LANES.items() if lane in METERED_LANES}
    assert metered
    assert [kind for kind in sorted(metered) if not no_retry(kind)] == []


def test_enqueue_dedupes_live_jobs(db, project):
    first = queue.enqueue("transcribe", project_id=project.id, dedupe_key=f"transcribe:{project.id}")
    second = queue.enqueue("transcribe", project_id=project.id, dedupe_key=f"transcribe:{project.id}")
    assert first == second
    assert db.scalar(select(Job).where(Job.id == first)) is not None
    assert len(db.scalars(select(Job)).all()) == 1


def test_dedupe_key_is_released_when_the_job_settles(db, project):
    """Otherwise a project could only ever be transcribed once, forever."""
    first = queue.enqueue("transcribe", project_id=project.id, dedupe_key="k")
    queue.finish(first, state="done", output={"ok": True})

    second = queue.enqueue("transcribe", project_id=project.id, dedupe_key="k")
    assert second != first


def test_claim_takes_the_cheapest_job_first(db, project):
    long_job = queue.enqueue("export_all", project_id=project.id)
    time.sleep(0.01)
    quick_job = queue.enqueue("import_video", project_id=project.id)

    claimed = queue.claim("worker-1")
    assert claimed is not None and claimed.id == quick_job
    assert long_job != quick_job


def test_claim_respects_the_workers_lane_capacity(db, project):
    """LANE_HEAVY is 1 because one ffmpeg render saturates the box. A second
    concurrent render does not go twice as fast; it makes both slower and
    risks the health check.

    The box is the WORKER's: it counts its own running jobs and asks only
    for lanes with room (it used to be one render for the whole system — see
    test_worker_lanes). With its heavy slot taken, it is not offered the
    second export."""
    queue.enqueue("export", project_id=project.id)
    queue.enqueue("export_all", project_id=project.id)

    first = queue.claim("worker-1")
    assert first is not None and first.lane == LANE_HEAVY

    room = open_lanes(LANE_LIMITS, [first.lane])
    assert LANE_HEAVY not in room
    assert queue.claim("worker-1", room) is None


def test_different_lanes_run_concurrently(db, project):
    queue.enqueue("export", project_id=project.id)
    queue.enqueue("suggest", project_id=project.id)

    lanes = set()
    for _ in range(2):
        job = queue.claim("worker-1")
        assert job is not None
        lanes.add(job.lane)
    assert lanes == {LANE_HEAVY, LANE_LLM}


def test_a_dead_workers_corpse_does_not_block_its_lane(db, project):
    """A row sitting in `running` is not evidence of life. A worker killed by
    OOM or a redeploy leaves one behind, and counting it as live would hold
    that lane shut forever. (Nothing counts rows any more — each worker
    counts its own jobs — but the property stays pinned.)"""
    queue.enqueue("export", project_id=project.id)
    claimed = queue.claim("worker-dead")
    assert claimed is not None

    row = db.get(Job, claimed.id)
    row.heartbeat_at = time.time() - 10_000
    db.commit()

    queue.enqueue("export_all", project_id=project.id)
    assert queue.claim("worker-2") is not None, "a stale corpse blocked the lane"


def test_reap_requeues_a_stale_retryable_job(db, project):
    queue.enqueue("export", project_id=project.id)
    claimed = queue.claim("worker-dead")
    row = db.get(Job, claimed.id)
    row.heartbeat_at = time.time() - 10_000
    db.commit()

    assert queue.reap_stale() == queue.Reaped(requeued=1, failed=0)
    db.expire_all()
    assert db.get(Job, claimed.id).state == "queued"


def test_reap_fails_a_stale_paid_job_instead_of_recharging(db, project):
    queue.enqueue("transcribe", project_id=project.id)
    claimed = queue.claim("worker-dead")
    row = db.get(Job, claimed.id)
    row.heartbeat_at = time.time() - 10_000
    db.commit()

    outcome = queue.reap_stale()
    db.expire_all()
    reaped = db.get(Job, claimed.id)
    assert reaped.state == "failed"
    assert "billed" in (reaped.error or "")
    # The count has to say FAILED, not "reaped". A caller handed one total
    # cannot tell the operator whether the work is coming back.
    assert outcome == queue.Reaped(requeued=0, failed=1)


def test_heartbeat_reports_a_cancel_request(db, project):
    job_id = queue.enqueue("export", project_id=project.id)
    queue.claim("worker-1")

    assert queue.heartbeat(job_id, progress=0.5, stage="render", message=None) is True
    queue.request_cancel(job_id)
    assert queue.heartbeat(job_id, progress=0.6, stage=None, message=None) is False


def test_cancelling_a_queued_job_settles_it_immediately(db, project):
    """Nothing has started, so there is nothing to notice the request."""
    job_id = queue.enqueue("export", project_id=project.id)
    assert queue.request_cancel(job_id) is True
    db.expire_all()
    assert db.get(Job, job_id).state == "canceled"


def test_queue_overview_flags_a_missing_worker(db, project):
    """Waiting work with nothing alive to do it is the single most common
    cause of "my job never starts" — and invisible without this."""
    queue.enqueue("export", project_id=project.id)
    overview = queue.queue_overview()
    assert overview["waiting"] == 1
    assert overview["live_workers"] == 0
    assert overview["stalled"] is True


def test_deleting_a_project_cancels_its_queued_work(db, project):
    queue.enqueue("export", project_id=project.id)
    queue.enqueue("suggest", project_id=project.id)
    assert queue.drop_project_jobs(project.id) == 2
    db.expire_all()
    assert {j.state for j in db.scalars(select(Job)).all()} == {"canceled"}


def test_payload_survives_the_round_trip_but_is_not_exposed(db, project):
    """`payload` is the queue's own bookkeeping; only the handler's return
    value is something a client asked for."""
    job_id = queue.enqueue("export", project_id=project.id, payload={"secret": "internal"})
    row = db.get(Job, job_id)
    assert queue.payload_of(row) == {"secret": "internal"}
    assert "secret" not in str(queue.to_dict(row))

    queue.finish(job_id, state="done", output={"outputs": []})
    db.expire_all()
    assert queue.to_dict(db.get(Job, job_id))["result"] == {"outputs": []}


def test_a_reaping_pass_reports_requeued_and_failed_apart(db, project):
    """One pass can do both at once. Summing them is what let the worker log
    "Requeued 1 job" for a corpse it had actually failed — and the person
    reading that waited for a run that was never coming."""
    queue.enqueue("export", project_id=project.id)
    retryable = queue.claim("worker-dead")
    queue.enqueue("transcribe", project_id=project.id)
    paid = queue.claim("worker-dead")
    assert retryable is not None and paid is not None, "both must be in flight to be reaped"

    for claimed in (retryable, paid):
        db.get(Job, claimed.id).heartbeat_at = time.time() - 10_000
    db.commit()

    outcome = queue.reap_stale()

    assert outcome == queue.Reaped(requeued=1, failed=1)
    assert outcome.total == 2
    assert bool(outcome) is True


def test_a_pass_that_found_nothing_is_falsy(db, project):
    """The worker logs only when there is something to say."""
    assert bool(queue.reap_stale()) is False


# ---------------------------------------------------------------------------
# Concurrency: racing clicks, retries, and a job taken over by another worker
# ---------------------------------------------------------------------------


def test_two_clicks_racing_past_the_dedupe_check_get_one_job(db, project, monkeypatch):
    """Both requests can read "no live job with this key" before either has
    inserted one. The loser's INSERT then hits the UNIQUE key — which used to
    surface as a 500 (29 of 30 double-submits in a burst)."""
    first = queue.enqueue("suggest", project_id=project.id, dedupe_key=f"suggest:{project.id}")

    real = queue._active_with_key
    calls = {"n": 0}

    def stale_check(session, key):
        calls["n"] += 1
        # The second request's pre-check ran before the first one committed.
        return None if calls["n"] == 1 else real(session, key)

    monkeypatch.setattr(queue, "_active_with_key", stale_check)
    second = queue.enqueue("suggest", project_id=project.id, dedupe_key=f"suggest:{project.id}")

    assert second == first
    assert db.scalar(select(func.count()).select_from(Job).where(Job.project_id == project.id)) == 1


def test_a_retry_keeps_its_dedupe_key_and_is_not_finished(db, project):
    """A job handed back for another attempt is still the live job. Dropping
    its key let a second click queue a duplicate next to it."""
    key = f"export_all:{project.id}:all"
    job_id = queue.enqueue("export_all", project_id=project.id, dedupe_key=key)
    claimed = queue.claim("worker-1")

    assert queue.finish(job_id, state="queued", error="boom", attempt=claimed.attempts) is True

    db.expire_all()
    job = db.get(Job, job_id)
    assert (job.state, job.dedupe_key, job.finished_at, job.claimed_by) == ("queued", key, None, None)
    assert queue.enqueue("export_all", project_id=project.id, dedupe_key=key) == job_id


def _taken_over(db, project) -> tuple[str, int, int]:
    """A job whose first worker stopped heartbeating, was reaped, and has been
    claimed by a second worker. Returns (id, first attempt, second attempt)."""
    job_id = queue.enqueue("export", project_id=project.id)
    first = queue.claim("worker-1").attempts
    db.expire_all()
    job = db.get(Job, job_id)
    job.heartbeat_at = time.time() - 10_000
    db.commit()
    assert queue.reap_stale().requeued == 1
    # The reaped job waits out its backoff before anyone may claim it.
    db.expire_all()
    job = db.get(Job, job_id)
    assert job.available_at is not None and job.available_at > time.time()
    job.available_at = time.time() - 1
    db.commit()
    second = queue.claim("worker-2").attempts
    assert second != first
    return job_id, first, second


def test_a_taken_over_jobs_first_worker_cannot_write_to_it(db, project):
    """After the reaper has given the job to another worker, the first one's
    heartbeat says "stop", and its verdict lands nowhere — it used to
    overwrite the row the second worker owned."""
    job_id, first, second = _taken_over(db, project)

    assert queue.heartbeat(job_id, progress=0.9, stage="old", message=None, attempt=first) is False
    assert queue.finish(job_id, state="done", output={"from": "worker-1"}, attempt=first) is False
    assert queue.abandon(job_id, requeue=True, reason="deploy", attempt=first) is False

    db.expire_all()
    job = db.get(Job, job_id)
    assert job.state == "running" and job.claimed_by == "worker-2"

    assert queue.heartbeat(job_id, progress=0.5, stage="render", message=None, attempt=second) is True
    assert queue.finish(job_id, state="done", output={"from": "worker-2"}, attempt=second) is True
    db.expire_all()
    assert db.get(Job, job_id).state == "done"


def test_a_database_error_in_progress_does_not_fail_the_job(db, project, monkeypatch):
    """Progress is a report. A flush that fails inside set_progress used to
    raise into the handler and fail a paid run that was going fine; the value
    is kept for the next flush instead."""
    import asyncio

    job_id = queue.enqueue("transcribe", project_id=project.id)
    handle = queue.JobHandle(job_id, "transcribe", project.id, {})

    def broken(*args, **kwargs):
        raise RuntimeError("server closed the connection unexpectedly")

    monkeypatch.setattr(queue, "heartbeat", broken)
    asyncio.run(handle.set_progress(0.4, stage="chunks"))

    assert handle._pending == {"progress": 0.4, "stage": "chunks"}
