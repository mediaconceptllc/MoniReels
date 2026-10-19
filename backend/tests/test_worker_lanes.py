"""Lane capacity is a worker's own, not the whole system's (audit ARCH-2).

`queue.claim()` used to count live jobs per lane across EVERY worker: heavy=1
and metered=1 for the whole system. Every producer's transcription,
suggestion and translation shared one paid slot — user B's 30-second
suggestion waited out user A's 8-minute transcription — and with two
exports queued a second worker took nothing, its CPU idle beside the
first's.

A lane bounds a resource, so it is counted where the resource is: the CPU,
RAM and disk of one worker box (heavy), one worker's share of a provider's
concurrency (stt, llm). Each worker counts the jobs IT is running and asks
the queue only for kinds whose lane still has room.
"""
from __future__ import annotations

import pytest

from app.dbmodels import Project, User
from app.jobs import queue
from app.security import hash_password
from tests.conftest import requires_db

pytestmark = requires_db


def _project(db, username: str) -> Project:
    salt, digest = hash_password("pw")
    user = User(username=username, pw_salt=salt, pw_hash=digest)
    db.add(user)
    db.flush()
    row = Project(owner_id=user.id, name=f"{username}'s", doc={})
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def two_users(db):
    return _project(db, "lane_a"), _project(db, "lane_b")


def test_a_second_worker_takes_the_second_export(db, two_users):
    """Two boxes, two renders: one ffmpeg run per box is the heavy lane's
    whole point, and it is a per-box resource."""
    a, b = two_users
    first = queue.enqueue("export", project_id=a.id)
    second = queue.enqueue("export", project_id=b.id)

    one = queue.claim("worker-1")
    two = queue.claim("worker-2")

    assert one is not None and two is not None
    assert {one.id, two.id} == {first, second}


def test_another_users_suggestion_does_not_wait_for_a_transcription(db, two_users):
    a, b = two_users
    queue.enqueue("transcribe", project_id=a.id)
    running = queue.claim("worker-1")
    assert running is not None and running.kind == "transcribe"

    suggest = queue.enqueue("suggest", project_id=b.id)
    nxt = queue.claim("worker-2")
    assert nxt is not None and nxt.id == suggest


def test_workers_racing_for_the_same_jobs_never_share_one(db, two_users):
    """With nothing counted across workers, the row lock is what hands each
    job to exactly one of them: SKIP LOCKED, under real contention."""
    from concurrent.futures import ThreadPoolExecutor

    a, b = two_users
    queued = {queue.enqueue("export", project_id=p.id) for p in (a, b, a)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = list(pool.map(lambda i: queue.claim(f"worker-{i}"), range(8)))

    taken = [job.id for job in got if job is not None]
    assert sorted(taken) == sorted(queued)


# ---------------------------------------------------------------------------
# One worker, counting its own jobs
# ---------------------------------------------------------------------------


async def _run_worker(monkeypatch, env: dict[str, str]) -> list[str]:
    """Run the real worker loop against the real queue until it stops
    claiming, holding every claimed job "running", and return the kinds it
    took. Only the job body is faked: the lane counting and the claim are
    the real ones."""
    import asyncio

    from app import config, worker

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()

    claimed: list[str] = []
    real_claim = queue.claim

    def claim(worker_id, lanes=None):
        job = real_claim(worker_id, lanes)
        if job is not None:
            claimed.append(job.kind)
        return job

    async def stop_when_settled():
        # Full lanes mean no claim at all, so "settled" is measured, not
        # counted: nothing new taken for a quarter of a second.
        seen, quiet = -1, 0
        while not worker._shutdown.is_set():
            await asyncio.sleep(0.025)
            quiet = quiet + 1 if len(claimed) == seen else 0
            seen = len(claimed)
            if quiet >= 10:
                worker._shutdown.set()

    async def hold(_job):
        # A job that keeps its slot until the worker stops.
        while not worker._shutdown.is_set():
            await asyncio.sleep(0.005)

    async def no_chores(_settings):
        return None

    async def schema_ready():
        return None

    monkeypatch.setattr(worker.queue, "claim", claim)
    monkeypatch.setattr(worker, "_run_job", hold)
    monkeypatch.setattr(worker, "_housekeeping", no_chores)
    # PR #63's wait for the API's migration: the test schema is create_all.
    monkeypatch.setattr(worker, "_wait_for_schema", schema_ready, raising=False)
    monkeypatch.setattr(worker, "clear_all_workdirs", lambda: None)
    monkeypatch.setattr(worker, "POLL_IDLE_SEC", 0.005)
    worker._shutdown.clear()
    try:
        await asyncio.wait_for(asyncio.gather(worker.main(), stop_when_settled()), timeout=20)
    finally:
        worker._shutdown.clear()
        config.get_settings.cache_clear()
    return claimed


def _queue_mixed(a: Project, b: Project) -> None:
    queue.enqueue("transcribe", project_id=a.id)
    queue.enqueue("transcribe", project_id=b.id)
    queue.enqueue("suggest", project_id=b.id)
    queue.enqueue("translate", project_id=a.id)
    queue.enqueue("suggest", project_id=a.id)
    queue.enqueue("export", project_id=a.id)
    queue.enqueue("export", project_id=b.id)


@pytest.mark.asyncio
async def test_one_worker_runs_each_lane_up_to_its_own_cap(monkeypatch, db, two_users):
    """heavy=1, stt=1, llm=2: one render, one transcription, two LLM calls —
    and a transcription no longer holds up anyone's suggestion."""
    _queue_mixed(*two_users)

    taken = await _run_worker(monkeypatch, {"WORKER_LANES": "", "WORKER_CONCURRENCY": "0"})

    assert sorted(taken) == ["export", "suggest", "transcribe", "translate"]


@pytest.mark.asyncio
async def test_worker_lanes_make_a_render_pool(monkeypatch, db, two_users):
    """A lane left out is not run here: `heavy=1` takes renders only, and
    leaves the paid work for an I/O pool."""
    _queue_mixed(*two_users)

    taken = await _run_worker(monkeypatch, {"WORKER_LANES": "heavy=1", "WORKER_CONCURRENCY": "0"})

    assert taken == ["export"]


@pytest.mark.asyncio
async def test_worker_lanes_make_an_io_pool(monkeypatch, db, two_users):
    _queue_mixed(*two_users)

    taken = await _run_worker(monkeypatch, {"WORKER_LANES": "stt=2,llm=4", "WORKER_CONCURRENCY": "0"})

    assert sorted(taken) == ["suggest", "suggest", "transcribe", "transcribe", "translate"]


@pytest.mark.asyncio
async def test_worker_concurrency_still_caps_the_total(monkeypatch, db, two_users):
    _queue_mixed(*two_users)

    taken = await _run_worker(monkeypatch, {"WORKER_LANES": "", "WORKER_CONCURRENCY": "2"})

    assert len(taken) == 2


@pytest.mark.asyncio
async def test_a_bad_worker_lanes_stops_the_worker_before_it_claims(monkeypatch, db, two_users):
    _queue_mixed(*two_users)

    with pytest.raises(ValueError, match="hevy"):
        await _run_worker(monkeypatch, {"WORKER_LANES": "hevy=1"})
    assert queue.claim("someone-else") is not None  # nothing was taken


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def test_parse_lanes():
    from app.jobs.kinds import LANE_LIMITS, parse_lanes

    assert parse_lanes("") == LANE_LIMITS
    assert parse_lanes("  ") == LANE_LIMITS
    assert parse_lanes("heavy=2, llm=4") == {"heavy": 2, "stt": 0, "llm": 4}
    assert parse_lanes("stt=0,llm=1") == {"heavy": 0, "stt": 0, "llm": 1}
    for bad in ("hevy=1", "heavy", "heavy=one", "heavy=-1", "heavy=1,heavy=2", "metered=2", "heavy=0"):
        with pytest.raises(ValueError, match="WORKER_LANES"):
            parse_lanes(bad)


def test_open_lanes():
    from app.jobs.kinds import open_lanes

    caps = {"heavy": 1, "stt": 1, "llm": 2}
    assert open_lanes(caps, []) == ["heavy", "stt", "llm"]
    assert open_lanes(caps, ["heavy", "llm"]) == ["stt", "llm"]
    assert open_lanes(caps, ["heavy", "stt", "llm", "llm"]) == []
    assert open_lanes(caps, ["llm"], ceiling=1) == []
    assert open_lanes({"heavy": 1, "stt": 0, "llm": 0}, []) == ["heavy"]


def test_claim_only_offers_the_lanes_asked_for(db, two_users):
    a, _ = two_users
    queue.enqueue("export", project_id=a.id)
    suggest = queue.enqueue("suggest", project_id=a.id)

    job = queue.claim("worker-1", ["llm"])
    assert job is not None and job.id == suggest
    assert queue.claim("worker-1", ["llm", "stt"]) is None
    assert queue.claim("worker-1", []) is None


def test_a_row_queued_under_the_old_lane_name_is_still_claimed(db, two_users):
    """Rows queued before the deploy that split `metered` into stt and llm
    say lane='metered'. The claim goes by kind, and puts the row right."""
    from app.dbmodels import Job

    a, _ = two_users
    queued = queue.enqueue("transcribe", project_id=a.id)
    db.get(Job, queued).lane = "metered"
    db.commit()

    job = queue.claim("worker-1", ["stt"])
    assert job is not None and job.id == queued
    db.expire_all()
    assert db.get(Job, queued).lane == "stt"


def test_a_kind_this_code_does_not_know_is_left_for_one_that_does(db, two_users):
    """A newer deploy's kind, met by an older worker (a rollback, or the old
    worker draining through a deploy), waits instead of being claimed and
    failed for want of a handler."""
    from app.dbmodels import Job

    a, _ = two_users
    queue.enqueue("export", project_id=a.id)
    future = queue.enqueue("export", project_id=a.id)
    db.get(Job, future).kind = "render_v2"
    db.commit()

    first = queue.claim("worker-1")
    assert first is not None and first.kind == "export"
    assert queue.claim("worker-1") is None
    db.expire_all()
    assert db.get(Job, future).state == "queued"


def test_paid_kinds_are_the_metered_lanes():
    """quotas' paid-job cap follows the lanes, so splitting them must not
    drop a paid kind out of it."""
    from app.quotas import PAID_KINDS

    assert PAID_KINDS == {"transcribe", "suggest", "translate"}
