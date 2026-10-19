"""Provider keys at rest (SEC-3).

The admin page stores the paid providers' keys in `settings`. In plain text
there, a database dump or backup was a dump of every paid account. With
SETTINGS_ENCRYPTION_KEY set they are sealed; without it nothing changes and
the admin page says so; and a sealed key that cannot be opened stops work
with the reason instead of reaching a provider as if it were the key.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.dbmodels import AuditLog, Setting, User
from app.security import hash_password
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

pytestmark = requires_db

SECRET = "sk-or-v1-0123456789abcdefSECRET9f3a"
KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


@pytest.fixture
def key(monkeypatch):
    """Set the encryption key (or a rotation list) for one test."""

    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
        else:
            monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", value)
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


@pytest.fixture
def client(db, key):
    from app.main import app

    key(None)
    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    return TestClient(app)


def _admin(db, client, username: str = "root") -> dict:
    salt, digest = hash_password("hunter2hunter2")
    db.add(User(username=username, pw_salt=salt, pw_hash=digest, role="admin"))
    db.commit()
    response = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _row(db, name: str) -> str:
    db.expire_all()
    return db.get(Setting, name).value


def test_a_key_entered_with_the_variable_set_is_stored_sealed(client, db, key):
    from app import provider_settings

    headers = _admin(db, client)
    key(KEY_A)
    written = client.put("/admin/settings", json={"openrouter_api_key": SECRET}, headers=headers)
    assert written.status_code == 200

    stored = _row(db, "openrouter_api_key")
    assert stored.startswith("enc:v1:")
    assert SECRET not in stored and "9f3a" not in stored
    # What a job reads is the key itself, and the page still tells keys apart.
    assert provider_settings.effective(db).openrouter_api_key == SECRET
    page = client.get("/admin/settings", headers=headers).json()
    assert page["openrouter_api_key"]["hint"].endswith("9f3a")


def test_without_the_variable_nothing_changes_and_the_page_says_so(client, db, key):
    headers = _admin(db, client)
    key(None)
    client.put("/admin/settings", json={"duudlaga_api_key": "dd-live-abcd"}, headers=headers)

    assert _row(db, "duudlaga_api_key") == "dd-live-abcd"
    status = client.get("/admin/providers", headers=headers).json()["secret_storage"]
    assert status["encrypted"] is False
    assert status["variable"] == "SETTINGS_ENCRYPTION_KEY"
    assert status["plaintext"] == ["duudlaga_api_key"]
    assert "dd-live-abcd" not in str(status)


def test_keys_stored_before_the_variable_are_sealed_once(db, key):
    """The API seals them at startup; a second instance starting beside it
    must find nothing left to do."""
    from app import provider_settings

    key(None)
    db.add(Setting(key="elevenlabs_api_key", value="el-plain-1234", updated_at=0))
    db.add(Setting(key="openrouter_model", value="openai/gpt-5", updated_at=0))
    db.commit()

    key(KEY_A)
    assert provider_settings.seal_stored(db) == 1
    assert _row(db, "elevenlabs_api_key").startswith("enc:v1:")
    # Not a secret, so never sealed: the page reads the model back in full.
    assert _row(db, "openrouter_model") == "openai/gpt-5"
    assert provider_settings.seal_stored(db) == 0
    assert provider_settings.effective(db).elevenlabs_api_key == "el-plain-1234"


def test_the_api_seals_at_startup(db, key, monkeypatch):
    """Through the real lifespan: a sealing step nothing calls protects nothing."""
    import app.main as main
    import app.migrate as migrate

    key(None)
    db.add(Setting(key="openrouter_api_key", value=SECRET, updated_at=0))
    db.commit()

    async def _caps(_ffmpeg):
        return Capabilities(ffmpeg_available=False, ffmpeg_version="", xfade_transitions=[])

    monkeypatch.setattr(migrate, "upgrade_to_head", lambda: None)
    monkeypatch.setattr(main, "build_capabilities", _caps)
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    key(KEY_A)
    with TestClient(main.app):
        pass
    assert _row(db, "openrouter_api_key").startswith("enc:v1:")


def test_a_wrong_key_does_not_stop_the_api_or_touch_the_row(db, key, monkeypatch):
    """Refusing to start would close the page that fixes it, and rewriting
    the row would destroy the only copy."""
    import app.main as main
    import app.migrate as migrate
    from app import provider_settings

    key(KEY_A)
    provider_settings.apply(db, {"openrouter_api_key": SECRET})
    sealed = _row(db, "openrouter_api_key")

    async def _caps(_ffmpeg):
        return Capabilities(ffmpeg_available=False, ffmpeg_version="", xfade_transitions=[])

    monkeypatch.setattr(migrate, "upgrade_to_head", lambda: None)
    monkeypatch.setattr(main, "build_capabilities", _caps)
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    key(KEY_B)
    with TestClient(main.app):
        pass
    assert _row(db, "openrouter_api_key") == sealed
    assert provider_settings.storage_status(db)["unreadable"] == ["openrouter_api_key"]


def test_rotation_reseals_under_the_first_key(db, key):
    from app import provider_settings

    key(KEY_A)
    db.add(Setting(key="openrouter_api_key", value="", updated_at=0))
    db.commit()
    provider_settings.apply(db, {"openrouter_api_key": SECRET})
    old = _row(db, "openrouter_api_key")

    key(f"{KEY_B},{KEY_A}")
    assert provider_settings.effective(db).openrouter_api_key == SECRET  # old key still opens
    assert provider_settings.seal_stored(db) == 1
    assert _row(db, "openrouter_api_key") != old

    key(KEY_B)  # the old key dropped: what was resealed still opens
    assert provider_settings.effective(db).openrouter_api_key == SECRET


@pytest.mark.parametrize("configured", [None, "wrong"])
def test_a_sealed_key_that_cannot_be_opened_is_an_error_not_a_key(client, db, key, configured):
    """No key, or the wrong one: the job stops with the variable named. The
    ciphertext must never be handed to a provider, and the environment's key
    must not quietly stand in either."""
    from app import provider_settings, secretbox

    headers = _admin(db, client)
    key(KEY_A)
    client.put("/admin/settings", json={"openrouter_api_key": SECRET}, headers=headers)

    key(None if configured is None else KEY_B)
    with pytest.raises(secretbox.SecretKeyError, match="SETTINGS_ENCRYPTION_KEY") as caught:
        provider_settings.effective(db)
    assert "openrouter_api_key" in str(caught.value)
    assert SECRET not in str(caught.value)

    # The page it is fixed from still answers, and names the field.
    providers = client.get("/admin/providers", headers=headers)
    assert providers.status_code == 200
    assert providers.json()["secret_storage"]["unreadable"] == ["openrouter_api_key"]
    assert client.get("/admin/settings", headers=headers).json()["openrouter_api_key"]["set"] is True

    # A request that needs the key says why, as a 503 — not a 500.
    blocked = client.get("/admin/tts/voices", headers=headers)
    assert blocked.status_code == 503
    assert "SETTINGS_ENCRYPTION_KEY" in blocked.json()["detail"]


def test_entering_the_key_again_recovers_from_a_lost_encryption_key(client, db, key):
    from app import provider_settings

    headers = _admin(db, client)
    key(KEY_A)
    client.put("/admin/settings", json={"openrouter_api_key": SECRET}, headers=headers)

    key(KEY_B)  # the old encryption key is gone
    written = client.put("/admin/settings", json={"openrouter_api_key": SECRET}, headers=headers)
    assert written.json()["changed"] == ["openrouter_api_key"]
    assert provider_settings.effective(db).openrouter_api_key == SECRET


def test_a_malformed_variable_is_refused_without_echoing_it(db, key):
    from app import provider_settings, secretbox

    db.add(Setting(key="duudlaga_api_key", value="dd-plain", updated_at=0))
    db.commit()
    key("not-a-fernet-key-but-a-secret-anyway")
    with pytest.raises(secretbox.SecretKeyError, match="SETTINGS_ENCRYPTION_KEY") as caught:
        provider_settings.seal_stored(db)
    assert "not-a-fernet-key" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_every_change_is_audited_by_name_never_by_value(client, db, key):
    headers = _admin(db, client, "keeper")
    key(KEY_A)
    client.put(
        "/admin/settings",
        json={"openrouter_api_key": SECRET, "elevenlabs_tts_voice_id": "voice-1"},
        headers=headers,
    )
    # Unchanged on the second save: nothing to record.
    client.put("/admin/settings", json={"openrouter_api_key": SECRET}, headers=headers)

    rows = db.scalars(select(AuditLog).order_by(AuditLog.path, AuditLog.id)).all()
    # Two kinds of row: the request itself, with the status it got (every
    # state-changing admin request is recorded — app.observability), and one
    # per field that actually changed, which is what says WHICH key moved.
    assert [(r.user, r.method, r.path, r.status) for r in rows] == [
        ("keeper", "PUT", "/admin/settings", 200),
        ("keeper", "PUT", "/admin/settings", 200),
        ("keeper", "PUT", "/admin/settings/elevenlabs_tts_voice_id", 200),
        ("keeper", "PUT", "/admin/settings/openrouter_api_key", 200),
    ]
    assert all(r.ip for r in rows)
    flat = " ".join(f"{r.user} {r.ip} {r.method} {r.path}" for r in rows)
    assert SECRET not in flat and "9f3a" not in flat
