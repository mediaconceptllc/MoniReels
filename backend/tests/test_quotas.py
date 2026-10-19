"""Per-account limits on the shared queue (SEC-4).

The audit measured what an ordinary editor could do before: queue 25 exports
at once, create 40 projects in 0.4 s, and — with a stolen password — run an
unbounded paid bill while holding the one paid lane every producer waits on.
These hold each limit to the server, including when two requests race.
"""
from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import quotas
from app.config import get_settings
from app.db import get_session_factory
from app.dbmodels import Job, Project, UsageEvent, User
from app.jobs import queue
from app.security import Principal, hash_password
from tests.conftest import requires_db

pytestmark = requires_db

os.environ.setdefault("R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("R2_ACCESS_KEY_ID", "testkey")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "testsecret")
os.environ.setdefault("R2_BUCKET", "testbucket")

VIDEO = {
    "source_key": "sources/x/source.mp4", "duration_sec": 60.0, "width": 1920,
    "height": 1080, "fps": 30.0, "has_audio": True, "codec": "h264", "thumbnail_key": "",
}


@pytest.fixture
def client(db):
    from app.main import app
    from app.video.capabilities import Capabilities

    get_settings.cache_clear()
    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    return TestClient(app)


@pytest.fixture
def limits(monkeypatch):
    """Set the limits for one test; everything else stays the real settings."""

    def apply(**values):
        settings = get_settings().model_copy(update=values)
        monkeypatch.setattr(quotas, "get_settings", lambda: settings)
        return settings

    return apply


def _user(db, username: str, role: str = "editor") -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(username=username, pw_salt=salt, pw_hash=digest, role=role)
    db.add(user)
    db.commit()
    return user


def _auth(client, username: str) -> dict:
    response = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _principal(user: User) -> Principal:
    return Principal(id=user.id, username=user.username, role=user.role)


def _project(db, owner: User, **doc) -> Project:
    row = Project(owner_id=owner.id, name="p", doc={"video": VIDEO, **doc})
    db.add(row)
    db.commit()
    return row


def _queue(db, user: User, kind: str, *, key: str | None = None, paid: bool | None = None) -> str:
    """What a route does: admit, then enqueue in the same transaction."""
    row = _project(db, user)
    quotas.admit_job(db, _principal(user), kind, paid=paid, dedupe_key=key)
    return queue.enqueue(kind, project_id=row.id, dedupe_key=key, db=db)


def _refused(call) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        call()
    return caught.value


# ---------------------------------------------------------------------------
# Active jobs
# ---------------------------------------------------------------------------


def test_an_account_is_refused_past_its_active_job_cap(db, limits):
    limits(quota_active_jobs=2, quota_active_paid_jobs=0)
    alice = _user(db, "alice")
    _queue(db, alice, "export")
    _queue(db, alice, "export_all")

    refused = _refused(lambda: _queue(db, alice, "export"))
    assert refused.status_code == 429
    assert "2 ажил" in refused.detail and "дээд тал нь 2" in refused.detail

    # Another account is not held back by Alice's queue.
    bob = _user(db, "bob")
    _queue(db, bob, "export")


def test_a_finished_job_frees_its_place(db, limits):
    limits(quota_active_jobs=1, quota_active_paid_jobs=0)
    alice = _user(db, "alice")
    job_id = _queue(db, alice, "export")
    _refused(lambda: _queue(db, alice, "export"))
    db.rollback()

    db.get(Job, job_id).state = "done"
    db.commit()
    _queue(db, alice, "export")


def test_the_paid_cap_counts_only_the_paid_kinds(db, limits):
    limits(quota_active_jobs=0, quota_active_paid_jobs=1)
    alice = _user(db, "alice")
    _queue(db, alice, "transcribe")
    # A render is not a paid kind: it neither counts nor is refused here.
    _queue(db, alice, "export")

    refused = _refused(lambda: _queue(db, alice, "suggest"))
    assert refused.status_code == 429 and "Төлбөртэй 1 ажил" in refused.detail


def test_imports_are_never_capped(db, limits):
    """An import follows an upload the browser already made; refusing it would
    strand the file. The project limits bound uploads instead."""
    limits(quota_active_jobs=1, quota_active_paid_jobs=0)
    alice = _user(db, "alice")
    _queue(db, alice, "export")
    for _ in range(3):
        _queue(db, alice, "import_video")
    _refused(lambda: _queue(db, alice, "export"))


def test_admins_are_exempt(db, limits):
    limits(quota_active_jobs=1, quota_active_paid_jobs=1, quota_monthly_usd=0.0)
    root = _user(db, "root", role="admin")
    for _ in range(3):
        _queue(db, root, "transcribe")


def test_a_click_that_lands_on_the_running_job_is_admitted_at_the_cap(db, limits):
    """The second click on a running transcription must get that one back —
    it adds no load, so refusing it would only hide the job's id."""
    limits(quota_active_jobs=1, quota_active_paid_jobs=1)
    alice = _user(db, "alice")
    row = _project(db, alice)
    key = f"transcribe:{row.id}"
    quotas.admit_job(db, _principal(alice), "transcribe", dedupe_key=key)
    first = queue.enqueue("transcribe", project_id=row.id, dedupe_key=key, db=db)

    quotas.admit_job(db, _principal(alice), "transcribe", dedupe_key=key)
    assert queue.enqueue("transcribe", project_id=row.id, dedupe_key=key, db=db) == first


def test_two_requests_racing_for_the_last_place_cannot_both_win(db, limits, monkeypatch):
    """Both used to count "0 of 1" before either inserted. The per-account
    lock makes the second one count after the first one's job exists."""
    limits(quota_active_jobs=1, quota_active_paid_jobs=0)
    alice = _user(db, "racer")
    rows = [_project(db, alice), _project(db, alice)]
    principal = _principal(alice)
    start = threading.Barrier(2)
    outcomes: list[object] = []

    def attempt(row):
        session = get_session_factory()()
        try:
            start.wait()
            quotas.admit_job(session, principal, "export")
            # Widen the window between the count and the insert: without the
            # lock both requests are inside it at once.
            time.sleep(0.4)
            queue.enqueue("export", project_id=row.id, db=session)
            outcomes.append("queued")
        except HTTPException as e:
            session.rollback()
            outcomes.append(e.status_code)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(row,)) for row in rows]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert sorted(outcomes, key=str) == [429, "queued"]
    db.expire_all()
    active = db.query(Job).filter(Job.kind == "export", Job.state == "queued").count()
    assert active == 1


# ---------------------------------------------------------------------------
# Monthly spend
# ---------------------------------------------------------------------------


def _spent(db, user: User, cost: float | None, requests: int = 1, at: float | None = None):
    db.add(UsageEvent(
        created_at=time.time() if at is None else at, user_id=user.id, provider="openrouter",
        kind="llm", requests=requests, cost_usd=cost,
    ))
    db.commit()


def test_a_paid_job_is_refused_once_the_month_reaches_the_cap(db, limits):
    limits(quota_active_jobs=0, quota_active_paid_jobs=0, quota_monthly_usd=5.0)
    alice = _user(db, "alice")
    _spent(db, alice, 3.0)
    _queue(db, alice, "suggest")

    _spent(db, alice, 2.5)
    _spent(db, alice, None, requests=4)
    refused = _refused(lambda: _queue(db, alice, "suggest"))
    db.rollback()
    assert refused.status_code == 429
    assert "$5.50" in refused.detail and "$5.00" in refused.detail
    # The unpriced part is named, not folded in as zero.
    assert "тодорхойгүй 4 хүсэлт" in refused.detail

    # A render that buys nothing outside is not held back by the bill…
    _queue(db, alice, "export", paid=False)
    # …but one with a Mongolian voice-over buys speech, and is.
    _refused(lambda: _queue(db, alice, "export", paid=True))


def test_last_month_and_unknown_prices_do_not_count_toward_the_cap(db, limits):
    limits(quota_active_jobs=0, quota_active_paid_jobs=0, quota_monthly_usd=1.0)
    alice = _user(db, "alice")
    _spent(db, alice, 50.0, at=quotas.month_start(time.time()) - 60)
    _spent(db, alice, None, requests=30)

    assert quotas.month_spend(db, alice.id) == (0.0, 30)
    _queue(db, alice, "transcribe")


def test_no_cap_is_the_default(db):
    assert get_settings().quota_monthly_usd is None


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def _create(client, auth, size: int = 1024):
    return client.post(
        "/projects", json={"name": "x", "filename": "a.mp4", "size_bytes": size}, headers=auth
    )


def test_project_creation_is_rate_limited_per_hour(client, db, limits):
    limits(quota_projects_per_hour=2, quota_projects_total=0)
    _user(db, "alice")
    auth = _auth(client, "alice")
    assert _create(client, auth).status_code == 201
    assert _create(client, auth).status_code == 201
    refused = _create(client, auth)
    assert refused.status_code == 429 and "цагт дээд тал нь 2" in refused.json()["detail"]

    # An hour later the rate has room again.
    db.query(Project).update({Project.created_at: time.time() - 3700})
    db.commit()
    assert _create(client, auth).status_code == 201


def test_project_total_is_capped(client, db, limits):
    limits(quota_projects_per_hour=0, quota_projects_total=1)
    _user(db, "alice")
    auth = _auth(client, "alice")
    assert _create(client, auth).status_code == 201
    refused = _create(client, auth)
    assert refused.status_code == 409 and "устгаад" in refused.json()["detail"]


def test_admins_create_projects_without_limits(client, db, limits):
    limits(quota_projects_per_hour=1, quota_projects_total=1)
    _user(db, "root", role="admin")
    auth = _auth(client, "root")
    for _ in range(3):
        assert _create(client, auth).status_code == 201


# ---------------------------------------------------------------------------
# Upload size
# ---------------------------------------------------------------------------


@pytest.fixture
def stored(monkeypatch):
    """What R2 holds, and what was deleted from it."""
    from app import r2

    state: dict = {"size": None, "deleted": []}
    monkeypatch.setattr(r2, "object_size", lambda key: state["size"])
    monkeypatch.setattr(r2, "delete", state["deleted"].append)
    return state


def test_an_upload_larger_than_declared_is_deleted_and_refused(client, db, stored):
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, size=1024).json()

    stored["size"] = 50 * 1024**2
    refused = client.post(f"/projects/{made['project_id']}/upload-complete", headers=auth)
    assert refused.status_code == 400
    assert "1024 байт" in refused.json()["detail"]
    assert stored["deleted"] == [made["upload_key"]]
    assert db.query(Job).count() == 0


def test_an_upload_over_the_limit_is_refused_even_without_a_declared_size(client, db, stored):
    """Projects made before the declared size was kept are checked against
    the maximum only."""
    from app.schemas import UPLOAD_MAX_BYTES

    alice = _user(db, "alice")
    row = _project(db, alice)
    row.video_key = "sources/x/source.mp4"
    db.commit()

    stored["size"] = UPLOAD_MAX_BYTES + 1
    refused = client.post(f"/projects/{row.id}/upload-complete", headers=_auth(client, "alice"))
    assert refused.status_code == 413
    assert stored["deleted"] == ["sources/x/source.mp4"]

    stored["size"] = 1234
    accepted = client.post(f"/projects/{row.id}/upload-complete", headers=_auth(client, "alice"))
    assert accepted.status_code == 200


def test_an_upload_of_the_declared_size_is_imported(client, db, stored):
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth, size=2048).json()
    stored["size"] = 2048
    accepted = client.post(f"/projects/{made['project_id']}/upload-complete", headers=auth)
    assert accepted.status_code == 200 and accepted.json()["job_id"]
    assert stored["deleted"] == []


def test_an_upload_that_has_not_landed_is_still_told_so(client, db, stored):
    _user(db, "alice")
    auth = _auth(client, "alice")
    made = _create(client, auth).json()
    response = client.post(f"/projects/{made['project_id']}/upload-complete", headers=auth)
    assert response.status_code == 400 and "ирээгүй" in response.json()["detail"]
    assert stored["deleted"] == []


# ---------------------------------------------------------------------------
# Export dedupe
# ---------------------------------------------------------------------------


def test_the_same_timeline_export_twice_is_one_job(client, db):
    alice = _user(db, "alice")
    row = _project(db, alice)
    auth = _auth(client, "alice")
    assert client.post(
        f"/projects/{row.id}/select", json={"ranges": [[0, 10]]}, headers=auth
    ).status_code == 200

    first = client.post(f"/projects/{row.id}/export", headers=auth)
    again = client.post(f"/projects/{row.id}/export", headers=auth)
    assert first.status_code == again.status_code == 200
    assert first.json()["job_id"] == again.json()["job_id"]

    # A different render is a different job.
    client.post(f"/projects/{row.id}/select", json={"ranges": [[0, 20]]}, headers=auth)
    changed = client.post(f"/projects/{row.id}/export", headers=auth)
    assert changed.json()["job_id"] != first.json()["job_id"]
