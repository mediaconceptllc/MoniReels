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
import time

import pytest
from fastapi.testclient import TestClient

from app.dbmodels import Project, User
from app.jobs import queue
from app.models import Segment, Transcript
from app.security import hash_password
from app.store import load, save
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

pytestmark = requires_db

# Presigning is pure local signing — boto3 needs credentials, never a network
# call — so a fake account exercises the real code path.
os.environ.setdefault("R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
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


def test_a_blocked_account_is_not_revealed_without_its_password(client, db, monkeypatch):
    """A wrong password on a blocked account must read exactly like a wrong
    password anywhere else (audit SEC-7). It used to answer 403 "blocked" at
    once — the account confirmed by the answer, and by the ~7 ms it took
    against ~160 ms for a name with no account.

    The timing is asserted as the work done, not the clock: the password is
    hashed for the blocked account just as for any other."""
    from app.api import routes_auth

    blocked = _user(db, "bob")
    blocked.active = False
    db.commit()

    checked = []
    real = routes_auth.verify_password
    monkeypatch.setattr(
        routes_auth, "verify_password", lambda *a: checked.append(a[0]) or real(*a)
    )
    unknown = client.post("/auth/login", json={"username": "nobody", "password": "whatever12345"})
    wrong = client.post("/auth/login", json={"username": "bob", "password": "whatever12345"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert checked == ["whatever12345"]

    # Only the holder of the password learns the account is blocked.
    right = client.post("/auth/login", json={"username": "bob", "password": "hunter2hunter2"})
    assert right.status_code == 403
    assert "хаагдсан" in right.json()["detail"]


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


def test_password_change_guesses_are_rate_limited(client, db):
    """The current password is checked with the same ~100ms PBKDF2 as a
    login; unbounded, one stolen token was a free password oracle that also
    ate the API's CPU (15 wrong guesses in a row, no 429)."""
    from app import ratelimit

    _user(db, "alice")
    auth = _auth(client, "alice")
    codes = [
        client.post(
            "/auth/password",
            json={"current_password": "not-the-password", "new_password": "brandnewpassword"},
            headers=auth,
        ).status_code
        for _ in range(ratelimit.PASSWORD_MAX_ATTEMPTS + 1)
    ]
    assert codes[:-1] == [401] * ratelimit.PASSWORD_MAX_ATTEMPTS
    assert codes[-1] == 429


def test_auth_refusals_are_said_in_mongolian(client, db):
    """The page shows these words as they come: a wrong password used to read
    "Incorrect username or password" in a product whose every other line is
    Mongolian, and a mistyped current password "Current password is
    incorrect"."""
    _user(db, "alice")
    wrong = client.post("/auth/login", json={"username": "alice", "password": "wrongwrongwrong"})
    assert wrong.json()["detail"] == "Нэвтрэх нэр эсвэл нууц үг буруу байна."

    mistyped = client.post(
        "/auth/password",
        json={"current_password": "not-the-password", "new_password": "brandnewpassword"},
        headers=_auth(client, "alice"),
    )
    assert mistyped.status_code == 401
    assert mistyped.json()["detail"] == "Одоогийн нууц үг буруу байна."

    missing = client.get("/auth/me")
    assert missing.json()["detail"] == "Нэвтрэх шаардлагатай."
    expired = client.get("/auth/me", headers={"Authorization": "Bearer not.a.token"})
    assert expired.json()["detail"].startswith("Нэвтрэлт хүчингүй")


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


def test_a_misconfigured_account_id_is_503_not_500(client, db, monkeypatch):
    """Production put the Cloudflare API token into R2_ACCOUNT_ID. boto3 built
    the endpoint from it and raised `Invalid endpoint` from four frames deep:
    the caller got a bare 500, and the token was printed into the deploy log.
    A configuration mistake has to be refused at the door, and named."""
    from app.config import get_settings

    _user(db, "alice")
    headers = _auth(client, "alice")
    monkeypatch.setenv("R2_ACCOUNT_ID", "cfat_" + "x" * 43)
    get_settings.cache_clear()

    response = client.post(
        "/projects", json={"name": "x", "filename": "a.mp4", "size_bytes": 10}, headers=headers
    )

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert "R2_ACCOUNT_ID" in detail
    assert "cfat_" not in detail


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
    # Mongolian, because lib/api.ts shows an API detail to the operator as
    # written — an English one reaches a Mongolian-only user unchanged.
    assert "видео" in response.json()["detail"].lower()


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
    assert "яриаг" in response.json()["detail"].lower()


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
    assert "free_bytes" in response.json()["api_disk"]


def test_queue_status_names_whose_disk_it_measured(client, db):
    """One `disk`, measured on the API, used to answer a question about the
    worker — a different machine, with a different filesystem. The worker's
    figure comes from the worker, with when it was taken, or not at all."""
    from app import worker_report

    _user(db, "alice")
    headers = _auth(client, "alice")
    # No worker has said anything: unknown, not the API's number relabelled.
    assert client.get("/jobs/queue", headers=headers).json()["worker_disk"] is None

    disk = {"free_bytes": 7_000_000_000, "total_bytes": 50_000_000_000, "min_free_bytes": 1024}
    worker_report.publish(db, worker_report.WorkerReport(
        separation=False, model="htdemucs", at=1_700_000_000.0, disk=disk,
    ))
    db.commit()
    body = client.get("/jobs/queue", headers=headers).json()
    assert body["worker_disk"] == {**disk, "measured_at": 1_700_000_000.0}
    assert body["api_disk"]["free_bytes"] != disk["free_bytes"]


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


def test_admin_routes_are_closed_to_editors(client, db):
    _user(db, "alice", role="editor")
    _user(db, "root", role="admin")

    assert client.get("/admin/users", headers=_auth(client, "alice")).status_code == 403
    assert client.get("/admin/users", headers=_auth(client, "root")).status_code == 200


def test_an_admin_reset_takes_the_new_password_in_the_body(client, db):
    alice = _user(db, "alice")
    _user(db, "root", role="admin")
    old = _auth(client, "alice")

    reset = client.post(
        f"/admin/users/{alice.id}/password",
        json={"new_password": "resetbyadmin1"},
        headers=_auth(client, "root"),
    )

    assert reset.status_code == 200
    assert client.get("/auth/me", headers=old).status_code == 401
    _auth(client, "alice", "resetbyadmin1")


def test_an_admin_reset_refuses_a_password_in_the_query_string(client, db):
    """The request line is what access logs record. A reset that still
    worked from `?new_password=` would put the password into every log
    between the browser and the API the moment logging works again."""
    alice = _user(db, "alice")
    _user(db, "root", role="admin")

    reset = client.post(
        f"/admin/users/{alice.id}/password",
        params={"new_password": "leakedviaurl1"},
        headers=_auth(client, "root"),
    )

    assert reset.status_code == 422
    assert "leakedviaurl1" not in reset.text
    # Nothing changed: the old password still signs in.
    _auth(client, "alice")


def test_provider_status_reports_an_unconfigured_provider(client, db):
    _user(db, "root", role="admin")
    response = client.get("/admin/providers", headers=_auth(client, "root"))

    assert response.status_code == 200
    body = response.json()
    assert body["stt"]["configured"] is False
    assert body["stt_providers"] == ["duudlaga", "elevenlabs"]
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
    # Keyed on the SELECTED recogniser, not on a vendor name: probing the
    # one that is not running leaves the one that is running unchecked.
    assert response.json()["stt"] == {
        "provider": "duudlaga",
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


def test_provider_balance_is_shown_and_the_key_beside_it_is_not(client, db, monkeypatch):
    """duudlaga's /me answers with the key itself next to the balance. The
    balance was fetched and thrown away; the raw answer, key included, went
    to the browser."""
    import app.stt.duudlaga_client as duudlaga
    from app.config import get_settings

    monkeypatch.setenv("DUUDLAGA_API_KEY", "dk_live_secret_value")
    get_settings.cache_clear()

    async def _me(self):
        return {
            "key": "dk_live_secret_value", "balance": 12.5, "email": "ops@example.com",
            "limits": {"daily_spend": 5, "key_limit": "dk_live_secret_value"},
            "plan": "pro", "user_id": "u_1", "paid": True,
        }

    monkeypatch.setattr(duudlaga.DuudlagaClient, "account_info", _me)
    _user(db, "root", role="admin")
    response = client.get("/admin/providers", headers=_auth(client, "root"))

    stt = response.json()["stt"]
    assert stt["ok"] is True and "account" not in stt
    assert stt["balance"] == [
        {"name": "balance", "value": 12.5},
        {"name": "limits.daily_spend", "value": 5},
        {"name": "plan", "value": "pro"},
    ]
    assert "dk_live_secret_value" not in response.text
    assert "ops@example.com" not in response.text
    get_settings.cache_clear()


def test_a_balance_summary_takes_only_what_it_recognises():
    from app.api.routes_admin import BALANCE_MAX_FIELDS, balance_summary

    assert balance_summary(None) == [] and balance_summary(["balance"]) == []
    # One level of nesting, scalars only, short text only.
    assert balance_summary({
        "subscription": {"character_count": 10, "character_limit": 100, "deep": {"limit": 1}},
        "tier": "x" * 41, "next_character_count_reset_unix": 1_700_000_000,
    }) == [
        {"name": "subscription.character_count", "value": 10},
        {"name": "subscription.character_limit", "value": 100},
        {"name": "next_character_count_reset_unix", "value": 1_700_000_000},
    ]
    many = {f"limit_{i}": i for i in range(40)}
    assert len(balance_summary(many)) == BALANCE_MAX_FIELDS


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


def test_the_last_active_admin_cannot_be_closed_by_any_path(db):
    """Unreachable over HTTP today — the caller is an active admin and may not
    close themselves — so it is called directly: the invariant must hold for
    the next path that closes an account, not only for this one."""
    from fastapi import HTTPException

    from app.api.routes_admin import set_active
    from app.security import Principal

    root = _user(db, "root", role="admin")
    nobody = Principal(id="not-a-user", username="ghost", role="admin")
    with pytest.raises(HTTPException) as refused:
        set_active(root.id, False, principal=nobody, db=db)
    assert refused.value.status_code == 400
    db.refresh(root)
    assert root.active is True

    # With a second active admin it is an ordinary deactivation.
    _user(db, "root2", role="admin")
    assert set_active(root.id, False, principal=nobody, db=db) == {"active": False}


def test_an_admin_manages_accounts_without_swagger(client, db):
    """Create, reset, close, reopen — the four things an operator does to an
    account, each answered in the language of the page that asks."""
    _user(db, "root", role="admin")
    admin = _auth(client, "root")

    created = client.post(
        "/admin/users", json={"username": "newbie", "password": "firstpass1"}, headers=admin
    )
    assert created.status_code == 201
    assert created.json()["role"] == "editor"
    again = client.post(
        "/admin/users", json={"username": "newbie", "password": "firstpass1"}, headers=admin
    )
    assert again.status_code == 409 and "аль хэдийн бий" in again.json()["detail"]

    user_id = created.json()["id"]
    session = _auth(client, "newbie", "firstpass1")
    closed = client.post(f"/admin/users/{user_id}/active?active=false", headers=admin)
    assert closed.json() == {"active": False}
    # Closing ends the session now, not when the token expires.
    assert client.get("/auth/me", headers=session).status_code == 401
    assert next(u for u in client.get("/admin/users", headers=admin).json()
                if u["id"] == user_id)["active"] is False

    client.post(f"/admin/users/{user_id}/active?active=true", headers=admin)
    assert client.get("/auth/me", headers=session).status_code == 200


def test_the_job_screens_are_admin_only(client, db):
    alice = _user(db, "alice", role="editor")
    headers = _auth(client, "alice")
    # Her own project's job: owning the work does not open the admin screen.
    row = Project(owner_id=alice.id, name="p", doc={})
    db.add(row)
    db.commit()
    job_id = queue.enqueue("export", project_id=row.id)

    assert client.get("/admin/jobs", headers=headers).status_code == 403
    assert client.get("/admin/jobs/stats", headers=headers).status_code == 403
    assert client.post(f"/admin/jobs/{job_id}/retry", headers=headers).status_code == 403
    assert client.post(
        "/admin/users", json={"username": "x1", "password": "firstpass1"}, headers=headers
    ).status_code == 403


def _settled(db, project_id, kind, state, *, created, finished, elapsed=None, attempts=1,
             payload=None):
    """A job row with its times written by hand, as the worker would have."""
    from app.dbmodels import Job

    output = {"elapsed_sec": elapsed} if elapsed is not None else None
    job = Job(
        project_id=project_id, kind=kind, state=state, attempts=attempts,
        created_at=created, updated_at=finished, finished_at=finished,
        result={"payload": payload or {}, "output": output},
    )
    db.add(job)
    db.commit()
    return job


def test_the_jobs_page_names_the_project_and_counts_what_it_does_not_show(client, db):
    _user(db, "root", role="admin")
    owner = _user(db, "alice")
    row = Project(owner_id=owner.id, name="Ярилцлага", doc={})
    db.add(row)
    db.commit()
    now = time.time()
    for i in range(5):
        _settled(db, row.id, "export", "failed", created=now - 100 + i, finished=now - 50 + i)
    _settled(db, row.id, "transcribe", "done", created=now - 10, finished=now - 5, elapsed=3.0)
    _settled(db, None, "export", "done", created=now - 9, finished=now - 4, elapsed=1.0)
    headers = _auth(client, "root")

    page = client.get("/admin/jobs?state=failed&limit=2&offset=1", headers=headers).json()
    assert page["total"] == 5 and page["limit"] == 2 and page["offset"] == 1
    assert len(page["jobs"]) == 2
    first = page["jobs"][0]
    assert first["project_name"] == "Ярилцлага" and first["owner"] == "alice"
    assert first["retryable"] is True and first["state"] == "failed"

    everything = client.get("/admin/jobs?limit=200", headers=headers).json()
    assert everything["total"] == 7
    # Every page of two together is the whole list, once each, in order.
    paged = []
    for offset in range(0, 7, 2):
        paged += client.get(f"/admin/jobs?limit=2&offset={offset}", headers=headers).json()["jobs"]
    assert [j["job_id"] for j in paged] == [j["job_id"] for j in everything["jobs"]]

    done = client.get("/admin/jobs?kind=transcribe", headers=headers).json()
    assert [j["state"] for j in done["jobs"]] == ["done"] and done["jobs"][0]["retryable"] is False
    # A job without a project is listed, not dropped by the join.
    assert any(j["project_name"] is None for j in everything["jobs"])

    assert client.get("/admin/jobs?state=lost", headers=headers).status_code == 422
    assert client.get("/admin/jobs?kind=render", headers=headers).status_code == 422
    assert client.get("/admin/jobs?limit=0", headers=headers).status_code == 422
    assert client.get("/admin/jobs?limit=201", headers=headers).status_code == 422


def test_only_a_job_that_stopped_short_is_retried(client, db):
    """A finished job re-run by hand is a second render, or for the metered
    kinds a second bill for a result that already exists."""
    from app.dbmodels import Job

    _user(db, "root", role="admin")
    owner = _user(db, "alice")
    row = Project(owner_id=owner.id, name="p", doc={})
    db.add(row)
    db.commit()
    headers = _auth(client, "root")
    now = time.time()

    done = _settled(db, row.id, "export", "done", created=now - 9, finished=now - 1, elapsed=2.0)
    assert client.post(f"/admin/jobs/{done.id}/retry", headers=headers).status_code == 400
    waiting = queue.enqueue("suggest", project_id=row.id)
    assert client.post(f"/admin/jobs/{waiting}/retry", headers=headers).status_code == 400
    assert client.post("/admin/jobs/nope/retry", headers=headers).status_code == 404

    canceled = _settled(db, row.id, "export", "canceled", created=now - 9, finished=now - 1,
                        elapsed=4.0, payload={"pick": {"shorts": ["s1"]}})
    canceled.cancel_requested = True
    history = [{"attempt": 1, "outcome": "requeued", "error": "disk full"},
               {"attempt": 2, "outcome": "canceled"}]
    canceled.history = history
    # Left over from an automatic retry the job never reached.
    canceled.available_at = now + 600
    db.commit()
    listed = client.get("/admin/jobs?state=canceled", headers=headers).json()["jobs"]
    assert [j["history"] for j in listed] == [history]
    assert client.post(f"/admin/jobs/{canceled.id}/retry", headers=headers).json() == {"requeued": True}

    db.expire_all()
    again = db.get(Job, canceled.id)
    assert again.state == "queued" and again.attempts == 0 and again.finished_at is None
    # A person asked for it now; and the attempts before this one stay on it.
    assert again.available_at is None and again.history == history
    # Left set, the run this starts would read it and stop on its first tick.
    assert again.cancel_requested is False
    # The handler's input survives; the cancelled run's output does not.
    assert again.result["payload"]["pick"] == {"shorts": ["s1"]}
    assert again.result["payload"]["requeued_at"] >= now
    assert "output" not in again.result


def test_a_retry_never_runs_beside_the_same_work_started_since(client, db):
    """A settled job gave up its dedupe key; the same action started again
    since holds it now. Requeueing the old one would run both."""
    _user(db, "root", role="admin")
    owner = _user(db, "alice")
    row = Project(owner_id=owner.id, name="p", doc={})
    db.add(row)
    db.commit()
    now = time.time()
    failed = _settled(db, row.id, "transcribe", "failed", created=now - 9, finished=now - 1)
    queue.enqueue("transcribe", project_id=row.id, dedupe_key=f"transcribe:{row.id}")

    response = client.post(f"/admin/jobs/{failed.id}/retry", headers=_auth(client, "root"))
    assert response.status_code == 409
    # Another kind on the same project is not a twin.
    other = _settled(db, row.id, "export", "failed", created=now - 9, finished=now - 1)
    assert client.post(f"/admin/jobs/{other.id}/retry", headers=_auth(client, "root")).status_code == 200


def test_job_statistics_are_measured_not_guessed(client, db):
    _user(db, "root", role="admin")
    owner = _user(db, "alice")
    row = Project(owner_id=owner.id, name="p", doc={})
    db.add(row)
    db.commit()
    now = time.time()
    hour = 3600.0
    # Three exports that worked first time: ran 10/20/30s after waiting 5/15/25s.
    for run, waited in ((10.0, 5.0), (20.0, 15.0), (30.0, 25.0)):
        _settled(db, row.id, "export", "done", created=now - hour,
                 finished=now - hour + waited + run, elapsed=run)
    # Worked on its second attempt: its run counts, its "wait" (which holds
    # the first, failed run) does not.
    _settled(db, row.id, "export", "done", created=now - 2 * hour, finished=now - hour,
             elapsed=40.0, attempts=2)
    # Requeued by an admin a day after it was asked for: waited 2s since.
    _settled(db, row.id, "suggest", "done", created=now - 26 * hour, finished=now - 2 * hour + 7,
             elapsed=5.0, payload={"requeued_at": now - 2 * hour})
    _settled(db, row.id, "export", "failed", created=now - hour, finished=now - hour + 3, elapsed=3.0)
    _settled(db, row.id, "export", "canceled", created=now - hour, finished=now - hour + 1)
    # Outside the window: settled eight days ago.
    _settled(db, row.id, "export", "failed", created=now - 9 * 24 * hour, finished=now - 8 * 24 * hour)
    queue.enqueue("export", project_id=row.id)

    stats = client.get("/admin/jobs/stats?days=7", headers=_auth(client, "root")).json()
    assert stats["days"] == 7
    overall = stats["overall"]
    assert (overall["done"], overall["failed"], overall["canceled"]) == (5, 1, 1)
    # A cancel is a decision, not a failure: 5 of 6, not 5 of 7.
    assert overall["success_pct"] == pytest.approx(83.3)
    assert overall["wait_samples"] == 4

    export = next(k for k in stats["kinds"] if k["kind"] == "export")
    assert export["run_p50_sec"] == 25.0  # 10, 20, 30 and the second-attempt 40
    assert export["wait_p50_sec"] == 15.0
    assert export["wait_p95_sec"] == pytest.approx(24.0)
    suggest = next(k for k in stats["kinds"] if k["kind"] == "suggest")
    assert suggest["wait_p50_sec"] == pytest.approx(2.0)
    assert [k["kind"] for k in stats["kinds"]] == ["suggest", "export"]

    assert stats["queue"]["waiting"] == 1
    assert stats["queue"]["oldest_waiting_sec"] is not None


def test_job_statistics_say_when_there_is_nothing_to_measure(client, db):
    """A rate from no samples is not a rate, and a window longer than the
    rows are kept would quietly report a shorter one."""
    from app.config import get_settings

    _user(db, "root", role="admin")
    stats = client.get("/admin/jobs/stats?days=365", headers=_auth(client, "root")).json()
    assert stats["days"] == get_settings().job_keep_days
    assert stats["overall"]["success_pct"] is None
    assert stats["overall"]["run_p50_sec"] is None
    assert stats["kinds"] == []
    assert stats["queue"]["oldest_waiting_sec"] is None


# ---------------------------------------------------------------------------
# Provider settings
# ---------------------------------------------------------------------------


def test_provider_settings_never_return_the_stored_value(client, db):
    """The point of storing a key server-side is that it stops being
    readable. A response that echoes it back turns the settings page into a
    way for anyone who reaches it to walk off with the credential."""
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    secret = "sk-or-v1-0123456789abcdefSECRET9f3a"

    written = client.put("/admin/settings", json={"openrouter_api_key": secret}, headers=headers)
    assert written.status_code == 200, written.text
    assert written.json()["changed"] == ["openrouter_api_key"]
    assert secret not in written.text

    read = client.get("/admin/settings", headers=headers)
    assert read.status_code == 200
    assert secret not in read.text
    field = read.json()["openrouter_api_key"]
    assert field["source"] == "db"
    assert field["set"] is True
    assert field["hint"].endswith("9f3a")


def test_a_stored_key_is_what_the_next_job_uses(client, db):
    """The worker is a different process; it cannot be handed the new value.
    Both sides read the same table, so the write has to be visible there."""
    from app import provider_settings

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put("/admin/settings", json={"duudlaga_api_key": "dd-live-abcd"}, headers=headers)

    assert provider_settings.effective(db).duudlaga_api_key == "dd-live-abcd"


def test_clearing_a_key_falls_back_to_the_environment(client, db, monkeypatch):
    """Without a way back, one mistyped key is permanent — the environment
    value it shadows could never be reached again."""
    from app import provider_settings
    from app.config import get_settings

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-the-environment")
    get_settings.cache_clear()

    client.put("/admin/settings", json={"openrouter_api_key": "from-the-page"}, headers=headers)
    assert provider_settings.effective(db).openrouter_api_key == "from-the-page"

    client.put("/admin/settings", json={"openrouter_api_key": ""}, headers=headers)
    assert provider_settings.effective(db).openrouter_api_key == "from-the-environment"
    assert client.get("/admin/settings", headers=headers).json()["openrouter_api_key"]["source"] == "env"


def test_a_field_that_was_not_sent_is_left_alone(client, db):
    """The page saves one form. Sending only what changed must not blank the
    keys the operator did not touch."""
    from app import provider_settings

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put(
        "/admin/settings",
        json={"openrouter_api_key": "keep-me", "duudlaga_api_key": "keep-me-too"},
        headers=headers,
    )

    client.put("/admin/settings", json={"openrouter_model": "openai/gpt-5"}, headers=headers)

    effective = provider_settings.effective(db)
    assert effective.openrouter_api_key == "keep-me"
    assert effective.duudlaga_api_key == "keep-me-too"
    assert effective.openrouter_model == "openai/gpt-5"


def test_the_endpoint_cannot_redirect_where_the_key_is_sent(client, db):
    """A key plus the freedom to choose the host it goes to is exfiltration:
    point the base URL at your own server and collect it on the first call.
    Only the closed set of fields is writable."""
    from app import provider_settings

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    before = provider_settings.effective(db).openrouter_base_url

    client.put(
        "/admin/settings",
        json={"openrouter_api_key": "k", "openrouter_base_url": "https://attacker.example/v1"},
        headers=headers,
    )

    assert provider_settings.effective(db).openrouter_base_url == before


def test_provider_settings_are_admin_only(client, db):
    _user(db, "alice")
    headers = _auth(client, "alice")
    assert client.get("/admin/settings", headers=headers).status_code == 403
    assert client.put("/admin/settings", json={"openrouter_api_key": "x"}, headers=headers).status_code == 403


def test_a_rejected_value_is_never_read_back(client, db):
    """Pydantic puts the rejected value in `input` and FastAPI hands it back
    by default: an over-long password, or an API key pasted into the settings
    form, would be echoed into the response body and anything that logs it."""
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    secret = "SECRET-VALUE-" * 60

    too_long_password = client.post(
        "/auth/login", json={"username": "root", "password": secret}
    )
    assert too_long_password.status_code == 422
    assert secret[:20] not in too_long_password.text
    assert "password" in too_long_password.text

    too_long_key = client.put(
        "/admin/settings", json={"openrouter_api_key": secret}, headers=headers
    )
    assert too_long_key.status_code == 422
    assert secret[:20] not in too_long_key.text


# ---------------------------------------------------------------------------
# Brand logo. Global, admin-only, and the image never passes through the API.
# ---------------------------------------------------------------------------


def test_the_brand_logo_is_admin_only(client, db):
    _user(db, "alice")
    _user(db, "root", role="admin")
    editor, admin = _auth(client, "alice"), _auth(client, "root")

    assert client.get("/admin/brand", headers=editor).status_code == 403
    assert client.put("/admin/brand/logo", headers=editor, json={"key": None}).status_code == 403
    assert client.get("/admin/brand", headers=admin).status_code == 200


def test_no_logo_is_set_to_begin_with(client, db):
    _user(db, "root", role="admin")
    body = client.get("/admin/brand", headers=_auth(client, "root")).json()
    assert body["logo"] is None
    assert body["intro"] is None
    assert body["outro"] is None


def test_the_upload_url_is_presigned_and_the_image_never_posts_here(client, db):
    _user(db, "root", role="admin")
    response = client.post(
        "/admin/brand/logo/upload-url",
        headers=_auth(client, "root"),
        json={"content_type": "image/png"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["key"].startswith("brand/logo-")
    assert body["key"].endswith(".png")
    assert "X-Amz-Signature" in body["url"]


def test_a_format_ffmpeg_cannot_read_is_refused_at_upload_time(client, db):
    # Refused here rather than at render time, where it would fail an export
    # the logo is only decorating.
    _user(db, "root", role="admin")
    response = client.post(
        "/admin/brand/logo/upload-url",
        headers=_auth(client, "root"),
        json={"content_type": "image/svg+xml"},
    )
    assert response.status_code == 400


def test_a_logo_key_outside_the_brand_prefix_is_refused(client, db):
    # Otherwise this route adopts any object in the bucket, including another
    # project's source video, and every export would try to draw it.
    _user(db, "root", role="admin")
    response = client.put(
        "/admin/brand/logo",
        headers=_auth(client, "root"),
        json={"key": "sources/someone-elses-video.mp4"},
    )
    assert response.status_code == 400


def test_clearing_the_logo_needs_no_storage_round_trip(client, db):
    _user(db, "root", role="admin")
    response = client.put("/admin/brand/logo", headers=_auth(client, "root"), json={"key": None})
    assert response.status_code == 200
    assert response.json()["logo"] is None


def test_export_settings_carry_the_per_project_logo_choice(client, db):
    # The image is global; whether to use it is not.
    _user(db, "alice")
    headers = _auth(client, "alice")
    created = client.post(
        "/projects",
        headers=headers,
        json={"name": "Тест", "filename": "a.mp4", "size_bytes": 1024},
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]
    body = client.get(f"/projects/{project_id}", headers=headers).json()

    logo = body["export"]["logo"]
    assert logo["enabled"] is False  # off until someone asks for it
    assert logo["position"] == "top-right"  # clear of the bottom subtitles


def test_every_brand_slot_has_its_own_upload_url(client, db):
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    for asset, content_type, ext in [
        ("logo", "image/png", ".png"),
        ("intro", "video/mp4", ".mp4"),
        ("outro", "video/mp4", ".mp4"),
    ]:
        response = client.post(
            f"/admin/brand/{asset}/upload-url", headers=headers, json={"content_type": content_type}
        )
        assert response.status_code == 200, response.text
        assert response.json()["key"] == f"brand/{asset}-" + response.json()["key"].split("-", 1)[1]
        assert response.json()["key"].endswith(ext)


def test_a_video_cannot_be_uploaded_as_the_logo(client, db):
    # Nor an image as the intro: each slot takes what ffmpeg can use for it.
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    assert (
        client.post(
            "/admin/brand/logo/upload-url", headers=headers, json={"content_type": "video/mp4"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/admin/brand/intro/upload-url", headers=headers, json={"content_type": "image/png"}
        ).status_code
        == 400
    )


def test_an_intro_key_cannot_be_adopted_as_the_outro(client, db):
    # Otherwise one upload silently fills two slots and the export plays the
    # title card at both ends.
    _user(db, "root", role="admin")
    response = client.put(
        "/admin/brand/outro", headers=_auth(client, "root"), json={"key": "brand/intro-123.mp4"}
    )
    assert response.status_code == 400


def test_an_unknown_brand_slot_is_a_404(client, db):
    _user(db, "root", role="admin")
    response = client.put(
        "/admin/brand/watermark", headers=_auth(client, "root"), json={"key": None}
    )
    assert response.status_code == 404


def test_intro_and_outro_are_off_until_a_project_asks(client, db):
    _user(db, "alice")
    headers = _auth(client, "alice")
    created = client.post(
        "/projects",
        headers=headers,
        json={"name": "Тест", "filename": "a.mp4", "size_bytes": 1024},
    )
    body = client.get(f"/projects/{created.json()['project_id']}", headers=headers).json()

    assert body["export"]["use_intro"] is False
    assert body["export"]["use_outro"] is False


# ---------------------------------------------------------------------------
# Providers. A paid job that cannot succeed must be refused before it queues,
# and a key stored for a feature nothing reads must not look ready.
# ---------------------------------------------------------------------------


def _project_with_video(db, owner) -> Project:
    row = Project(
        owner_id=owner.id, name="p",
        doc={"video": {
            "source_key": "sources/x/source.mp4", "duration_sec": 60.0, "width": 1920,
            "height": 1080, "fps": 30.0, "has_audio": True, "codec": "h264", "thumbnail_key": "",
        }},
    )
    db.add(row)
    db.commit()
    return row


def test_transcribe_is_refused_when_no_stt_key_is_set(client, db, monkeypatch):
    # Queued, this claims a worker slot and downloads the video before dying.
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(update={"duudlaga_api_key": ""})
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)

    alice = _user(db, "alice")
    row = _project_with_video(db, alice)
    response = client.post(f"/projects/{row.id}/transcribe", headers=_auth(client, "alice"))

    assert response.status_code == 503
    assert "duudlaga" in response.json()["detail"].lower()


def test_suggest_is_refused_when_no_llm_key_is_set(client, db, monkeypatch):
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(update={"openrouter_api_key": ""})
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)

    alice = _user(db, "alice")
    row = _project_with_video(db, alice)
    row.doc = {**row.doc, "transcript": {
        "language": "mn", "full_text": "яриа",
        "segments": [{"id": "s", "start": 0.0, "end": 2.0, "text": "яриа", "words": []}],
    }}
    db.commit()

    response = client.post(f"/projects/{row.id}/suggest", headers=_auth(client, "alice"))

    assert response.status_code == 503
    assert "openrouter" in response.json()["detail"].lower()


def test_readiness_is_visible_to_someone_who_cannot_change_it(client, db):
    # The warning has to appear on the page with the paid button, and that
    # page is not admin-only.
    _user(db, "alice")
    response = client.get("/projects/providers/status", headers=_auth(client, "alice"))

    assert response.status_code == 200
    names = {c["name"] for c in response.json()["capabilities"]}
    assert names == {"stt", "llm", "tts"}


def test_readiness_leaks_no_keys_or_balances(client, db):
    _user(db, "alice")
    body = client.get("/projects/providers/status", headers=_auth(client, "alice")).json()

    for capability in body["capabilities"]:
        assert set(capability) == {"name", "label", "ready", "blocked"}


def test_a_key_without_a_chosen_voice_is_not_a_voice_over():
    """A key alone has nothing to send. Reported ready, the first export to
    find that out would be the first one somebody asked a voice for."""
    from app import providers
    from app.config import get_settings

    settings = get_settings().model_copy(
        update={"elevenlabs_api_key": "sk_live_whatever", "elevenlabs_tts_voice_id": ""}
    )
    tts = next(c for c in providers.describe(settings) if c.name == providers.TTS)

    assert tts.implemented is True
    assert tts.ready is False
    assert "хоолой" in tts.blocked


def test_a_key_and_a_voice_are_a_voice_over():
    from app import providers
    from app.config import get_settings

    settings = get_settings().model_copy(
        update={"elevenlabs_api_key": "sk_live_whatever", "elevenlabs_tts_voice_id": "Voice123"}
    )
    tts = next(c for c in providers.describe(settings) if c.name == providers.TTS)
    assert tts.ready is True and tts.blocked is None
    assert "eleven_v3" in tts.provider


def test_no_key_says_so_before_it_asks_for_a_voice():
    from app import providers
    from app.config import get_settings

    settings = get_settings().model_copy(
        update={"elevenlabs_api_key": "", "elevenlabs_tts_voice_id": "Voice123"}
    )
    tts = next(c for c in providers.describe(settings) if c.name == providers.TTS)
    assert tts.ready is False and "түлхүүр" in tts.blocked


@pytest.mark.parametrize("voice_id", ["../user", "a/b", "a b", "x" * 65])
def test_a_voice_id_that_would_change_the_request_path_is_refused(client, db, voice_id):
    """It goes into ElevenLabs' URL path; stored, `../user` would send the
    account's key to a different endpoint on every export."""
    _user(db, "voiceadmin", role="admin")
    response = client.put(
        "/admin/settings", json={"elevenlabs_tts_voice_id": voice_id},
        headers=_auth(client, "voiceadmin"),
    )
    assert response.status_code == 422


def test_a_voice_is_chosen_and_read_back_in_full(client, db):
    """Not a secret: the admin has to be able to see which voice is set."""
    _user(db, "voiceadmin2", role="admin")
    auth = _auth(client, "voiceadmin2")
    saved = client.put("/admin/settings", json={"elevenlabs_tts_voice_id": "Voice123"}, headers=auth)
    assert saved.status_code == 200
    assert saved.json()["settings"]["elevenlabs_tts_voice_id"]["hint"] == "Voice123"
    assert saved.json()["settings"]["elevenlabs_tts_model"]["hint"] == "eleven_v3"


def test_a_font_the_image_lacks_is_refused_on_save(client, db):
    # libass would substitute silently, so the setting would look applied and
    # the export would use something else.
    alice = _user(db, "alice")
    row = _project_with_video(db, alice)
    response = client.patch(
        f"/projects/{row.id}",
        headers=_auth(client, "alice"),
        json={"subtitle_style": {"font_family": "Comic Sans MS"}},
    )
    assert response.status_code == 422


def test_an_installed_font_saves(client, db):
    from app.subtitle import fonts

    alice = _user(db, "alice")
    row = _project_with_video(db, alice)
    response = client.patch(
        f"/projects/{row.id}",
        headers=_auth(client, "alice"),
        json={"subtitle_style": {"font_family": fonts.available()[0]}},
    )
    assert response.status_code == 200, response.text


def test_the_font_list_only_offers_what_is_installed(client, db):
    from app.subtitle import fonts

    _user(db, "alice")
    body = client.get("/projects/subtitle/fonts", headers=_auth(client, "alice")).json()

    assert body["families"] == list(fonts.available())
    assert body["default"] == fonts.DEFAULT_FAMILY
    # A single-segment path here is swallowed by /projects/{project_id}.
    assert client.get("/projects/subtitle-fonts", headers=_auth(client, "alice")).status_code == 404


# ---------------------------------------------------------------------------
# Subtitle templates. A studio-wide house style, applied by anyone, changed
# by admin — the same line the brand assets draw.
# ---------------------------------------------------------------------------


def _style(**overrides) -> dict:
    from app.subtitle import fonts

    return {
        "enabled": True,
        "font_family": fonts.available()[0],
        "font_size": 48,
        "primary_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 2.0,
        "shadow": 0.0,
        "position": "bottom",
        "margin_v": 40,
        **overrides,
    }


def test_anyone_signed_in_can_read_the_templates(client, db):
    # A house style nobody can apply is not a house style.
    _user(db, "alice")
    response = client.get("/projects/subtitle/templates", headers=_auth(client, "alice"))
    assert response.status_code == 200
    assert response.json()["templates"] == []


def test_only_an_admin_can_save_or_delete_one(client, db):
    _user(db, "alice")
    _user(db, "root", role="admin")
    body = {"name": "Үндсэн", "style": _style()}

    assert client.post(
        "/projects/subtitle/templates", headers=_auth(client, "alice"), json=body
    ).status_code == 403

    created = client.post("/projects/subtitle/templates", headers=_auth(client, "root"), json=body)
    assert created.status_code == 201, created.text

    template_id = created.json()["id"]
    assert client.delete(
        f"/projects/subtitle/templates/{template_id}", headers=_auth(client, "alice")
    ).status_code == 403
    assert client.delete(
        f"/projects/subtitle/templates/{template_id}", headers=_auth(client, "root")
    ).status_code == 200


def test_a_template_cannot_carry_a_font_the_image_lacks(client, db):
    # A saved house style that silently renders in something else is worse
    # than no template at all.
    _user(db, "root", role="admin")
    response = client.post(
        "/projects/subtitle/templates",
        headers=_auth(client, "root"),
        json={"name": "Муу", "style": _style(font_family="Comic Sans MS")},
    )
    assert response.status_code == 422


def test_two_templates_cannot_share_a_name(client, db):
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    body = {"name": "Үндсэн", "style": _style()}

    assert client.post("/projects/subtitle/templates", headers=headers, json=body).status_code == 201
    assert client.post("/projects/subtitle/templates", headers=headers, json=body).status_code == 409


def test_a_partial_style_is_refused(client, db):
    # SubtitleStyleIn is a PATCH where everything is optional; a template
    # with half its fields missing is not a house style.
    _user(db, "root", role="admin")
    response = client.post(
        "/projects/subtitle/templates",
        headers=_auth(client, "root"),
        json={"name": "Дутуу", "style": {"font_size": 48}},
    )
    assert response.status_code == 422


def test_deleting_a_template_does_not_restyle_a_project(client, db):
    # A template is a starting point that was COPIED in, not a live link.
    from app.subtitle import fonts

    _user(db, "root", role="admin")
    alice = _user(db, "alice")
    admin = _auth(client, "root")

    created = client.post(
        "/projects/subtitle/templates",
        headers=admin,
        json={"name": "Үндсэн", "style": _style(font_size=72)},
    )
    assert created.status_code == 201, created.text
    created = created.json()

    row = _project_with_video(db, alice)
    client.patch(
        f"/projects/{row.id}",
        headers=_auth(client, "alice"),
        json={"subtitle_style": {"font_size": 72, "font_family": fonts.available()[0]}},
    )
    client.delete(f"/projects/subtitle/templates/{created['id']}", headers=admin)

    body = client.get(f"/projects/{row.id}", headers=_auth(client, "alice")).json()
    assert body["subtitle_style"]["font_size"] == 72


def test_the_selected_recogniser_is_what_gets_probed(client, db, monkeypatch):
    # Two keys can be set at once — elevenlabs_api_key is also the TTS slot —
    # so reading "any key" would report ready because a different vendor is
    # configured, and would probe the recogniser that is not running.
    from app.config import get_settings

    monkeypatch.setenv("STT_PROVIDER", "elevenlabs")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_eleven_test")
    monkeypatch.setenv("DUUDLAGA_API_KEY", "")
    get_settings.cache_clear()

    import app.stt.elevenlabs_client as eleven

    async def _ok(self):
        return {"tier": "creator"}

    monkeypatch.setattr(eleven.ElevenLabsSttClient, "account_info", _ok)

    _user(db, "root", role="admin")
    body = client.get("/admin/providers", headers=_auth(client, "root")).json()

    assert body["stt"]["provider"] == "elevenlabs"
    assert body["stt"]["configured"] is True
    assert body["stt"]["ok"] is True
    get_settings.cache_clear()


def test_an_unknown_recogniser_is_refused_at_save(client, db):
    # Stored, a typo would look saved and fail a job an hour later.
    _user(db, "root", role="admin")
    response = client.put(
        "/admin/settings", headers=_auth(client, "root"), json={"stt_provider": "whisper"}
    )
    assert response.status_code == 422


def test_the_recogniser_can_be_switched(client, db):
    _user(db, "root", role="admin")
    response = client.put(
        "/admin/settings", headers=_auth(client, "root"), json={"stt_provider": "elevenlabs"}
    )
    assert response.status_code == 200
    assert "stt_provider" in response.json()["changed"]


# --- a model name nothing checked ----------------------------------------
#
# `deepseek/deepseek-v4-flash-latest` was typed into the settings page, saved
# cleanly, and turned three suggest jobs into 400s. `stt_provider` in the same
# form has always been a closed set for exactly this reason; the model beside
# it was free text.


def _set_model(client, headers, model: str, checker):
    from app.ai import openrouter_client

    original = openrouter_client.check_model
    openrouter_client.check_model = checker
    try:
        return client.put("/admin/settings", json={"openrouter_model": model}, headers=headers)
    finally:
        openrouter_client.check_model = original


def _rejects(*suggestions):
    from app.ai import openrouter_client

    async def check(config, model, http_client=None):
        return openrouter_client.ModelCheck(known=False, suggestions=tuple(suggestions))

    return check


def test_a_model_openrouter_does_not_serve_is_refused_at_the_box_it_was_typed_in(client, db):
    from app import provider_settings

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put("/admin/settings", json={"openrouter_api_key": "sk-live"}, headers=headers)
    before = provider_settings.effective(db).openrouter_model

    response = _set_model(client, headers, "deepseek/deepseek-v4-flash-latest", _rejects())

    assert response.status_code == 400
    assert "deepseek/deepseek-v4-flash-latest" in response.json()["detail"]
    assert provider_settings.effective(db).openrouter_model == before, "the bad name was stored anyway"


def test_a_near_miss_is_answered_with_what_it_probably_meant(client, db):
    """Turning "wrong" into "did you mean" is the difference between one
    correction and a hunt through a third party's model list."""
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put("/admin/settings", json={"openrouter_api_key": "sk-live"}, headers=headers)

    response = _set_model(
        client, headers, "deepseek/deepseek-v4-flash-latest", _rejects("deepseek/deepseek-v4-flash")
    )

    assert response.status_code == 400
    assert "deepseek/deepseek-v4-flash" in response.json()["detail"]


def test_the_refusals_a_person_reads_are_in_mongolian(client, db):
    """Every one of these was English in a Mongolian interface (audit
    ARCH-8): the page shows a refusal's `detail` as it comes."""
    import re

    import pytest as _pytest

    from app import brand, provider_settings

    cyrillic = re.compile("[\u0400-\u04FF]")
    _user(db, "plain")
    _user(db, "root", role="admin")

    forbidden = client.get("/admin/users", headers=_auth(client, "plain"))
    assert forbidden.status_code == 403
    assert cyrillic.search(forbidden.json()["detail"]), forbidden.json()

    headers = _auth(client, "root")
    client.put("/admin/settings", json={"openrouter_api_key": "sk-live"}, headers=headers)
    refused = _set_model(client, headers, "deepseek/deepseek-v4-flash-latest", _rejects("deepseek/x"))
    assert cyrillic.search(refused.json()["detail"]), refused.json()
    assert "Did you mean" not in refused.json()["detail"]

    for call in (
        lambda: brand.new_key("banner", "image/png"),
        lambda: brand.new_key("logo", "application/pdf"),
        lambda: provider_settings.apply(db, {"jwt_secret": "x"}, actor="root", ip=""),
    ):
        with _pytest.raises(ValueError) as caught:
            call()
        assert cyrillic.search(str(caught.value)), str(caught.value)


def test_an_unreadable_catalogue_does_not_stop_the_save(client, db):
    """Refusing here would make an OpenRouter outage a settings lockout —
    including the outage an operator is trying to configure their way out of.
    The save goes through and says it could not be checked."""
    from app import provider_settings
    from app.ai import openrouter_client

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put("/admin/settings", json={"openrouter_api_key": "sk-live"}, headers=headers)

    async def unavailable(config, model, http_client=None):
        raise openrouter_client.ModelCatalogUnavailable("connection refused")

    response = _set_model(client, headers, "openai/gpt-5", unavailable)

    assert response.status_code == 200
    assert response.json()["model_check"] == {"verified": False, "reason": "connection refused"}
    assert provider_settings.effective(db).openrouter_model == "openai/gpt-5"


def test_a_verified_model_says_so(client, db):
    from app.ai import openrouter_client

    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put("/admin/settings", json={"openrouter_api_key": "sk-live"}, headers=headers)

    async def known(config, model, http_client=None):
        return openrouter_client.ModelCheck(known=True)

    response = _set_model(client, headers, "openai/gpt-5", known)

    assert response.json()["model_check"] == {"verified": True}


def test_no_key_means_nothing_to_check_against(client, db):
    """With no key the app cannot call OpenRouter for a job either, so the
    check protects nothing — and asking anyway buys a 401 after a timeout the
    operator waits through."""
    _user(db, "root", role="admin")
    headers = _auth(client, "root")

    async def must_not_run(config, model, http_client=None):
        raise AssertionError("asked the catalogue with no key to ask it with")

    response = _set_model(client, headers, "openai/gpt-5", must_not_run)

    assert response.status_code == 200
    assert response.json()["model_check"]["verified"] is False


def test_clearing_the_model_is_not_a_model_to_check(client, db):
    """Empty drops the override and falls back to the environment, which this
    route has no business vetoing."""
    _user(db, "root", role="admin")
    headers = _auth(client, "root")
    client.put("/admin/settings", json={"openrouter_api_key": "sk-live"}, headers=headers)

    async def must_not_run(config, model, http_client=None):
        raise AssertionError("checked an empty model name")

    response = _set_model(client, headers, "", must_not_run)

    assert response.status_code == 200
    assert response.json()["model_check"] is None


# ---------------------------------------------------------------------------
# The list carries each project's thumbnail
# ---------------------------------------------------------------------------


def test_the_list_signs_a_thumbnail_for_each_project(client, db):
    """Import has always made one and the detail page has always signed one;
    the list never asked, so a wall of VIDEO projects read as a wall of text.

    Signed per request, like every other media URL here — never stored, so a
    URL cannot outlive its own expiry inside a page left open."""
    user = _user(db, "thumbowner")
    row = Project(
        owner_id=user.id, name="with a thumbnail", doc={},
        video_key="sources/x/source.mp4", thumbnail_key="thumbnails/x/thumb.jpg",
    )
    db.add(row)
    db.commit()

    listed = client.get("/projects", headers=_auth(client, "thumbowner"))
    assert listed.status_code == 200, listed.text
    card = next(p for p in listed.json() if p["id"] == row.id)
    assert card["thumbnail_url"], "the list must sign the thumbnail it already has"
    assert "thumbnails/x/thumb.jpg" in card["thumbnail_url"]


def test_a_project_with_no_thumbnail_yet_says_null(client, db):
    """Null, not a missing key and not a broken URL: the import is still
    running, and the card draws its own frame rather than going ragged."""
    user = _user(db, "nothumb")
    db.add(Project(owner_id=user.id, name="still importing", doc={}))
    db.commit()

    listed = client.get("/projects", headers=_auth(client, "nothumb"))
    card = listed.json()[0]
    assert "thumbnail_url" in card
    assert card["thumbnail_url"] is None


def test_the_thumbnail_url_is_never_stored(client, db):
    """A signed URL expires; a stored one would keep being handed out after it
    had. Two reads of the same row must produce two fresh signatures."""
    user = _user(db, "freshsig")
    row = Project(
        owner_id=user.id, name="p", doc={},
        video_key="sources/y/source.mp4", thumbnail_key="thumbnails/y/thumb.jpg",
    )
    db.add(row)
    db.commit()
    auth = _auth(client, "freshsig")

    first = client.get("/projects", headers=auth).json()[0]["thumbnail_url"]
    db.expire_all()
    assert db.get(Project, row.id).doc == {}, "the signature must not land in the document"
    assert first and "X-Amz-Signature" in first


def test_storage_off_yields_null_rather_than_the_raw_key(client, db, monkeypatch):
    """The case the null branch really guards.

    A project can hold a thumbnail key while storage is unconfigured — the row
    outlives the setting. Handing the KEY back in a field the browser puts in
    an `src` would render a broken image AND publish the object's path, and
    both are worse than the frame the card draws for itself.
    """
    from app import r2

    monkeypatch.setattr(r2, "enabled", lambda: False)
    user = _user(db, "nostorage")
    db.add(Project(
        owner_id=user.id, name="p", doc={},
        video_key="sources/z/source.mp4", thumbnail_key="thumbnails/z/thumb.jpg",
    ))
    db.commit()

    card = client.get("/projects", headers=_auth(client, "nostorage")).json()[0]
    assert card["thumbnail_url"] is None
    assert "thumbnails/z" not in str(card)


# ---------------------------------------------------------------------------
# What a project has cost, and what the next paid run will cost
# ---------------------------------------------------------------------------

def _priced_suggest(db, project_id: str, *, cost: float, characters: int, jid: str) -> None:
    from app.dbmodels import Job

    db.add(Job(
        id=jid, project_id=project_id, kind="suggest", state="done",
        progress=1.0, stage="done",
        result={"payload": {}, "output": {
            "shorts": 3, "youtube": 2, "characters": characters, "elapsed_sec": 11.0,
            "llm": {"calls": 2, "prompt_tokens": 9000, "completion_tokens": 800,
                    "cost_usd": cost, "models": ["example/model"]},
        }},
        created_at=time.time(), finished_at=time.time(),
    ))
    db.commit()


def test_the_project_reports_what_it_has_spent(client, db):
    """The figure was metered from the first day and reachable only through
    the database. The page that asks for a paid run is the page that should
    be able to say what the last one cost."""
    user = _user(db, "spendreader")
    row = Project(owner_id=user.id, name="p", doc={})
    db.add(row)
    db.commit()
    _priced_suggest(db, row.id, cost=0.0312, characters=9000, jid="apispend1")

    data = client.get(f"/projects/{row.id}", headers=_auth(client, "spendreader")).json()
    assert data["spend"]["spent_usd"] == pytest.approx(0.0312)
    assert data["spend"]["priced_jobs"] == 1
    # The jobs behind the total are pruned, so the window travels with it.
    assert data["spend"]["keep_days"] > 0


def test_the_estimate_is_scaled_to_this_projects_transcript(client, db):
    user = _user(db, "spendscaler")
    row = Project(owner_id=user.id, name="p", doc={})
    db.add(row)
    db.commit()
    project = load(db, row.id)
    project.transcript = Transcript(
        language="mn",
        segments=[Segment(id="s1", start=0.0, end=4.0, text="ү" * 18_000)],
        full_text="ү" * 18_000,
    )
    save(db, project)
    _priced_suggest(db, row.id, cost=0.03, characters=9000, jid="apispend2")

    spend = client.get(f"/projects/{row.id}", headers=_auth(client, "spendscaler")).json()["spend"]
    # Twice the transcript of the measured run, so twice the bill.
    assert spend["suggest_estimate_usd"] == pytest.approx(0.06)
    assert spend["suggest_samples"] == 1


def test_a_project_with_nothing_measured_offers_no_estimate(client, db):
    """Null rather than 0.00: a zero next to a paid button is a promise, and
    the very first run is when the question is actually being asked."""
    user = _user(db, "spendfirst")
    row = Project(owner_id=user.id, name="p", doc={})
    db.add(row)
    db.commit()

    spend = client.get(f"/projects/{row.id}", headers=_auth(client, "spendfirst")).json()["spend"]
    assert spend["suggest_estimate_usd"] is None
    assert spend["spent_usd"] == 0.0
    # Speech-to-text is billed per minute by the recogniser and nothing here
    # counts it, which the payload says rather than the page assuming.
    assert spend["stt_measured"] is False


def test_another_owners_runs_never_price_this_one(client, db):
    """The rate is owner-scoped. Reading it across accounts would leak how
    much somebody else's work costs — and, on a shared instance, roughly how
    much of it there is."""
    mine = _user(db, "spendmine")
    theirs = _user(db, "spendtheirs")
    ours = Project(owner_id=mine.id, name="mine", doc={})
    hers = Project(owner_id=theirs.id, name="theirs", doc={})
    db.add_all([ours, hers])
    db.commit()
    project = load(db, ours.id)
    project.transcript = Transcript(
        language="mn",
        segments=[Segment(id="s1", start=0.0, end=4.0, text="ү" * 9000)],
        full_text="ү" * 9000,
    )
    save(db, project)
    _priced_suggest(db, hers.id, cost=4.00, characters=9000, jid="apispend3")

    spend = client.get(f"/projects/{ours.id}", headers=_auth(client, "spendmine")).json()["spend"]
    assert spend["suggest_estimate_usd"] is None


def test_the_job_history_is_deeper_than_one_afternoon(client, db):
    """Ten rows is three exports and a retry. The question this answers —
    what ran, how long it took, what it cost — needs more than that, and the
    page is told the limit so a short list does not read as a quiet project."""
    from app.api.projects.documents import JOB_HISTORY_LIMIT
    from app.dbmodels import Job

    user = _user(db, "historyreader")
    row = Project(owner_id=user.id, name="p", doc={})
    db.add(row)
    db.commit()
    for i in range(15):
        db.add(Job(
            id=f"apihist{i}", project_id=row.id, kind="export", state="done",
            result={"payload": {}, "output": {"elapsed_sec": 1.0}},
            created_at=time.time() + i,
        ))
    db.commit()

    data = client.get(f"/projects/{row.id}", headers=_auth(client, "historyreader")).json()
    assert len(data["jobs"]) == 15
    assert data["job_history_limit"] == JOB_HISTORY_LIMIT >= 15


# ---------------------------------------------------------------------------
# How many ideas to ask for
# ---------------------------------------------------------------------------

def _suggestable(db, monkeypatch, username: str, duration_sec: float) -> Project:
    """A project the suggest route will accept, with the LLM configured."""
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(update={"openrouter_api_key": "sk-or-test"})
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)

    owner = _user(db, username)
    row = _project_with_video(db, owner)
    row.doc = {
        **row.doc,
        "video": {**row.doc["video"], "duration_sec": duration_sec},
        "transcript": {
            "language": "mn", "full_text": "яриа",
            "segments": [{"id": "s", "start": 0.0, "end": 2.0, "text": "яриа", "words": []}],
        },
    }
    db.commit()
    return row


def _queued_payload(db, job_id: str) -> dict:
    from app.dbmodels import Job

    db.expire_all()
    return queue.payload_of(db.get(Job, job_id))


def test_the_chosen_counts_travel_to_the_job(client, db, monkeypatch):
    row = _suggestable(db, monkeypatch, "chooser", duration_sec=1800.0)
    response = client.post(
        f"/projects/{row.id}/suggest", json={"shorts": 6, "youtube": 2},
        headers=_auth(client, "chooser"),
    )
    assert response.status_code == 200
    assert _queued_payload(db, response.json()["job_id"]) == {"shorts": 6, "youtube": 2}


def test_no_body_still_means_what_the_button_always_meant(client, db, monkeypatch):
    """A client from before the choice must keep working unchanged."""
    row = _suggestable(db, monkeypatch, "oldclient", duration_sec=1800.0)
    response = client.post(f"/projects/{row.id}/suggest", headers=_auth(client, "oldclient"))
    assert response.status_code == 200
    assert _queued_payload(db, response.json()["job_id"]) == {"shorts": 3, "youtube": 3}


def test_more_shorts_than_the_video_can_hold_is_refused_not_clamped(client, db, monkeypatch):
    """A silent clamp turns "8" into "2" AFTER the click; the producer finds
    out only when fewer come back than they paid to ask for."""
    row = _suggestable(db, monkeypatch, "greedy", duration_sec=100.0)  # holds 2
    response = client.post(
        f"/projects/{row.id}/suggest", json={"shorts": 8, "youtube": 0},
        headers=_auth(client, "greedy"),
    )
    assert response.status_code == 422
    assert "2" in response.json()["detail"]


def test_plans_for_a_video_under_twenty_minutes_are_refused(client, db, monkeypatch):
    row = _suggestable(db, monkeypatch, "shortvid", duration_sec=900.0)
    response = client.post(
        f"/projects/{row.id}/suggest", json={"shorts": 3, "youtube": 3},
        headers=_auth(client, "shortvid"),
    )
    assert response.status_code == 422
    assert "20 минут" in response.json()["detail"]


def test_counts_outside_the_absolute_range_never_reach_the_route(client, db, monkeypatch):
    """Zero shorts would be a paid call for nothing; nine exceeds any video."""
    row = _suggestable(db, monkeypatch, "bounds", duration_sec=3600.0)
    auth = _auth(client, "bounds")
    for body in ({"shorts": 0}, {"shorts": 9}, {"youtube": -1}, {"youtube": 6}):
        assert client.post(f"/projects/{row.id}/suggest", json=body, headers=auth).status_code == 422


def test_the_project_says_what_range_the_picker_may_offer(client, db, monkeypatch):
    """The page draws its picker from this, never from its own copy of the
    rule — a second copy is how a number reaches the page that the server
    then refuses."""
    row = _suggestable(db, monkeypatch, "limits", duration_sec=1800.0)
    limits = client.get(f"/projects/{row.id}", headers=_auth(client, "limits")).json()["suggest_limits"]
    assert limits == {"shorts_max": 8, "youtube_max": 5, "shorts_default": 3, "youtube_default": 3}


def test_a_short_video_is_offered_what_it_can_hold(client, db, monkeypatch):
    row = _suggestable(db, monkeypatch, "shortlimits", duration_sec=100.0)
    limits = client.get(f"/projects/{row.id}", headers=_auth(client, "shortlimits")).json()["suggest_limits"]
    assert limits == {"shorts_max": 2, "youtube_max": 0, "shorts_default": 2, "youtube_default": 0}


# ---------------------------------------------------------------------------
# Languages: declaring one, hearing it, translating it, exporting it
# ---------------------------------------------------------------------------

def _english_project(db, username: str, *, translations=(None, None), llm_key: bool = True,
                     monkeypatch=None, **export) -> Project:
    from app import provider_settings
    from app.config import get_settings

    if monkeypatch is not None:
        settings = get_settings().model_copy(update={"openrouter_api_key": "sk-or-test" if llm_key else ""})
        monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)
    owner = _user(db, username)
    row = _project_with_video(db, owner)
    doc = {
        **row.doc,
        "language": "en",
        "transcript": {
            "language": "eng", "full_text": "Hello. Bye.",
            "segments": [
                {"id": "a", "start": 0.0, "end": 2.0, "text": "Hello.", "words": [],
                 "translation": translations[0]},
                {"id": "b", "start": 2.0, "end": 4.0, "text": "Bye.", "words": [],
                 "translation": translations[1]},
            ],
        },
    }
    if export:
        doc["export"] = export
    row.doc = doc
    db.commit()
    return row


def test_a_project_is_created_in_the_language_it_is_spoken_in(client, db):
    _user(db, "polyglot")
    auth = _auth(client, "polyglot")
    made = client.post("/projects", json={
        "name": "talk", "filename": "talk.mp4", "size_bytes": 1024, "language": "en",
    }, headers=auth).json()
    assert client.get(f"/projects/{made['project_id']}", headers=auth).json()["language"] == "en"


def test_a_client_that_never_says_gets_mongolian(client, db):
    _user(db, "monoglot")
    auth = _auth(client, "monoglot")
    body = {"name": "t", "filename": "t.mp4", "size_bytes": 1024}
    made = client.post("/projects", json=body, headers=auth).json()
    assert client.get(f"/projects/{made['project_id']}", headers=auth).json()["language"] == "mn"


def test_a_language_nothing_can_hear_is_refused(client, db):
    _user(db, "klingon")
    response = client.post("/projects", json={
        "name": "t", "filename": "t.mp4", "size_bytes": 1024, "language": "tlh",
    }, headers=_auth(client, "klingon"))
    assert response.status_code == 422


def test_a_wrong_language_can_be_corrected(client, db):
    alice = _user(db, "fixer")
    row = _project_with_video(db, alice)
    auth = _auth(client, "fixer")
    assert client.patch(f"/projects/{row.id}", json={"language": "en"}, headers=auth).status_code == 200
    assert client.get(f"/projects/{row.id}", headers=auth).json()["language"] == "en"


def test_an_english_video_is_refused_when_scribe_cannot_hear_it(client, db, monkeypatch):
    """The readiness check read the SELECTED recogniser. An English video is
    heard by Scribe whatever is selected, so a missing Scribe key has to stop
    it here, before the download — and say why."""
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "stt_provider": "duudlaga", "duudlaga_api_key": "dd-key", "elevenlabs_api_key": "",
    })
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)
    row = _english_project(db, "noscribe")

    response = client.post(
        f"/projects/{row.id}/transcribe", json={"force": True}, headers=_auth(client, "noscribe")
    )
    assert response.status_code == 503
    assert "ElevenLabs Scribe" in response.json()["detail"]


def test_an_english_video_is_accepted_when_scribe_can_hear_it(client, db, monkeypatch):
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(update={"stt_provider": "duudlaga", "elevenlabs_api_key": "el-key"})
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)
    row = _english_project(db, "scribe")
    auth = _auth(client, "scribe")
    response = client.post(f"/projects/{row.id}/transcribe", json={"force": True}, headers=auth)
    assert response.status_code == 200


def test_translating_queues_a_job_that_fills_only_the_gaps_by_default(client, db, monkeypatch):
    row = _english_project(db, "translator", monkeypatch=monkeypatch)
    response = client.post(f"/projects/{row.id}/translate", headers=_auth(client, "translator"))
    assert response.status_code == 200
    assert _queued_payload(db, response.json()["job_id"]) == {"force": False, "scope": "all"}


def test_force_travels_to_the_job(client, db, monkeypatch):
    row = _english_project(db, "forcer", monkeypatch=monkeypatch)
    response = client.post(
        f"/projects/{row.id}/translate", json={"force": True}, headers=_auth(client, "forcer")
    )
    assert _queued_payload(db, response.json()["job_id"]) == {"force": True, "scope": "all"}


def test_the_lines_the_suggestions_cut_can_be_translated_on_their_own(client, db, monkeypatch):
    """What the translation after the suggestions runs — and the button that
    finishes it, when a run left some behind."""
    row = _english_project(db, "scoper", monkeypatch=monkeypatch)
    auth = _auth(client, "scoper")
    body = {"scope": "suggested"}

    # Nothing is cut before there are suggestions: refused, not a free job.
    none = client.post(f"/projects/{row.id}/translate", json=body, headers=auth)
    assert none.status_code == 400

    _with_suggestions(db, row)
    response = client.post(f"/projects/{row.id}/translate", json=body, headers=auth)
    assert _queued_payload(db, response.json()["job_id"]) == {"force": False, "scope": "suggested"}

    wrong = {"scope": "everything"}
    assert client.post(f"/projects/{row.id}/translate", json=wrong, headers=auth).status_code == 422


def test_a_mongolian_video_is_not_sent_for_translation(client, db, monkeypatch):
    """A paid call whose answer is the text it was given is a bill for nothing."""
    row = _suggestable(db, monkeypatch, "mnonly", duration_sec=600.0)
    response = client.post(f"/projects/{row.id}/translate", headers=_auth(client, "mnonly"))
    assert response.status_code == 400


def test_translation_is_refused_without_a_model_key(client, db, monkeypatch):
    row = _english_project(db, "nollm", monkeypatch=monkeypatch, llm_key=False)
    assert client.post(f"/projects/{row.id}/translate", headers=_auth(client, "nollm")).status_code == 503


def test_a_translation_can_be_edited_by_hand(client, db, monkeypatch):
    row = _english_project(db, "editor1", translations=("Сайн уу.", None), monkeypatch=monkeypatch)
    auth = _auth(client, "editor1")
    body = {"segments": [{"id": "a", "translation": "Сайн байна уу."}]}
    assert client.put(f"/projects/{row.id}/transcript", json=body, headers=auth).json()["updated"] == 1
    segs = client.get(f"/projects/{row.id}", headers=auth).json()["transcript"]["segments"]
    assert segs[0]["translation"] == "Сайн байна уу." and segs[0]["text"] == "Hello."


def test_correcting_what_was_said_clears_its_translation(client, db, monkeypatch):
    """A translation of words that are no longer there is a subtitle saying
    something nobody said. Cleared, the line is exactly what the next run —
    which sends only untranslated lines — fixes."""
    row = _english_project(db, "editor2", translations=("Сайн уу.", "Баяртай."), monkeypatch=monkeypatch)
    auth = _auth(client, "editor2")
    body = {"segments": [{"id": "a", "text": "Hello, friend."}]}
    result = client.put(f"/projects/{row.id}/transcript", json=body, headers=auth).json()
    assert result["translations_cleared"] == 1
    segs = client.get(f"/projects/{row.id}", headers=auth).json()["transcript"]["segments"]
    assert segs[0]["translation"] is None and segs[1]["translation"] == "Баяртай."


def test_an_edit_that_gives_both_keeps_the_new_translation(client, db, monkeypatch):
    row = _english_project(db, "editor3", translations=("Сайн уу.", None), monkeypatch=monkeypatch)
    auth = _auth(client, "editor3")
    body = {"segments": [{"id": "a", "text": "Hi.", "translation": "Сайн."}]}
    result = client.put(f"/projects/{row.id}/transcript", json=body, headers=auth).json()
    assert result["translations_cleared"] == 0
    segs = client.get(f"/projects/{row.id}", headers=auth).json()["transcript"]["segments"]
    assert segs[0]["translation"] == "Сайн."


def test_an_empty_translation_clears_it(client, db, monkeypatch):
    row = _english_project(db, "editor4", translations=("Сайн уу.", None), monkeypatch=monkeypatch)
    auth = _auth(client, "editor4")
    body = {"segments": [{"id": "a", "translation": " "}]}
    client.put(f"/projects/{row.id}/transcript", json=body, headers=auth)
    segs = client.get(f"/projects/{row.id}", headers=auth).json()["transcript"]["segments"]
    assert segs[0]["translation"] is None


def test_an_edit_that_says_nothing_is_refused(client, db, monkeypatch):
    row = _english_project(db, "editor5", monkeypatch=monkeypatch)
    response = client.put(f"/projects/{row.id}/transcript", json={"segments": [{"id": "a"}]},
                            headers=_auth(client, "editor5"))
    assert response.status_code == 422


def _with_suggestions(db, row) -> None:
    project = load(db, row.id)
    from app.models import Cut, ShortIdea, Suggestions

    project.suggestions = Suggestions(shorts=[ShortIdea(
        id="sh1", title="t", hook_text="h", hook_quote="q", caption="c", why_it_works="w",
        cuts=[
            Cut(start=0.0, end=2.0, role="hook", reason="r"),
            Cut(start=2.0, end=4.0, role="payoff", reason="r"),
        ],
    )])
    save(db, project)
    db.commit()


def test_an_export_whose_mongolian_subtitles_would_have_holes_is_refused(client, db, monkeypatch):
    """Minutes of encoding, then untranslated lines in the source language
    mid-video with nothing to say why. Refused before, with both ways out."""
    row = _english_project(db, "holes", translations=("Сайн уу.", None), monkeypatch=monkeypatch)
    _with_suggestions(db, row)
    response = client.post(f"/projects/{row.id}/export-all", headers=_auth(client, "holes"))
    assert response.status_code == 409
    assert "1 мөр" in response.json()["detail"]


def _with_a_line_far_from_the_cuts(db, row, *, cut: bool = False) -> None:
    """A third line at 30-32s, untranslated. `cut` adds a second idea that
    cuts it; otherwise no idea does."""
    project = load(db, row.id)
    from app.models import Cut, Segment, ShortIdea

    project.transcript.segments.append(Segment(id="far", start=30.0, end=32.0, text="Far away."))
    if cut:
        project.suggestions.shorts.append(ShortIdea(
            id="sh2", title="t2", hook_text="h", hook_quote="q", caption="c", why_it_works="w",
            cuts=[Cut(start=29.5, end=33.0, role="hook", reason="r")],
        ))
    save(db, project)
    db.commit()


def test_a_line_no_idea_cuts_never_holds_the_export_back(client, db, monkeypatch):
    """The translation after the suggestions translates what they cut and
    nothing else — so a line nobody cut is a line nobody sees."""
    row = _english_project(db, "farline", translations=("Сайн уу.", "Баяртай."), monkeypatch=monkeypatch)
    _with_suggestions(db, row)
    _with_a_line_far_from_the_cuts(db, row)
    auth = _auth(client, "farline")

    view = client.get(f"/projects/{row.id}", headers=auth).json()["translation"]
    assert (view["missing"], view["suggested_lines"], view["suggested_missing"]) == (1, 2, 0)
    assert view["suggested_ids"] == ["a", "b"] and view["blocks_export"] is False
    assert client.post(f"/projects/{row.id}/export-all", headers=auth).status_code == 200


def test_an_export_of_some_ideas_asks_only_about_their_lines(client, db, monkeypatch):
    row = _english_project(db, "pickline", translations=("Сайн уу.", "Баяртай."), monkeypatch=monkeypatch)
    _with_suggestions(db, row)
    _with_a_line_far_from_the_cuts(db, row, cut=True)
    auth = _auth(client, "pickline")

    everything = client.post(f"/projects/{row.id}/export-all", headers=auth)
    assert everything.status_code == 409 and "1 мөр" in everything.json()["detail"]
    view = client.get(f"/projects/{row.id}", headers=auth).json()["translation"]
    # The page's buttons export every idea, and the page is told so.
    assert view["blocks_export"] is True and view["suggested_missing"] == 1

    ready = client.post(f"/projects/{row.id}/export-all", json={"shorts": ["sh1"]}, headers=auth)
    assert ready.status_code == 200
    held = client.post(f"/projects/{row.id}/export-all", json={"shorts": ["sh2"]}, headers=auth)
    assert held.status_code == 409


def test_a_hand_cut_timeline_is_held_to_the_same_rule(client, db, monkeypatch):
    """The "cut it myself" export renders the same subtitles as the ideas do.
    Guarded on one route and not the other, holes would ship from the one
    nobody thought to check."""
    row = _english_project(db, "timeline", translations=("Сайн уу.", None), monkeypatch=monkeypatch)
    auth = _auth(client, "timeline")
    cut = client.post(f"/projects/{row.id}/select", json={"ranges": [[0.0, 4.0]]}, headers=auth)
    assert cut.status_code == 200
    response = client.post(f"/projects/{row.id}/export", headers=auth)
    assert response.status_code == 409 and "1 мөр" in response.json()["detail"]

    body = {"segments": [{"id": "b", "translation": "Баяртай."}]}
    client.put(f"/projects/{row.id}/transcript", json=body, headers=auth)
    assert client.post(f"/projects/{row.id}/export", headers=auth).status_code == 200


def test_an_export_in_the_source_language_needs_no_translation(client, db, monkeypatch):
    row = _english_project(db, "srcsubs", monkeypatch=monkeypatch, subtitle_language="source")
    _with_suggestions(db, row)
    assert client.post(f"/projects/{row.id}/export-all", headers=_auth(client, "srcsubs")).status_code == 200


def test_an_export_with_no_subtitles_needs_no_translation(client, db, monkeypatch):
    row = _english_project(db, "nosubs", monkeypatch=monkeypatch, burn_subtitles=False, write_srt=False)
    _with_suggestions(db, row)
    assert client.post(f"/projects/{row.id}/export-all", headers=_auth(client, "nosubs")).status_code == 200


def test_a_fully_translated_export_goes_ahead(client, db, monkeypatch):
    row = _english_project(db, "allgood", translations=("Сайн уу.", "Баяртай."), monkeypatch=monkeypatch)
    _with_suggestions(db, row)
    response = client.post(f"/projects/{row.id}/export-all", headers=_auth(client, "allgood"))
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("username", "translations", "export"),
    [
        ("verdict1", ("Сайн уу.", None), {}),
        ("verdict2", ("Сайн уу.", "Баяртай."), {}),
        ("verdict3", (None, None), {"subtitle_language": "source"}),
        ("verdict4", (None, None), {"burn_subtitles": False, "write_srt": False}),
        ("verdict5", (None, None), {"burn_subtitles": False, "write_srt": True}),
    ],
)
def test_the_page_is_told_exactly_what_the_export_will_decide(
    client, db, monkeypatch, username, translations, export
):
    """The page disables its export button from `blocks_export`. Worked out
    anywhere but the guard itself, the two would drift, and the page would
    offer a click the server refuses — or hide one it would accept."""
    row = _english_project(db, username, translations=translations, monkeypatch=monkeypatch, **export)
    _with_suggestions(db, row)
    auth = _auth(client, username)
    view = client.get(f"/projects/{row.id}", headers=auth).json()["translation"]
    refused = client.post(f"/projects/{row.id}/export-all", headers=auth).status_code == 409
    assert view["blocks_export"] is refused
    missing = sum(1 for t in translations if t is None)
    assert (view["lines"], view["translated"], view["missing"]) == (2, 2 - missing, missing)


def test_a_mongolian_video_has_nothing_to_translate(client, db):
    alice = _user(db, "mnview")
    row = _project_with_video(db, alice)
    view = client.get(f"/projects/{row.id}", headers=_auth(client, "mnview")).json()["translation"]
    assert view == {"needed": False, "lines": 0, "translated": 0, "missing": 0, "blocks_export": False,
                    "used_for": [], "suggested_lines": 0, "suggested_missing": 0, "suggested_ids": []}


def test_the_subtitle_language_is_a_setting(client, db):
    alice = _user(db, "subslang")
    row = _project_with_video(db, alice)
    auth = _auth(client, "subslang")
    body = {"export": {"subtitle_language": "source"}}
    saved = client.patch(f"/projects/{row.id}", json=body, headers=auth).json()
    assert saved["export"]["subtitle_language"] == "source"
    bad = {"export": {"subtitle_language": "fr"}}
    assert client.patch(f"/projects/{row.id}", json=bad, headers=auth).status_code == 422



# ---------------------------------------------------------------------------
# Export settings: every field the page saves is a field the server keeps
# ---------------------------------------------------------------------------

def test_every_export_setting_can_be_written():
    """`logo`, `use_intro` and `use_outro` were missing from the input model
    from the day they were added, and pydantic drops a field it does not know
    without a word: the page said "saved" and the next read had them off. The
    rule, so the next field added to one list cannot be missed in the other."""
    from app.models import ExportSettings, LogoSettings
    from app.schemas import ExportSettingsIn, LogoIn

    assert set(ExportSettingsIn.model_fields) == set(ExportSettings.model_fields)
    assert set(LogoIn.model_fields) == set(LogoSettings.model_fields)


def test_the_logo_intro_and_outro_choices_are_kept(client, db):
    alice = _user(db, "brandkeep")
    row = _project_with_video(db, alice)
    auth = _auth(client, "brandkeep")
    sized = {"export": {"logo": {"width_pct": 20.0}}}
    assert client.patch(f"/projects/{row.id}", json=sized, headers=auth).status_code == 200
    body = {"export": {"logo": {"enabled": True, "position": "top-left"},
                       "use_intro": True, "use_outro": True}}
    assert client.patch(f"/projects/{row.id}", json=body, headers=auth).status_code == 200

    saved = client.get(f"/projects/{row.id}", headers=auth).json()["export"]
    assert saved["use_intro"] is True and saved["use_outro"] is True
    assert saved["logo"]["enabled"] is True and saved["logo"]["position"] == "top-left"
    # Merged, not replaced: what was not sent keeps the value it was GIVEN —
    # a default would come back either way.
    assert saved["logo"]["width_pct"] == 20.0


def test_a_logo_position_nothing_can_draw_is_refused(client, db):
    alice = _user(db, "brandbad")
    row = _project_with_video(db, alice)
    body = {"export": {"logo": {"position": "center"}}}
    response = client.patch(f"/projects/{row.id}", json=body, headers=_auth(client, "brandbad"))
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The Mongolian voice-over
# ---------------------------------------------------------------------------

def _voice_ready(monkeypatch, ready: bool = True) -> None:
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "openrouter_api_key": "sk-or-test",
        "elevenlabs_api_key": "el-key" if ready else "",
        "elevenlabs_tts_voice_id": "Voice123" if ready else "",
    })
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)


def test_the_voice_choice_is_kept_and_its_level_bounded(client, db):
    alice = _user(db, "voicekeep")
    row = _project_with_video(db, alice)
    auth = _auth(client, "voicekeep")
    body = {"export": {"voice_over": True, "original_volume": 0.35}}
    saved = client.patch(f"/projects/{row.id}", json=body, headers=auth).json()["export"]
    assert saved["voice_over"] is True and saved["original_volume"] == 0.35

    too_loud = {"export": {"original_volume": 1.5}}
    assert client.patch(f"/projects/{row.id}", json=too_loud, headers=auth).status_code == 422


def test_the_page_is_told_whether_the_voice_can_be_made(client, db, monkeypatch):
    row = _english_project(db, "voiceview", translations=("Сайн уу.", "Баяртай."),
                           voice_over=True)
    auth = _auth(client, "voiceview")

    _voice_ready(monkeypatch, ready=False)
    view = client.get(f"/projects/{row.id}", headers=auth).json()["voice"]
    assert view["on"] is True and "түлхүүр" in view["blocked"]

    _voice_ready(monkeypatch, ready=True)
    assert client.get(f"/projects/{row.id}", headers=auth).json()["voice"] == {
        "on": True, "blocked": None,
    }


def test_a_mongolian_video_has_no_voice_to_make(client, db):
    alice = _user(db, "voicemn")
    row = _project_with_video(db, alice)
    auth = _auth(client, "voicemn")
    client.patch(f"/projects/{row.id}", json={"export": {"voice_over": True}}, headers=auth)
    assert client.get(f"/projects/{row.id}", headers=auth).json()["voice"] == {
        "on": False, "blocked": None,
    }


def test_an_export_whose_voice_cannot_be_made_is_refused_before_it_starts(client, db, monkeypatch):
    """Not exported silent: the producer asked for a Mongolian voice, and a
    video without one looks finished."""
    row = _english_project(db, "voiceno", translations=("Сайн уу.", "Баяртай."), voice_over=True)
    _with_suggestions(db, row)
    _voice_ready(monkeypatch, ready=False)
    auth = _auth(client, "voiceno")

    for route in ("export-all", "export"):
        if route == "export":
            client.post(f"/projects/{row.id}/select", json={"ranges": [[0.0, 4.0]]}, headers=auth)
        response = client.post(f"/projects/{row.id}/{route}", headers=auth)
        assert response.status_code == 503, route
        assert response.json()["detail"].startswith("Монгол дуу")


def test_a_voice_with_lines_left_to_translate_names_the_way_out_that_works(client, db, monkeypatch):
    """Subtitles already in the source language: the usual advice — switch
    them to the source language — would fix nothing."""
    row = _english_project(db, "voicegap", translations=("Сайн уу.", None),
                           voice_over=True, subtitle_language="source")
    _with_suggestions(db, row)
    _voice_ready(monkeypatch)
    response = client.post(f"/projects/{row.id}/export-all", headers=_auth(client, "voicegap"))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "Монгол дуу орчуулгыг уншдаг" in detail and "эх хэлээр" not in detail


def test_a_voice_over_that_can_be_made_goes_ahead(client, db, monkeypatch):
    row = _english_project(db, "voicego", translations=("Сайн уу.", "Баяртай."), voice_over=True)
    _with_suggestions(db, row)
    _voice_ready(monkeypatch)
    response = client.post(f"/projects/{row.id}/export-all", headers=_auth(client, "voicego"))
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# A clean dub: the source speech removed from under the voice, not ducked
# ---------------------------------------------------------------------------

def _worker_says(db, *, separation: bool) -> None:
    """What the worker would publish about itself (app.worker_report)."""
    import time

    from app import worker_report

    worker_report.publish(db, worker_report.WorkerReport(
        separation=separation, model="htdemucs", at=time.time(),
    ))
    db.commit()


def test_the_source_speech_is_ducked_unless_asked_and_only_two_ways_are_known(client, db):
    alice = _user(db, "speechkeep")
    row = _project_with_video(db, alice)
    auth = _auth(client, "speechkeep")
    # Every project exported before the choice existed sounds as it did.
    assert client.get(f"/projects/{row.id}", headers=auth).json()["export"]["source_speech"] == "duck"

    body = {"export": {"source_speech": "remove"}}
    saved = client.patch(f"/projects/{row.id}", json=body, headers=auth).json()["export"]
    assert saved["source_speech"] == "remove"

    muted = {"export": {"source_speech": "mute"}}
    assert client.patch(f"/projects/{row.id}", json=muted, headers=auth).status_code == 422


def test_removing_the_speech_waits_for_a_worker_that_says_it_can(client, db, monkeypatch):
    """The API image never has Demucs; only the worker can answer, and until
    it has, unknown is not "can"."""
    row = _english_project(db, "cleandub", translations=("Сайн уу.", "Баяртай."),
                           voice_over=True, source_speech="remove")
    auth = _auth(client, "cleandub")

    def blocked():
        return client.get(f"/projects/{row.id}", headers=auth).json()["voice"]["blocked"]

    # With no key there is no voice to lay on anything: that is said first.
    _voice_ready(monkeypatch, ready=False)
    assert "түлхүүр" in blocked()

    _voice_ready(monkeypatch)
    assert "мэдээлээгүй" in blocked()

    _worker_says(db, separation=False)
    # Names the fix, and the choice that works without it.
    assert "INSTALL_DUB=1" in blocked() and "Намсгах" in blocked()

    _worker_says(db, separation=True)
    assert blocked() is None


def test_a_ducked_voice_asks_nothing_of_the_worker(client, db, monkeypatch):
    row = _english_project(db, "duckdub", translations=("Сайн уу.", "Баяртай."), voice_over=True)
    _voice_ready(monkeypatch)
    _worker_says(db, separation=False)
    view = client.get(f"/projects/{row.id}", headers=_auth(client, "duckdub")).json()["voice"]
    assert view == {"on": True, "blocked": None}


def test_removing_the_speech_asks_nothing_while_the_voice_is_off(client, db, monkeypatch):
    """The choice outlives the voice that needed it. Turned off, there is no
    dub to clean — and an export refused for it would be refused for nothing."""
    row = _english_project(db, "voiceoff", translations=("Сайн уу.", "Баяртай."),
                           voice_over=False, source_speech="remove")
    _with_suggestions(db, row)
    _voice_ready(monkeypatch)
    _worker_says(db, separation=False)
    auth = _auth(client, "voiceoff")
    assert client.get(f"/projects/{row.id}", headers=auth).json()["voice"] == {
        "on": False, "blocked": None,
    }
    assert client.post(f"/projects/{row.id}/export-all", headers=auth).status_code == 200


def test_a_video_without_sound_has_no_speech_to_remove(client, db, monkeypatch):
    """The worker lays the voice alone there; refusing it would be refusing
    an export that would have been made."""
    row = _english_project(db, "mutedub", translations=("Сайн уу.", "Баяртай."),
                           voice_over=True, source_speech="remove")
    row.doc = {**row.doc, "video": {**row.doc["video"], "has_audio": False}}
    db.commit()
    _voice_ready(monkeypatch)
    view = client.get(f"/projects/{row.id}", headers=_auth(client, "mutedub")).json()["voice"]
    assert view == {"on": True, "blocked": None}


def test_an_export_that_cannot_remove_the_speech_is_refused_before_it_starts(
    client, db, monkeypatch,
):
    """Not queued to fail in the worker after the voice was bought, and not
    exported with the English still under it: that is the export the producer
    chose not to make."""
    row = _english_project(db, "cleanno", translations=("Сайн уу.", "Баяртай."),
                           voice_over=True, source_speech="remove")
    _with_suggestions(db, row)
    _voice_ready(monkeypatch)
    _worker_says(db, separation=False)
    auth = _auth(client, "cleanno")

    for route in ("export-all", "export"):
        if route == "export":
            client.post(f"/projects/{row.id}/select", json={"ranges": [[0.0, 4.0]]}, headers=auth)
        response = client.post(f"/projects/{row.id}/{route}", headers=auth)
        assert response.status_code == 503, route
        assert "INSTALL_DUB=1" in response.json()["detail"]

    _worker_says(db, separation=True)
    assert client.post(f"/projects/{row.id}/export-all", headers=auth).status_code == 200


def test_the_admin_sees_whether_the_worker_can_remove_the_speech(client, db):
    _user(db, "root", role="admin")

    def separation():
        body = client.get("/admin/providers", headers=_auth(client, "root")).json()
        return next(c for c in body["capabilities"] if c["name"] == "separation")

    # Listed before any worker has spoken: an operator looking for why the
    # choice is refused finds the row that says so.
    unknown = separation()
    assert unknown["ready"] is False and "мэдээлээгүй" in unknown["blocked"]

    _worker_says(db, separation=True)
    ready = separation()
    assert ready["ready"] is True and ready["blocked"] is None
    assert "htdemucs" in ready["provider"]


# ---------------------------------------------------------------------------
# Choosing the voice
# ---------------------------------------------------------------------------

def _fake_voices(monkeypatch, *, voices=None, languages=None, fail=None, languages_fail=False):
    from app.tts import elevenlabs

    class Fake:
        async def voices(self):
            if fail:
                raise elevenlabs.TtsError(fail)
            return voices or []

        async def model_languages(self, model_id):
            if languages_fail:
                raise elevenlabs.TtsError("models unavailable")
            return languages

        async def aclose(self):
            pass

    monkeypatch.setattr(elevenlabs, "build_client", lambda settings: Fake())


def test_the_admin_picks_from_the_voices_the_account_has(client, db, monkeypatch):
    _user(db, "voicepick", role="admin")
    _voice_ready(monkeypatch)
    _fake_voices(monkeypatch, voices=[{"id": "V1", "name": "Ana"}], languages=["en", "mn"])
    body = client.get("/admin/tts/voices", headers=_auth(client, "voicepick")).json()
    assert body["voices"] == [{"id": "V1", "name": "Ana"}]
    assert body["mongolian"] is True and body["model"] == "eleven_v3"
    assert body["voice_id"] == "Voice123" and body["error"] is None


@pytest.mark.parametrize(("languages", "fails", "expected"), [
    (["en", "ru"], False, False),
    (None, False, None),
    (["mn"], True, None),
])
def test_whether_the_model_speaks_mongolian_is_asked_not_guessed(
    client, db, monkeypatch, languages, fails, expected
):
    _user(db, "voicelang", role="admin")
    _voice_ready(monkeypatch)
    _fake_voices(monkeypatch, voices=[{"id": "V1", "name": "Ana"}], languages=languages,
                 languages_fail=fails)
    body = client.get("/admin/tts/voices", headers=_auth(client, "voicelang")).json()
    assert body["mongolian"] is expected
    # The voices are still worth showing when the language list is not.
    assert body["voices"]


def test_a_provider_that_cannot_be_reached_is_the_answer_not_a_500(client, db, monkeypatch):
    _user(db, "voicedown", role="admin")
    _voice_ready(monkeypatch)
    _fake_voices(monkeypatch, fail="ElevenLabs дуу үүсгэж чадсангүй (401): API түлхүүр буруу")
    response = client.get("/admin/tts/voices", headers=_auth(client, "voicedown"))
    assert response.status_code == 200
    assert "API түлхүүр буруу" in response.json()["error"] and response.json()["voices"] == []


def test_no_key_is_said_without_calling_anyone(client, db, monkeypatch):
    _user(db, "voicenokey", role="admin")
    _voice_ready(monkeypatch, ready=False)
    _fake_voices(monkeypatch, fail="must not be called")
    body = client.get("/admin/tts/voices", headers=_auth(client, "voicenokey")).json()
    assert "түлхүүр" in body["error"] and body["voices"] == []


def test_the_voice_list_is_for_admins_only(client, db):
    _user(db, "voiceeditor")
    assert client.get("/admin/tts/voices", headers=_auth(client, "voiceeditor")).status_code == 403


# ---------------------------------------------------------------------------
# A voice per speaker
# ---------------------------------------------------------------------------

def test_each_speaker_is_given_a_voice_and_the_map_is_sent_whole(client, db):
    alice = _user(db, "speakermap")
    row = _project_with_video(db, alice)
    auth = _auth(client, "speakermap")

    def patch(export: dict) -> dict:
        response = client.patch(f"/projects/{row.id}", json={"export": export}, headers=auth)
        assert response.status_code == 200, response.text
        return response.json()["export"]["speaker_voices"]

    # An empty id is "the default voice": no entry, rather than a stored "".
    assert patch({"speaker_voices": {"speaker_0": "Anna", "speaker_1": ""}}) == {"speaker_0": "Anna"}
    # Sent whole, so taking a speaker's voice away is sending the map without it.
    assert patch({"speaker_voices": {"speaker_1": "Bold"}}) == {"speaker_1": "Bold"}
    # Another setting saved on its own leaves the voices as they were.
    assert patch({"voice_over": True}) == {"speaker_1": "Bold"}
    assert patch({"speaker_voices": {}}) == {}


@pytest.mark.parametrize("voices", [
    {"speaker_0": "../user"},
    {"speaker_0": "a/b"},
    {"speaker_0": "a b"},
    {"speaker_0": "x" * 65},
    {"": "Anna"},
    {"s" * 65: "Anna"},
])
def test_a_speakers_voice_that_would_change_the_request_path_is_refused(client, db, voices):
    """The id goes into ElevenLabs' URL path at export, with the account's key."""
    alice = _user(db, "speakerbad")
    row = _project_with_video(db, alice)
    response = client.patch(f"/projects/{row.id}", json={"export": {"speaker_voices": voices}},
                            headers=_auth(client, "speakerbad"))
    assert response.status_code == 422


def test_no_more_voices_than_a_transcript_has_speakers(client, db):
    alice = _user(db, "speakermany")
    row = _project_with_video(db, alice)
    auth = _auth(client, "speakermany")
    fifty = {f"speaker_{i}": "Anna" for i in range(50)}
    ok = client.patch(f"/projects/{row.id}", json={"export": {"speaker_voices": fifty}}, headers=auth)
    assert ok.status_code == 200
    too_many = {**fifty, "speaker_50": "Anna"}
    refused = client.patch(f"/projects/{row.id}", json={"export": {"speaker_voices": too_many}},
                           headers=auth)
    assert refused.status_code == 422


def test_the_page_is_told_who_speaks(client, db):
    row = _english_project(db, "speakerview")
    doc = dict(row.doc)
    doc["transcript"] = {
        **doc["transcript"],
        "segments": [
            {**doc["transcript"]["segments"][0], "speaker": "speaker_1"},
            {**doc["transcript"]["segments"][1], "speaker": "speaker_0"},
        ],
    }
    row.doc = doc
    db.commit()
    auth = _auth(client, "speakerview")
    assert client.get(f"/projects/{row.id}", headers=auth).json()["speakers"] == [
        {"id": "speaker_1", "lines": 1, "sample": "Hello."},
        {"id": "speaker_0", "lines": 1, "sample": "Bye."},
    ]

    untranscribed = _project_with_video(db, _user(db, "speakernone"))
    body = client.get(f"/projects/{untranscribed.id}", headers=_auth(client, "speakernone")).json()
    assert body["speakers"] == []


def test_the_voices_a_speaker_can_have_are_listed_for_whoever_edits(client, db, monkeypatch):
    """Not only for admins: the producer choosing a speaker's voice is not one."""
    _user(db, "speakerpick")
    _voice_ready(monkeypatch)
    _fake_voices(monkeypatch, voices=[{"id": "V1", "name": "Ana"}], languages=["mn"])
    response = client.get("/projects/tts/voices", headers=_auth(client, "speakerpick"))
    assert response.status_code == 200
    # Thinner than the admin's: nothing about the model or the account.
    assert response.json() == {
        "voices": [{"id": "V1", "name": "Ana"}], "default_voice_id": "Voice123", "error": None,
    }


def test_the_speakers_voice_list_says_why_it_is_empty(client, db, monkeypatch):
    _user(db, "speakerdown")
    auth = _auth(client, "speakerdown")

    _voice_ready(monkeypatch, ready=False)
    _fake_voices(monkeypatch, fail="must not be called")
    body = client.get("/projects/tts/voices", headers=auth).json()
    assert body["voices"] == [] and "түлхүүр" in body["error"] and body["default_voice_id"] is None

    _voice_ready(monkeypatch)
    _fake_voices(monkeypatch, fail="ElevenLabs дуу үүсгэж чадсангүй (401): API түлхүүр буруу")
    response = client.get("/projects/tts/voices", headers=auth)
    assert response.status_code == 200
    assert "API түлхүүр буруу" in response.json()["error"] and response.json()["voices"] == []


def test_the_speakers_voice_list_needs_a_login(client):
    assert client.get("/projects/tts/voices").status_code == 401


# ---------------------------------------------------------------------------
# Replacing finished work is confirmed
# ---------------------------------------------------------------------------


def test_recognising_again_must_be_confirmed(client, db, monkeypatch):
    """Recognising again replaces the whole transcript — hand corrections and
    translations with it — and is billed again. It used to be one
    unconfirmed click on the stage's only button."""
    from app import provider_settings
    from app.config import get_settings
    from app.dbmodels import Job

    settings = get_settings().model_copy(
        update={"stt_provider": "elevenlabs", "elevenlabs_api_key": "el-key"}
    )
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)
    row = _english_project(db, "redoaudio", translations=("Сайн уу.", None))
    auth = _auth(client, "redoaudio")

    refused = client.post(f"/projects/{row.id}/transcribe", headers=auth)
    assert refused.status_code == 409
    assert "2 мөр" in refused.json()["detail"]
    assert db.query(Job).filter(Job.project_id == row.id).count() == 0

    confirmed = client.post(f"/projects/{row.id}/transcribe", json={"force": True}, headers=auth)
    assert confirmed.status_code == 200


def test_a_project_without_a_transcript_needs_no_confirmation(client, db, monkeypatch):
    from app import provider_settings
    from app.config import get_settings

    settings = get_settings().model_copy(
        update={"stt_provider": "elevenlabs", "elevenlabs_api_key": "el-key"}
    )
    monkeypatch.setattr(provider_settings, "effective", lambda _db: settings)
    owner = _user(db, "firstaudio")
    row = _project_with_video(db, owner)
    first = client.post(f"/projects/{row.id}/transcribe", headers=_auth(client, "firstaudio"))
    assert first.status_code == 200


def test_new_suggestions_must_be_confirmed_when_there_are_some(client, db, monkeypatch):
    row = _suggestable(db, monkeypatch, "redoideas", duration_sec=600.0)
    auth = _auth(client, "redoideas")
    _with_suggestions(db, row)

    refused = client.post(f"/projects/{row.id}/suggest", headers=auth)
    assert refused.status_code == 409
    assert "санал аль хэдийн бий" in refused.json()["detail"]

    assert client.post(f"/projects/{row.id}/suggest", json={"force": True}, headers=auth).status_code == 200


# ---------------------------------------------------------------------------
# Concurrency: one connection per request, and no lost updates
# ---------------------------------------------------------------------------


@pytest.fixture
def connections():
    """The most connections any single request held at once."""
    from sqlalchemy import event

    from app.db import get_engine

    pool = get_engine().pool
    state = {"now": 0, "peak": 0}

    def out(*_):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])

    def back(*_):
        state["now"] -= 1

    event.listen(pool, "checkout", out)
    event.listen(pool, "checkin", back)
    yield state
    event.remove(pool, "checkout", out)
    event.remove(pool, "checkin", back)


def test_a_request_holds_one_database_connection_at_a_time(client, db, connections, monkeypatch):
    """A request's session holds its connection from `current_user` on. The
    queue helpers used to open a SECOND one, so ten concurrent requests each
    held one and waited for another: the pool (5 + 5) deadlocked for 30 s
    and every one of them answered 500."""
    from app import r2

    monkeypatch.setattr(r2, "object_size", lambda key: 1024)
    owner = _user(db, "onepool")
    auth = _auth(client, "onepool")
    row = _project_with_video(db, owner)
    row.video_key = "sources/x/source.mp4"
    db.commit()
    db.close()

    calls = [
        ("GET", f"/projects/{row.id}"),
        ("GET", "/jobs/queue"),
        ("POST", f"/projects/{row.id}/upload-complete"),
    ]
    for method, path in calls:
        connections["peak"] = connections["now"] = 0
        response = client.request(method, path, headers=auth)
        assert response.status_code < 300, (path, response.text)
        assert connections["peak"] == 1, f"{method} {path} held {connections['peak']} connections"

    job_id = client.get(f"/projects/{row.id}", headers=auth).json()["jobs"][0]["job_id"]
    connections["peak"] = connections["now"] = 0
    assert client.post(f"/jobs/{job_id}/cancel", headers=auth).json()["canceled"] is True
    assert connections["peak"] == 1


def test_a_document_edit_waits_for_a_write_in_progress_instead_of_undoing_it(client, db):
    """The document is saved whole. A PATCH that read it while a worker was
    writing its result used to save the old copy back over that result — the
    job read "done", and its suggestions were gone. Every read-modify-write
    now locks the row, so the PATCH waits and then edits what is there."""
    import threading

    from app.db import get_session_factory
    from app.models import Suggestions
    from app.store import get_row, load, save, to_domain

    owner = _user(db, "lockowner")
    row = _project_with_video(db, owner)
    project = to_domain(row)
    project.transcript = Transcript(
        language="mn", segments=[Segment(id="s1", start=0, end=1, text="сайн")], full_text="сайн"
    )
    save(db, project)
    db.commit()
    auth = _auth(client, "lockowner")

    worker = get_session_factory()()  # a worker's write-back, mid-transaction
    written = load(worker, row.id, for_update=True)
    written.suggestions = Suggestions(shorts=[], requested_shorts=7)  # the worker's result
    save(worker, written)

    result = {}
    thread = threading.Thread(target=lambda: result.update(
        response=client.patch(f"/projects/{row.id}", json={"export": {"crf": 30}}, headers=auth)
    ))
    thread.start()
    thread.join(timeout=0.5)
    assert thread.is_alive(), "the PATCH should be waiting for the row lock"

    worker.commit()
    worker.close()
    thread.join(timeout=10)
    assert result["response"].status_code == 200

    db.expire_all()
    final = to_domain(get_row(db, row.id))
    assert final.export.crf == 30
    assert final.suggestions is not None and final.suggestions.requested_shorts == 7


def test_every_document_save_locks_the_row_it_read():
    """A rule, not a list: any function in app/ that saves a project document
    must have read it with `for_update=True` (creating a new project is the
    one exception — there is nothing to race with)."""
    import ast
    from pathlib import Path

    offenders = []
    for path in Path("app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
            saves = [c for c in calls if c.func.id == "save"
                     and not any(k.arg == "owner_id" for k in c.keywords)]
            if not saves or path.name == "store.py":
                continue
            locked = any(
                c.func.id in ("load", "get_row")
                and any(k.arg == "for_update" and getattr(k.value, "value", None) is True for k in c.keywords)
                for c in calls
            )
            if not locked:
                offenders.append(f"{path}:{fn.name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


def test_deleting_an_output_keeps_a_file_another_output_still_plays(client, db, monkeypatch):
    """Exports rendered before keys carried the job id share keys: the second
    export wrote over the first one's file, so the older row and the newer
    row play the same object. Deleting the older row must not delete the
    file the newer row is showing."""
    from app import r2
    from app.dbmodels import Output

    deleted: list[str] = []
    monkeypatch.setattr(r2, "delete", deleted.append)

    owner = _user(db, "legacyoutputs")
    row = _project_with_video(db, owner)
    shared, alone = f"outputs/{row.id}/reel_1.mp4", f"outputs/{row.id}/reel_2.mp4"
    older = Output(project_id=row.id, kind="reel", title="a", r2_key=shared,
                   srt_key=f"outputs/{row.id}/reel_1.srt", size_bytes=1)
    newer = Output(project_id=row.id, kind="reel", title="b", r2_key=shared,
                   srt_key=f"outputs/{row.id}/reel_1.srt", size_bytes=1)
    single = Output(project_id=row.id, kind="reel", title="c", r2_key=alone, srt_key=None, size_bytes=1)
    db.add_all([older, newer, single])
    db.commit()
    auth = _auth(client, "legacyoutputs")

    assert client.delete(f"/projects/{row.id}/outputs/{older.id}", headers=auth).status_code == 200
    assert deleted == []  # the newer row still plays both objects

    assert client.delete(f"/projects/{row.id}/outputs/{single.id}", headers=auth).status_code == 200
    assert deleted == [alone]

    assert client.delete(f"/projects/{row.id}/outputs/{newer.id}", headers=auth).status_code == 200
    assert deleted == [alone, shared, f"outputs/{row.id}/reel_1.srt"]
