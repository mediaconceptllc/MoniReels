"""What the job panels are told: a waiting job's place in line, each
project's newest job for the list cards, and which brand files exist.

All three answered a question the interface could not: "why has it not
started" (UX-9), "what is happening to this project" (UX-9, the list), and
"will the logo I ticked actually appear" (UX-8).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.api import routes_jobs
from app.dbmodels import Job, Project, Setting, User
from app.jobs.kinds import KINDS, lane_of
from app.security import hash_password
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

pytestmark = requires_db


@pytest.fixture
def client(db):
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    return TestClient(app)


def _user(db, username: str) -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(username=username, pw_salt=salt, pw_hash=digest, role="editor")
    db.add(user)
    db.commit()
    return user


def _auth(client: TestClient, username: str) -> dict:
    response = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _project(db, owner: User, name: str = "p") -> Project:
    row = Project(owner_id=owner.id, name=name, doc={})
    db.add(row)
    db.commit()
    return row


def _job(db, project: Project | None, kind: str, *, state: str = "queued", age: float = 0.0, **kw) -> Job:
    job = Job(
        project_id=project.id if project else None,
        kind=kind,
        state=state,
        lane=lane_of(kind),
        created_at=time.time() - age,
        **kw,
    )
    db.add(job)
    db.commit()
    return job


def _other_lane_kind(kind: str) -> str:
    return next(k for k in KINDS if lane_of(k) != lane_of(kind))


# ---------------------------------------------------------------------------
# queue_ahead
# ---------------------------------------------------------------------------


def test_a_waiting_job_says_how_many_are_ahead_of_it_in_its_lane(client, db):
    me = _user(db, "maker")
    headers = _auth(client, "maker")
    a, b, c = (_project(db, me, n) for n in "abc")
    first = _job(db, a, "transcribe", age=30)
    second = _job(db, b, "transcribe", age=20)
    third = _job(db, c, "transcribe", age=10)

    def ahead(job: Job):
        response = client.get(f"/jobs/{job.id}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["queue_ahead"]

    assert [ahead(first), ahead(second), ahead(third)] == [0, 1, 2]


def test_only_what_a_worker_would_take_first_is_counted(client, db):
    me = _user(db, "maker")
    headers = _auth(client, "maker")
    p = _project(db, me)
    mine = _job(db, p, "transcribe", age=10)
    # Older, but in another lane: a different slot, it does not hold this back.
    _job(db, p, _other_lane_kind("transcribe"), age=100)
    # Older and same lane, but running: the panel says a slot must come free,
    # it is not "ahead in the queue".
    _job(db, p, "transcribe", state="running", age=90)
    # Older, same lane, but still backing off after a failure.
    _job(db, p, "transcribe", age=80, available_at=time.time() + 600)
    # Newer, but more urgent: claim() takes the lower priority number first.
    _job(db, p, "transcribe", age=1, priority=mine.priority - 1)

    assert client.get(f"/jobs/{mine.id}", headers=headers).json()["queue_ahead"] == 1


def test_a_job_that_is_not_waiting_has_no_place_in_line(client, db):
    me = _user(db, "maker")
    headers = _auth(client, "maker")
    p = _project(db, me)
    for state in ("running", "done", "failed", "canceled"):
        job = _job(db, p, "transcribe", state=state)
        assert client.get(f"/jobs/{job.id}", headers=headers).json()["queue_ahead"] is None, state


def test_the_live_stream_carries_the_place_in_line_too(db):
    """The panel follows a job through the stream first: a field only GET
    returned would be missing exactly while the stream works."""
    me = _user(db, "maker")
    p = _project(db, me)
    _job(db, p, "transcribe", age=20)
    mine = _job(db, p, "transcribe", age=10)
    snapshot = routes_jobs._snapshot(mine.id)
    assert snapshot is not None and snapshot["queue_ahead"] == 1
    assert routes_jobs._snapshot("no-such-job") is None


@pytest.mark.asyncio
async def test_the_events_stream_carries_the_place_in_line(db):
    """Not only the helper: the STREAM must be built from it. The panel reads
    the stream first, so a stream still built from the plain row would drop
    the place in line exactly while everything else works.

    The endpoint is driven directly and its first frame taken: a waiting job
    never ends the stream, and reading one over HTTP would wait for that."""
    from app.security import Principal

    me = _user(db, "maker")
    p = _project(db, me)
    _job(db, p, "transcribe", age=20)
    mine = _job(db, p, "transcribe", age=10)

    response = await routes_jobs.job_events(
        mine.id, principal=Principal(id=me.id, username=me.username, role="editor"), db=db
    )
    frames = response.body_iterator
    try:
        first = await asyncio.wait_for(frames.__anext__(), timeout=10)
    finally:
        await frames.aclose()
    assert first["event"] == "job"
    assert json.loads(first["data"])["queue_ahead"] == 1


# ---------------------------------------------------------------------------
# /jobs/latest
# ---------------------------------------------------------------------------


def test_latest_is_each_projects_newest_job_and_only_the_callers(client, db):
    me = _user(db, "maker")
    other = _user(db, "other")
    headers = _auth(client, "maker")
    a = _project(db, me, "a")
    b = _project(db, me, "b")
    theirs = _project(db, other, "theirs")
    _job(db, a, "transcribe", state="failed", age=100)
    newest_a = _job(db, a, "suggest", state="running", age=5, progress=0.42)
    newest_b = _job(db, b, "transcribe", state="failed", age=50)
    _job(db, theirs, "transcribe", age=1)
    _job(db, None, "import_video", age=1)  # no project at all

    response = client.get("/jobs/latest", headers=headers)
    assert response.status_code == 200, response.text
    by_project = {row["project_id"]: row for row in response.json()["jobs"]}
    assert set(by_project) == {a.id, b.id}
    assert by_project[a.id]["job_id"] == newest_a.id
    assert by_project[a.id]["state"] == "running"
    assert by_project[a.id]["progress"] == 0.42
    assert by_project[b.id]["job_id"] == newest_b.id
    assert by_project[b.id]["state"] == "failed"
    # Only what a card shows.
    assert set(by_project[a.id]) == {"project_id", "job_id", "kind", "state", "progress", "finished_at"}


def test_latest_needs_a_signed_in_caller(client):
    assert client.get("/jobs/latest").status_code == 401


# ---------------------------------------------------------------------------
# brand availability for the export panel
# ---------------------------------------------------------------------------


def test_the_export_panel_is_told_which_brand_files_exist(client, db):
    _user(db, "maker")
    headers = _auth(client, "maker")
    empty = client.get("/projects/providers/status", headers=headers).json()
    assert empty["brand"] == {"logo": False, "intro": False, "outro": False}

    db.add(Setting(key="brand_logo_key", value="brand/logo-1.png", updated_at=time.time()))
    db.commit()
    after = client.get("/projects/providers/status", headers=headers).json()
    assert after["brand"] == {"logo": True, "intro": False, "outro": False}
    # Whether, never where: no key or URL reaches a producer's page.
    assert "brand/logo-1.png" not in str(after)
