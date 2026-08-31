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


def test_a_stored_key_for_an_unbuilt_feature_never_reads_as_ready():
    # ElevenLabs has a key field and no code behind it. Reporting that as
    # working is how the first attempt to use it becomes a bug report.
    from app import providers
    from app.config import get_settings

    settings = get_settings().model_copy(update={"elevenlabs_api_key": "sk_live_whatever"})
    tts = next(c for c in providers.describe(settings) if c.name == providers.TTS)

    assert tts.configured is True
    assert tts.implemented is False
    assert tts.ready is False
    assert tts.blocked


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
