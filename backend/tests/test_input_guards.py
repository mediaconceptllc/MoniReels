"""Small inputs the API must refuse or contain (audit INFO-1, INFO-2).

Each of these reached a 500, a duplicate account, a token that came back to
life, or a number one account had no business reading. None is an exploit
on its own; each is a place where the server's answer was wrong.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.dbmodels import Job, Project, User
from app.models import Segment, SubtitleStyle
from app.security import hash_password
from app.subtitle.ass import build_ass_document
from app.video.capabilities import Capabilities
from tests.conftest import requires_db

BACKEND = Path(__file__).resolve().parents[1]


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
# A range that is not a number
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.parametrize("ranges", ["[[NaN, 5]]", "[[0, NaN]]", "[[0, Infinity]]", "[[-Infinity, 5]]"])
def test_a_range_that_is_not_a_finite_number_is_refused(client, db, ranges):
    """Python's JSON reader takes NaN and Infinity, and NaN fails every
    comparison the route makes — `end <= start`, `start < 0`, past the end —
    so it walked through all three into the document, which Postgres then
    refused to store: a 500 for a typo."""
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
    headers = {**_auth(client, "alice"), "Content-Type": "application/json"}

    response = client.post(
        f"/projects/{row.id}/select", content='{"ranges": ' + ranges + '}', headers=headers
    )
    assert response.status_code == 422, response.text
    db.refresh(row)
    assert "clips" not in (row.doc or {}) or not row.doc["clips"]


# ---------------------------------------------------------------------------
# The jobs page's paging
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.parametrize("query", ["limit=-1", "limit=0", "offset=-1", "offset=99999999999999999999"])
def test_the_jobs_page_refuses_paging_it_cannot_serve(client, db, query):
    """A negative limit, or an offset past what Postgres can count, used to
    reach the query and come back as a 500."""
    _user(db, "root", role="admin")
    response = client.get(f"/admin/jobs?{query}", headers=_auth(client, "root"))
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Usernames that differ only in case
# ---------------------------------------------------------------------------


@requires_db
def test_a_username_differing_only_in_case_is_taken(client, db):
    """"alice" and "Alice" are one person to everyone reading a list of
    names, and two accounts to the database — someone can be given the
    other's work, or told the wrong password works."""
    _user(db, "root", role="admin")
    admin = _auth(client, "root")
    first = client.post("/admin/users", json={"username": "alice", "password": "firstpass1"},
                        headers=admin)
    assert first.status_code == 201

    for variant in ("Alice", "ALICE", "aLiCe"):
        again = client.post("/admin/users", json={"username": variant, "password": "firstpass1"},
                            headers=admin)
        assert again.status_code == 409, variant
        # The name already there is the one named, not the one just typed.
        assert "alice" in again.json()["detail"]

    assert sum(1 for u in client.get("/admin/users", headers=admin).json()
               if u["username"].lower() == "alice") == 1


@requires_db
def test_two_creations_cannot_both_pass_the_check(client, db):
    """The check and the insert are one step: a creation waits for another
    one in flight, so "alice" and "Alice" created at the same moment cannot
    both see the name free. Held here from a second connection, the lock
    must keep the request waiting until it is let go."""
    import threading

    from sqlalchemy import func, select

    from app.api.routes_admin import USERNAME_LOCK_CLASS
    from app.db import get_engine

    _user(db, "root", role="admin")
    admin = _auth(client, "root")
    answers: list = []

    def create() -> None:
        answers.append(client.post(
            "/admin/users", json={"username": "alice", "password": "firstpass1"}, headers=admin
        ))

    with get_engine().connect() as other:
        held = other.begin()
        other.execute(select(func.pg_advisory_xact_lock(USERNAME_LOCK_CLASS, 0)))
        creating = threading.Thread(target=create)
        creating.start()
        creating.join(timeout=1.0)
        assert creating.is_alive() and not answers, "a user was created while the lock was held"
        held.rollback()
    creating.join(timeout=10)
    assert [a.status_code for a in answers] == [201]


@requires_db
def test_an_account_created_before_the_rule_still_signs_in_by_its_own_name(client, db):
    """Two accounts that already differ only in case are not renamed or
    merged — each still signs in with exactly the name it was given."""
    _user(db, "Bob")
    _user(db, "bob", password="anotherpass1")
    assert client.get("/auth/me", headers=_auth(client, "Bob")).json()["username"] == "Bob"
    assert client.get(
        "/auth/me", headers=_auth(client, "bob", "anotherpass1")
    ).json()["username"] == "bob"


# ---------------------------------------------------------------------------
# Closing and reopening an account
# ---------------------------------------------------------------------------


@requires_db
def test_reopening_an_account_does_not_revive_its_old_sessions(client, db):
    """Closing an account ended its sessions only because the token check
    refuses an inactive account; reopening it brought every token issued
    before the close back to life — including one taken from whoever the
    account was closed to stop."""
    _user(db, "root", role="admin")
    admin = _auth(client, "root")
    alice = _user(db, "alice")
    stolen = _auth(client, "alice")
    before = alice.token_serial or 0

    assert client.post(f"/admin/users/{alice.id}/active?active=false",
                       headers=admin).json() == {"active": False}
    db.refresh(alice)
    assert (alice.token_serial or 0) == before + 1
    assert client.get("/auth/me", headers=stolen).status_code == 401

    assert client.post(f"/admin/users/{alice.id}/active?active=true",
                       headers=admin).json() == {"active": True}
    db.refresh(alice)
    assert (alice.token_serial or 0) == before + 2
    assert client.get("/auth/me", headers=stolen).status_code == 401
    # Reopened means she can sign in again, with a new token.
    assert client.get("/auth/me", headers=_auth(client, "alice")).status_code == 200


@requires_db
def test_setting_an_account_to_the_state_it_is_in_signs_nobody_out(client, db):
    _user(db, "root", role="admin")
    admin = _auth(client, "root")
    alice = _user(db, "alice")
    session = _auth(client, "alice")
    assert client.post(f"/admin/users/{alice.id}/active?active=true",
                       headers=admin).json() == {"active": True}
    assert client.get("/auth/me", headers=session).status_code == 200


# ---------------------------------------------------------------------------
# The queue as each account sees it
# ---------------------------------------------------------------------------


def _job(db, project_id: str | None, state: str = "queued") -> None:
    db.add(Job(project_id=project_id, kind="export", state=state))
    db.commit()


@requires_db
def test_an_editor_sees_their_own_queue_and_no_machine(client, db):
    """Every editor used to read the whole system's job counts and both
    machines' disks: how busy the service is and how full its disks are is
    the operator's business. An editor's question is about their own work —
    is it waiting, and is anything alive to do it."""
    alice = _user(db, "alice")
    bob = _user(db, "bob")
    mine = Project(owner_id=alice.id, name="a", doc={})
    theirs = Project(owner_id=bob.id, name="b", doc={})
    db.add_all([mine, theirs])
    db.commit()
    _job(db, mine.id)
    _job(db, mine.id, state="done")
    for _ in range(3):
        _job(db, theirs.id)
    _job(db, None)  # a system job belongs to nobody's view but the admin's

    body = client.get("/jobs/queue", headers=_auth(client, "alice")).json()
    assert body["scope"] == "own"
    assert body["waiting"] == 1
    assert body["counts"] == {"queued": 1, "done": 1}
    # No worker has reported and nothing is running: her waiting job is stuck.
    assert body["stalled"] is True
    for key in ("api_disk", "worker_disk", "live_workers", "worker_seen_at"):
        assert key not in body, key

    # Nothing of hers waiting: whatever is stuck is not hers to be told about.
    carol = _user(db, "carol")
    assert carol
    quiet = client.get("/jobs/queue", headers=_auth(client, "carol")).json()
    assert quiet == {"scope": "own", "counts": {}, "waiting": 0, "stalled": False, "unserved_lanes": []}


@requires_db
def test_the_admin_sees_the_whole_queue_and_both_disks(client, db):
    alice = _user(db, "alice")
    _user(db, "root", role="admin")
    mine = Project(owner_id=alice.id, name="a", doc={})
    db.add(mine)
    db.commit()
    _job(db, mine.id)
    _job(db, None)

    body = client.get("/jobs/queue", headers=_auth(client, "root")).json()
    assert body["scope"] == "all"
    assert body["waiting"] == 2
    assert "free_bytes" in body["api_disk"]
    assert body["worker_disk"] is None
    assert "live_workers" in body and "worker_seen_at" in body


# ---------------------------------------------------------------------------
# A carriage return in a burned subtitle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["a\r\nb", "a\rb", "a\nb"])
def test_a_carriage_return_never_reaches_the_subtitle_script(text):
    """Text pasted from Windows carries \\r\\n; the \\n became ASS's \\N and the
    \\r stayed raw inside the Dialogue line, where a reader that ends lines
    at \\r cuts the event in two and drops the rest of the text."""
    doc = build_ass_document([Segment(id="1", start=0.0, end=1.0, text=text)], SubtitleStyle())
    assert "\r" not in doc
    dialogue = [line for line in doc.split("\n") if line.startswith("Dialogue:")]
    assert len(dialogue) == 1 and dialogue[0].endswith(r"a\Nb")


# ---------------------------------------------------------------------------
# The schema browser in production
# ---------------------------------------------------------------------------


def _status_in(environment: str, path: str) -> int:
    script = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        f"print(TestClient(app).get({path!r}).status_code)\n"
    )
    env = {**os.environ, "ENVIRONMENT": environment, "PYTHONDONTWRITEBYTECODE": "1"}
    out = subprocess.run(
        [sys.executable, "-c", script], cwd=BACKEND, env=env, capture_output=True, text=True,
        timeout=60, check=True,
    )
    return int(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("environment", ["development", "Development", "local"])
def test_a_development_server_keeps_its_schema(environment):
    assert _status_in(environment, "/openapi.json") == 200
    assert _status_in(environment, "/docs") == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_production_publishes_no_schema(path):
    """The schema lists every admin route and its parameters to anyone who
    asks. The admin screens are in the app now; nothing in production reads
    it. Run in a process of its own with the environment set, so the answer
    does not depend on the shell or the .env this suite runs under."""
    assert _status_in("production", path) == 404


@pytest.mark.parametrize("environment", ["staging", "prodution", ""])
def test_any_other_environment_publishes_no_schema(environment):
    """Open only where it was asked for: a misspelt or new environment name
    is treated as production, not as a development server."""
    assert _status_in(environment, "/openapi.json") == 404
