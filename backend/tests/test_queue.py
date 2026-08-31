"""Durable job queue.

This is the piece with no desktop-era equivalent, and the one whose failures
are silent: a job that never starts looks identical to a job that is slow.
The tests below pin the four rules that make the difference.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from app.dbmodels import Job, Project, User
from app.jobs import queue
from app.jobs.kinds import LANE_HEAVY, LANE_METERED, lane_of, no_retry, priority_of, validate_registry
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


def test_claim_respects_lane_capacity(db, project):
    """LANE_HEAVY is 1 because one ffmpeg render saturates the box. A second
    concurrent render does not go twice as fast; it makes both slower and
    risks the health check."""
    queue.enqueue("export", project_id=project.id)
    queue.enqueue("export_all", project_id=project.id)

    first = queue.claim("worker-1")
    assert first is not None and first.lane == LANE_HEAVY

    # The lane is now full; nothing else heavy may start.
    assert queue.claim("worker-2") is None


def test_different_lanes_run_concurrently(db, project):
    queue.enqueue("export", project_id=project.id)
    queue.enqueue("suggest", project_id=project.id)

    lanes = set()
    for _ in range(2):
        job = queue.claim("worker-1")
        assert job is not None
        lanes.add(job.lane)
    assert lanes == {LANE_HEAVY, LANE_METERED}


def test_a_dead_workers_corpse_does_not_block_its_lane(db, project):
    """A row sitting in `running` is not evidence of life. A worker killed by
    OOM or a redeploy leaves one behind, and counting it as live would hold
    that lane shut forever."""
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
