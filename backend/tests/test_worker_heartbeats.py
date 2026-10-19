"""One heartbeat row per worker, and a lane nobody serves named (#64's
follow-up).

The single `worker_report` setting was last-writer-wins. With a render pool
(`WORKER_LANES=heavy=1`, built with Demucs) and an I/O pool (`stt=2,llm=4`,
built without) the API's answer to "can an export remove the speech?"
flipped with whichever spoke last — and "is a worker alive" said yes while
every export waited for a render pool that was down.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import providers, worker_report
from app.dbmodels import Job, Project, User, WorkerHeartbeat
from app.jobs import queue
from app.security import hash_password
from tests.conftest import requires_db

pytestmark = requires_db

RENDER = {"heavy": 1, "stt": 0, "llm": 0}
IO = {"heavy": 0, "stt": 2, "llm": 4}


def _beat(db, worker_id: str, lanes: dict[str, int], *, separation: bool, at: float | None = None,
          free: int = 9_000_000_000) -> worker_report.Identity:
    me = worker_report.Identity.of(worker_id, lanes, started_at=1_700_000_000.0)
    worker_report.beat(db, me, worker_report.WorkerReport(
        separation=separation, model="htdemucs", at=time.time() if at is None else at,
        disk={"free_bytes": free, "total_bytes": 50_000_000_000, "min_free_bytes": 1024},
        worker=me.id,
    ))
    db.commit()
    return me


def _lanes(overview: dict) -> dict[str, tuple[int, int, bool | None]]:
    return {row["lane"]: (row["runnable"], row["workers"], row["served"]) for row in overview["lanes"]}


def test_two_workers_with_different_lanes_each_keep_their_own_row(db):
    _beat(db, "render-1", RENDER, separation=True)
    # The I/O pool speaks LAST: with one shared row this was the answer.
    _beat(db, "io-1", IO, separation=False)
    queue.enqueue("export", project_id=None)
    queue.enqueue("transcribe", project_id=None)

    rows = {b.id: b for b in worker_report.heartbeats(db)}
    assert set(rows) == {"render-1", "io-1"}
    assert rows["render-1"].lanes == RENDER and rows["io-1"].lanes == IO
    assert rows["render-1"].started_at == 1_700_000_000.0

    overview = queue.queue_overview(db=db)
    assert _lanes(overview) == {"heavy": (1, 1, True), "stt": (1, 1, True), "llm": (0, 1, True)}
    assert overview["unserved_lanes"] == [] and overview["stalled"] is False

    # The clean-dub guard asks the workers that render, whoever spoke last.
    report = worker_report.read(db)
    assert report is not None and report.separation is True and report.partial is False
    assert providers.separation(report).ready
    # And the old single row, for an API from before the heartbeats, was
    # written by the render worker only — the I/O pool's "cannot" never
    # reached it.
    legacy = worker_report.read_legacy(db)
    assert legacy is not None and legacy.worker == "render-1" and legacy.separation is True


def test_a_dead_worker_is_not_counted_and_ages_out(db):
    now = time.time()
    _beat(db, "render-1", RENDER, separation=True, at=now - worker_report.STALE_SEC - 1)
    _beat(db, "io-1", IO, separation=False, at=now)
    queue.enqueue("export", project_id=None)

    overview = queue.queue_overview(db=db)
    # Something is alive, so this is not "the worker service is down" — it
    # is "nobody renders", and that is what the page must say.
    assert overview["stalled"] is False
    assert _lanes(overview)["heavy"] == (1, 0, False)
    assert overview["unserved_lanes"] == ["heavy"]
    # The dead render worker's word is no longer the export guard's answer:
    # the guard falls back to the old row, as before the heartbeats.
    assert worker_report.read(db) == worker_report.read_legacy(db)

    # Kept long enough to be named, then purged; the live row stays.
    assert worker_report.purge(db, now=now) == 0
    assert worker_report.purge(db, now=now + worker_report.FORGET_SEC) == 1
    db.commit()
    assert [b.id for b in worker_report.heartbeats(db)] == ["io-1"]


def test_only_a_lane_with_runnable_work_is_warned_about(db):
    _beat(db, "io-1", IO, separation=False)

    overview = queue.queue_overview(db=db)
    assert _lanes(overview)["heavy"] == (0, 0, False)  # shown, but nothing waits
    assert overview["unserved_lanes"] == []

    # A retry still backing off is not work anyone could take yet.
    job_id = queue.enqueue("export", project_id=None)
    db.get(Job, job_id).available_at = time.time() + 600
    db.commit()
    assert queue.queue_overview(db=db)["unserved_lanes"] == []

    queue.enqueue("import_video", project_id=None)
    assert queue.queue_overview(db=db)["unserved_lanes"] == ["heavy"]


def test_served_is_unknown_rather_than_a_false_alarm(db):
    queue.enqueue("export", project_id=None)

    # No worker running this code has spoken: unknown, and the old "stalled"
    # sign is what answers.
    overview = queue.queue_overview(db=db)
    assert {row["served"] for row in overview["lanes"]} == {None}
    assert overview["unserved_lanes"] == [] and overview["stalled"] is True

    # An older build alive mid-deploy: it writes the single row without a
    # worker name, and does not say its lanes — it may be the one rendering.
    worker_report.publish(db, worker_report.WorkerReport(separation=True, model="htdemucs", at=time.time()))
    db.commit()
    _beat(db, "io-1", IO, separation=False)
    overview = queue.queue_overview(db=db)
    assert _lanes(overview)["heavy"] == (1, 0, None)
    assert overview["unserved_lanes"] == [] and overview["stalled"] is False


def test_every_render_worker_must_be_able_to_separate(db):
    """An export goes to whichever render worker claims it first."""
    _beat(db, "render-a", RENDER, separation=True, free=8_000_000_000)
    _beat(db, "render-b", RENDER, separation=False, free=2_000_000_000)

    report = worker_report.read(db)
    assert report is not None and report.separation is False and report.partial is True
    capability = providers.separation(report)
    assert not capability.ready and "зарим" in (capability.blocked or "")
    # The disk an export can fail on is the tightest one.
    assert report.disk is not None and report.disk["free_bytes"] == 2_000_000_000


def test_a_malformed_row_reads_as_serving_nothing(db):
    db.add(WorkerHeartbeat(
        id="odd", lanes={"heavy": True, "stt": "2", "llm": -1, "gpu": 3}, started_at=0.0,
        seen_at=time.time(), separation=False, model="", disk={"free_bytes": "lots"},
    ))
    db.commit()
    (row,) = worker_report.heartbeats(db)
    assert row.lanes == {"heavy": 0, "stt": 0, "llm": 0} and row.disk is None


def test_a_clean_shutdown_removes_the_worker_at_once(db, monkeypatch):
    """The worker loop writes its row on the housekeeping cadence and takes
    it away when it stops cleanly — not three minutes later."""
    from app import config, worker

    monkeypatch.setenv("WORKER_LANES", "heavy=1")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def")
    config.get_settings.cache_clear()
    seen: list[worker_report.Heartbeat] = []

    def claim(_worker_id, _lanes=None):
        rows = worker_report.heartbeats(db)
        db.rollback()  # a fresh snapshot on the next look
        if rows:
            seen.extend(rows)
            worker._shutdown.set()
        return None

    async def schema_ready():
        return None

    async def no_chores(_settings):
        return None

    monkeypatch.setattr(worker.queue, "claim", claim)
    monkeypatch.setattr(worker, "_wait_for_schema", schema_ready)
    monkeypatch.setattr(worker, "_housekeeping", no_chores)
    monkeypatch.setattr(worker, "clear_all_workdirs", lambda: None)
    monkeypatch.setattr(worker, "validate_registry", lambda _h: None)
    monkeypatch.setattr(worker, "POLL_IDLE_SEC", 0.01)
    worker._shutdown.clear()
    try:
        asyncio.run(asyncio.wait_for(worker.main(), timeout=20))
    finally:
        worker._shutdown.clear()
        config.get_settings.cache_clear()

    assert seen, "the worker never wrote its heartbeat"
    assert seen[0].lanes == {"heavy": 1, "stt": 0, "llm": 0}
    assert seen[0].commit == "abc123def"
    db.expire_all()
    assert worker_report.heartbeats(db) == []


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db):
    from app.config import get_settings
    from app.main import app
    from app.video.capabilities import Capabilities

    get_settings.cache_clear()
    app.state.capabilities = Capabilities(ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=[])
    app.state.ffmpeg = None
    return TestClient(app)


def _login(db, client: TestClient, username: str, role: str) -> dict:
    salt, digest = hash_password("hunter2hunter2")
    db.add(User(username=username, pw_salt=salt, pw_hash=digest, role=role))
    db.commit()
    token = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"}).json()
    return {"Authorization": f"Bearer {token['token']}"}


def test_the_queue_says_which_lane_nobody_serves_and_only_admins_see_workers(db, client):
    editor = _login(db, client, "alice", "editor")
    admin = _login(db, client, "root", "admin")
    _beat(db, "io-1", IO, separation=False)
    queue.enqueue("export", project_id=None)

    # A job of nobody's project is not the editor's to be told about.
    body = client.get("/jobs/queue", headers=editor).json()
    assert body["unserved_lanes"] == []
    alice = db.query(User).filter_by(username="alice").one()
    mine = Project(owner_id=alice.id, name="a", doc={})
    db.add(mine)
    db.commit()
    queue.enqueue("export", project_id=mine.id)
    body = client.get("/jobs/queue", headers=editor).json()
    assert body["unserved_lanes"] == ["heavy"]
    # Names of machines and commits are an operator's business.
    assert "workers" not in body and "lanes" not in body

    stats = client.get("/admin/jobs/stats", headers=admin).json()["queue"]
    assert stats["unserved_lanes"] == ["heavy"]
    (only,) = stats["workers"]
    assert only["id"] == "io-1" and only["live"] is True and only["lanes"] == IO
