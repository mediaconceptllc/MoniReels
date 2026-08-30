"""HTTP surface: auth, ownership isolation, and the pipeline gates.

Every access rule is enforced on the server. These tests exist so a route
added later cannot quietly drop one — the frontend hiding a button is not a
control.

The app is exercised without its lifespan: startup runs Alembic and probes
ffmpeg, neither of which belongs in a unit test. `app.state` is filled in by
hand instead, so the routes see exactly what they see in production.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.dbmodels import Project, User
from app.jobs import queue
from app.security import hash_password
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

pytestmark = requires_db

# Presigning is pure local signing — boto3 needs credentials, never a network
# call — so a fake account exercises the real code path.
os.environ.setdefault("R2_ACCOUNT_ID", "testaccount")
os.environ.setdefault("R2_ACCESS_KEY_ID", "testkey")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "testsecret")
os.environ.setdefault("R2_BUCKET", "testbucket")


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


def _user(db, username: str, role: str = "editor", password: str = "hunter2hunter2") -> User:
    salt, digest = hash_password(password)
    user = User(username=username, pw_salt=salt, pw_hash=digest, role=role)
    db.add(user)
    db.commit()
    return user


def _auth(client: TestClient, username: str, password: str = "hunter2hunter2") -> dict:
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_health_needs_no_token(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_returns_a_usable_token(client, db):
    _user(db, "alice")
    headers = _auth(client, "alice")
    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


def test_wrong_password_is_401(client, db):
    _user(db, "alice")
    response = client.post("/auth/login", json={"username": "alice", "password": "wrongwrongwrong"})
    assert response.status_code == 401


def test_unknown_user_and_wrong_password_are_indistinguishable(client, db):
    """Different responses here are a free account-enumeration oracle."""
    _user(db, "alice")
    unknown = client.post("/auth/login", json={"username": "nobody", "password": "whatever12345"})
    wrong = client.post("/auth/login", json={"username": "alice", "password": "whatever12345"})
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_repeated_failures_are_rate_limited(client, db):
    """PBKDF2 costs ~100ms of CPU per attempt. Unbounded, a few dozen
    concurrent guesses stall every worker — this is availability protection
    before it is guess-slowing."""
    _user(db, "alice")
    codes = [
        client.post("/auth/login", json={"username": "alice", "password": "nope123456"}).status_code
        for _ in range(12)
    ]
    assert 429 in codes, "login was never rate limited"


def test_changing_a_password_invalidates_older_tokens(client, db):
    """Without this, "my account was compromised, I changed my password"
    leaves the attacker's session alive until it expires on its own."""
    _user(db, "alice")
    old = _auth(client, "alice")

    changed = client.post(
        "/auth/password",
        json={"current_password": "hunter2hunter2", "new_password": "brandnewpassword"},
        headers=old,
    )
    assert changed.status_code == 200

    assert client.get("/auth/me", headers=old).status_code == 401
    # The person who made the change is handed a working token, not signed out.
    fresh = {"Authorization": f"Bearer {changed.json()['token']}"}
    assert client.get("/auth/me", headers=fresh).status_code == 200


def test_protected_routes_reject_a_missing_token(client):
    assert client.get("/projects").status_code == 401
    assert client.get("/jobs/queue").status_code == 401


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def test_create_project_returns_a_direct_upload_url(client, db):
    """Media must never pass through the API — a multi-gigabyte video through
    a dyno is a timeout and a doubled bandwidth bill."""
    _user(db, "alice")
    headers = _auth(client, "alice")

    response = client.post(
        "/projects", json={"name": "Ярилцлага", "filename": "episode.mp4", "size_bytes": 1024},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["upload_url"].startswith("https://")
    assert "X-Amz-Signature" in body["upload_url"]
    # Keyed on the project id, which never changes: a rename must not orphan
    # the object already uploaded under the old name.
    assert body["upload_key"] == f"sources/{body['project_id']}/source.mp4"


def test_create_project_rejects_an_unsupported_format(client, db):
    _user(db, "alice")
    headers = _auth(client, "alice")
    response = client.post(
        "/projects", json={"name": "x", "filename": "notes.txt", "size_bytes": 10}, headers=headers
    )
    assert response.status_code == 422


def test_projects_are_isolated_between_accounts(client, db):
    """Someone else's project must be indistinguishable from one that does
    not exist — a 403 would confirm it is real."""
    alice = _user(db, "alice")
    _user(db, "bob")
    row = Project(owner_id=alice.id, name="Alice private", doc={})
    db.add(row)
    db.commit()

    bob_headers = _auth(client, "bob")
    assert client.get(f"/projects/{row.id}", headers=bob_headers).status_code == 404
    assert client.delete(f"/projects/{row.id}", headers=bob_headers).status_code == 404
    assert client.get("/projects", headers=bob_headers).json() == []

    alice_headers = _auth(client, "alice")
    assert client.get(f"/projects/{row.id}", headers=alice_headers).status_code == 200


def test_patch_only_touches_the_fields_that_were_sent(client, db):
    """A whole-document PUT lets a stale tab overwrite work it never loaded."""
    alice = _user(db, "alice")
    row = Project(owner_id=alice.id, name="original", doc={"export": {"crf": 18, "preset": "slow"}})
    db.add(row)
    db.commit()
    headers = _auth(client, "alice")

    response = client.patch(f"/projects/{row.id}", json={"export": {"crf": 25}}, headers=headers)
    assert response.status_code == 200
    export = response.json()["export"]
    assert export["crf"] == 25
    assert export["preset"] == "slow", "an unsent field was overwritten"
    assert response.json()["name"] == "original"


def test_patch_rejects_an_out_of_range_crf(client, db):
    alice = _user(db, "alice")
    row = Project(owner_id=alice.id, name="p", doc={})
    db.add(row)
    db.commit()
    headers = _auth(client, "alice")
    response = client.patch(f"/projects/{row.id}", json={"export": {"crf": 99}}, headers=headers)
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Pipeline gates
# ---------------------------------------------------------------------------


def test_transcribe_requires_a_video(client, db):
    alice = _user(db, "alice")
    row = Project(owner_id=alice.id, name="empty", doc={})
    db.add(row)
    db.commit()
    headers = _auth(client, "alice")

    response = client.post(f"/projects/{row.id}/transcribe", headers=headers)
    assert response.status_code == 400
    assert "video" in response.json()["detail"].lower()


def test_suggest_requires_a_transcript(client, db):
    """Queueing a paid LLM job that cannot possibly succeed is a bill for
    nothing."""
    alice = _user(db, "alice")
    row = Project(
        owner_id=alice.id, name="p",
        doc={"video": {
            "source_key": "sources/x/source.mp4", "duration_sec": 60.0, "width": 1920,
            "height": 1080, "fps": 30.0, "has_audio": True, "codec": "h264", "thumbnail_key": "",
        }},
    )
    db.add(row)
    db.commit()
    headers = _auth(client, "alice")

    response = client.post(f"/projects/{row.id}/suggest", headers=headers)
    assert response.status_code == 400
    assert "transcribe" in response.json()["detail"].lower()


def test_select_rejects_a_range_past_the_end_of_the_video(client, db):
    alice = _user(db, "alice")
    row = Project(
        owner_id=alice.id, name="p",
        doc={"video": {
            "source_key": "sources/x/source.mp4", "duration_sec": 60.0, "width": 1920,
            "height": 1080, "fps": 30.0, "has_audio": True, "codec": "h264", "thumbnail_key": "",
        }},
    )
    db.add(row)
    db.commit()
    headers = _auth(client, "alice")

    def _select(ranges):
        return client.post(f"/projects/{row.id}/select", json={"ranges": ranges}, headers=headers)

    assert _select([[0, 500]]).status_code == 422
    assert _select([[10, 5]]).status_code == 422
    ok = _select([[0, 10], [20, 30]])
    assert ok.status_code == 200 and ok.json()["clips"] == 2


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def test_job_of_another_account_is_not_found(client, db):
    alice = _user(db, "alice")
    _user(db, "bob")
    row = Project(owner_id=alice.id, name="p", doc={})
    db.add(row)
    db.commit()
    job_id = queue.enqueue("export", project_id=row.id)

    assert client.get(f"/jobs/{job_id}", headers=_auth(client, "bob")).status_code == 404
    assert client.get(f"/jobs/{job_id}", headers=_auth(client, "alice")).status_code == 200


def test_queue_status_reports_disk(client, db):
    """"Why did my export fail" is a full disk often enough that the number
    belongs on the page, not only in the server log."""
    _user(db, "alice")
    response = client.get("/jobs/queue", headers=_auth(client, "alice"))
    assert response.status_code == 200
    assert "free_bytes" in response.json()["disk"]


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def test_admin_routes_are_closed_to_editors(client, db):
    _user(db, "alice", role="editor")
    _user(db, "root", role="admin")

    assert client.get("/admin/users", headers=_auth(client, "alice")).status_code == 403
    assert client.get("/admin/users", headers=_auth(client, "root")).status_code == 200


def test_provider_status_reports_an_unconfigured_provider(client, db):
    _user(db, "root", role="admin")
    response = client.get("/admin/providers", headers=_auth(client, "root"))

    assert response.status_code == 200
    body = response.json()
    assert body["duudlaga"]["configured"] is False
    assert body["storage"]["configured"] is True


def test_provider_status_reports_low_credits_instead_of_failing(client, db, monkeypatch):
    """This route exists because `insufficient_credits` otherwise surfaces
    only after a job has claimed a worker and downloaded the video. A
    diagnostics page that 500s on the very condition it was built to report
    would be worse than not having one."""
    import app.stt.duudlaga_client as duudlaga
    from app.config import get_settings

    monkeypatch.setenv("DUUDLAGA_API_KEY", "dk_live_test")
    get_settings.cache_clear()

    async def _no_credits(self):
        raise duudlaga.DuudlagaError("duudlaga.dev дээрх кредит дууссан байна.", status=402,
                                     code="insufficient_credits")

    monkeypatch.setattr(duudlaga.DuudlagaClient, "account_info", _no_credits)

    _user(db, "root", role="admin")
    response = client.get("/admin/providers", headers=_auth(client, "root"))

    assert response.status_code == 200
    assert response.json()["duudlaga"] == {
        "configured": True,
        "ok": False,
        "error": "duudlaga.dev дээрх кредит дууссан байна.",
    }
    get_settings.cache_clear()


def test_provider_status_never_echoes_a_key(client, db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DUUDLAGA_API_KEY", "dk_live_secret_value")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-value")
    get_settings.cache_clear()

    import app.stt.duudlaga_client as duudlaga

    async def _ok(self):
        return {"balance": 10.0}

    monkeypatch.setattr(duudlaga.DuudlagaClient, "account_info", _ok)

    _user(db, "root", role="admin")
    body = client.get("/admin/providers", headers=_auth(client, "root")).text
    assert "dk_live_secret_value" not in body
    assert "sk-or-secret-value" not in body
    assert "testsecret" not in body
    get_settings.cache_clear()


def test_provider_status_is_admin_only(client, db):
    _user(db, "alice", role="editor")
    assert client.get("/admin/providers", headers=_auth(client, "alice")).status_code == 403


def test_an_admin_cannot_disable_their_own_account(client, db):
    """Otherwise nobody is left who can re-enable anyone."""
    root = _user(db, "root", role="admin")
    response = client.post(
        f"/admin/users/{root.id}/active?active=false", headers=_auth(client, "root")
    )
    assert response.status_code == 400
