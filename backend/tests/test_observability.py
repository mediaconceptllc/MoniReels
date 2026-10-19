"""Which request or job a line belongs to, who changed what, and the limits
every database connection starts with (OPS-3)."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import observability
from app.dbmodels import AuditLog, Job, Project, User
from app.jobs import queue
from app.security import hash_password
from app.utils import logging as app_logging
from app.video.capabilities import Capabilities
from tests.conftest import requires_db


def _capture(fmt: str = "json") -> tuple[logging.Handler, io.StringIO]:
    """A handler wired exactly as setup_logging wires stdout's, writing to a
    buffer: the filter that stamps the ids lives on the handler, so caplog's
    own handler would never see them."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.addFilter(app_logging._Context())
    handler.setFormatter(
        app_logging.JsonFormatter() if fmt == "json" else app_logging._TextFormatter("%(message)s")
    )
    return handler, buffer


@pytest.fixture
def captured():
    handler, buffer = _capture()
    root = logging.getLogger()
    saved = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield buffer
    root.removeHandler(handler)
    root.setLevel(saved)


def _lines(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Log lines
# ---------------------------------------------------------------------------


def test_a_json_line_carries_its_level_ids_and_traceback(captured):
    token = app_logging.request_id.set("req-12345678")
    try:
        logging.getLogger("app.test").info("Сайн уу %s", 1)
        try:
            raise ValueError("boom")
        except ValueError:
            logging.getLogger("app.test").exception("it broke")
    finally:
        app_logging.request_id.reset(token)

    info, error = _lines(captured)
    assert info["message"] == "Сайн уу 1"  # Cyrillic kept, not \u-escaped
    assert info["level"] == "info"  # lowercase: what the platform files by
    assert info["request_id"] == "req-12345678"
    assert "job_id" not in info  # absent, not null, when there is none
    assert error["level"] == "error"
    assert "ValueError: boom" in error["exception"]


def test_the_text_format_keeps_the_ids_too():
    handler, buffer = _capture("text")
    log = logging.getLogger("app.test.text")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    token = app_logging.job_id.set("job42")
    try:
        log.info("hello")
    finally:
        app_logging.job_id.reset(token)
        log.removeHandler(handler)
    assert buffer.getvalue().strip() == "hello [job_id=job42]"


def test_setup_logging_picks_the_format_from_the_environment(monkeypatch):
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        for value, expected in (("text", app_logging._TextFormatter), ("", app_logging.JsonFormatter)):
            monkeypatch.setenv("LOG_FORMAT", value)
            monkeypatch.setattr(app_logging, "_CONFIGURED", False)
            app_logging.setup_logging()
            assert isinstance(root.handlers[0].formatter, expected)

        # The ids come from the handler setup_logging installs — the one the
        # platform reads — not only from a handler a test wires by hand.
        handler = root.handlers[0]
        token = app_logging.request_id.set("req-from-setup")
        try:
            record = logging.LogRecord("app.x", logging.INFO, __file__, 1, "hello", None, None)
            assert handler.filter(record)
            assert json.loads(handler.format(record))["request_id"] == "req-from-setup"
        finally:
            app_logging.request_id.reset(token)
    finally:
        root.handlers = saved
        app_logging._CONFIGURED = True


@pytest.mark.parametrize(
    "offered, kept",
    [
        ("abcdef12", True),
        ("trace.id-01_AB", True),
        ("short", False),
        ("x" * 65, False),
        ("bad id with spaces", False),
        ("evil\r\nSet-Cookie: a=b", False),
        (None, False),
    ],
)
def test_a_callers_request_id_is_kept_only_when_it_looks_like_one(offered, kept):
    rid = observability.new_request_id(offered)
    assert (rid == offered) is kept
    assert observability._REQUEST_ID_RE.match(rid)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(db):
    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    app.state.capabilities = Capabilities(
        ffmpeg_available=True, ffmpeg_version="6.1", xfade_transitions=["fade"]
    )
    app.state.ffmpeg = None
    return TestClient(app, raise_server_exceptions=False)


def _user(db, username: str, role: str = "editor") -> User:
    salt, digest = hash_password("hunter2hunter2")
    user = User(username=username, pw_salt=salt, pw_hash=digest, role=role)
    db.add(user)
    db.commit()
    return user


def _auth(client: TestClient, username: str) -> dict:
    response = client.post("/auth/login", json={"username": username, "password": "hunter2hunter2"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _audit(db) -> list[AuditLog]:
    db.expire_all()
    return db.query(AuditLog).order_by(AuditLog.id).all()


@requires_db
def test_every_response_names_its_request_and_the_log_line_does_too(client, captured):
    response = client.get("/health/nope", headers={"X-Request-ID": "trace-from-the-page"})
    assert response.headers["X-Request-ID"] == "trace-from-the-page"

    fresh = client.get("/capabilities")
    assert observability._REQUEST_ID_RE.match(fresh.headers["X-Request-ID"])
    assert fresh.headers["X-Request-ID"] != "trace-from-the-page"

    request_lines = [line for line in _lines(captured) if line["message"].startswith("GET /health/nope")]
    assert [line.get("request_id") for line in request_lines] == ["trace-from-the-page"]
    # And nothing leaks into code running outside any request.
    assert app_logging.request_id.get() is None


@requires_db
def test_the_request_id_is_readable_by_the_page(client):
    response = client.get("/capabilities", headers={"Origin": "http://localhost:3000"})
    assert "x-request-id" in response.headers.get("access-control-expose-headers", "").lower()


@requires_db
def test_a_500_names_its_request_carries_cors_and_hides_the_exception(client, captured, monkeypatch):
    """Starlette's own 500 was the text "Internal Server Error", produced
    outside CORS: the page saw a network error and the person had nothing
    to quote."""
    from app.main import app

    @app.get("/__boom")
    def boom():
        raise RuntimeError("secret path /srv/key.pem")

    try:
        response = client.get(
            "/__boom", headers={"Origin": "http://localhost:3000", "X-Request-ID": "rid-500-test"}
        )
    finally:
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/__boom"]

    assert response.status_code == 500
    assert response.json() == {"detail": "Серверт алдаа гарлаа. Лавлах дугаар: rid-500-test"}
    assert response.headers["X-Request-ID"] == "rid-500-test"
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "secret path" not in response.text
    logged = [line for line in _lines(captured) if line["level"] == "error"]
    assert logged and logged[0]["request_id"] == "rid-500-test"
    assert "secret path /srv/key.pem" in logged[0]["exception"]


@requires_db
def test_admin_changes_leave_an_audit_row_and_reads_do_not(client, db):
    _user(db, "boss", role="admin")
    headers = _auth(client, "boss")

    assert client.get("/admin/users", headers=headers).status_code == 200
    made = client.post(
        "/admin/users",
        headers=headers,
        json={"username": "newbie", "password": "longenough1", "role": "editor"},
    )
    assert made.status_code == 201

    rows = _audit(db)
    assert [(r.user, r.method, r.path, r.status) for r in rows] == [("boss", "POST", "/admin/users", 201)]
    assert rows[0].ip  # the client address the request log uses


@requires_db
def test_a_refused_admin_change_is_recorded_with_its_status(client, db):
    _user(db, "plain")
    headers = _auth(client, "plain")
    assert (
        client.post(
            "/admin/users", headers=headers, json={"username": "x" * 5, "password": "longenough1"}
        ).status_code
        == 403
    )
    assert (
        client.post("/admin/users", json={"username": "x" * 5, "password": "longenough1"}).status_code == 401
    )

    assert [(r.user, r.status) for r in _audit(db)] == [("plain", 403), ("", 401)]


@requires_db
def test_a_password_change_is_recorded_without_the_password(client, db):
    _user(db, "changer")
    headers = _auth(client, "changer")
    response = client.post(
        "/auth/password",
        headers=headers,
        json={"current_password": "hunter2hunter2", "new_password": "a-new-long-password"},
    )
    assert response.status_code == 200, response.text
    (row,) = _audit(db)
    assert (row.user, row.method, row.path, row.status) == ("changer", "POST", "/auth/password", 200)
    assert "a-new-long-password" not in repr(vars(row))


@requires_db
def test_project_work_is_not_audited(client, db):
    _user(db, "maker")
    headers = _auth(client, "maker")
    client.post(
        "/projects",
        headers=headers,
        json={"name": "p", "filename": "a.mp4", "size_bytes": 10, "language": "mn"},
    )
    assert _audit(db) == []


@requires_db
def test_a_failed_audit_write_never_fails_the_request(client, db, monkeypatch):
    _user(db, "boss2", role="admin")
    headers = _auth(client, "boss2")

    def broken(**_):
        raise RuntimeError("database gone")

    monkeypatch.setattr("app.db.session_scope", broken)
    response = client.post(
        "/admin/users",
        headers=headers,
        json={"username": "other", "password": "longenough1", "role": "editor"},
    )
    assert response.status_code == 201


@requires_db
def test_old_audit_rows_are_purged_and_recent_ones_kept(db):
    now = time.time()
    db.add_all([AuditLog(at=now - 400 * 86400, path="/admin/old"), AuditLog(at=now - 10, path="/admin/new")])
    db.commit()
    assert observability.purge_audit(365) == 1
    assert [r.path for r in _audit(db)] == ["/admin/new"]


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


@requires_db
def test_a_jobs_log_lines_carry_its_id(db, captured, monkeypatch):
    from app import worker

    salt, digest = hash_password("pw")
    owner = User(username="jobowner", pw_salt=salt, pw_hash=digest)
    db.add(owner)
    db.flush()
    project = Project(owner_id=owner.id, name="p", doc={})
    db.add(project)
    db.commit()

    async def handler(handle):
        logging.getLogger("app.test.handler").info("inside the handler")
        # Threads it hands work to carry the id too (to_thread copies context).
        await asyncio.to_thread(logging.getLogger("app.test.thread").info, "inside a thread")
        return {}

    monkeypatch.setitem(worker.HANDLERS, "suggest", handler)
    job_id = queue.enqueue("suggest", project_id=project.id)
    db.expire_all()
    job = db.get(Job, job_id)
    job.state = "running"
    db.commit()
    db.refresh(job)

    asyncio.run(worker._run_job(job))

    mine = [line for line in _lines(captured) if line["message"].startswith("inside")]
    assert [(line["job_id"], line["job_kind"]) for line in mine] == [(job_id, "suggest")] * 2


@requires_db
def test_an_idle_worker_that_reported_recently_is_not_called_down(db):
    """An idle worker counted as dead: a job it was not yet allowed to take
    read as "the worker service is down"."""
    from app import worker_report

    queue.enqueue("suggest", project_id=None)

    assert queue.queue_overview(db=db)["stalled"] is True  # no sign of life at all

    worker_report.publish(db, worker_report.WorkerReport(separation=False, model="m", at=time.time()))
    db.commit()
    overview = queue.queue_overview(db=db)
    assert overview["stalled"] is False
    assert overview["worker_seen_at"] == pytest.approx(time.time(), abs=5)

    worker_report.publish(
        db,
        worker_report.WorkerReport(
            separation=False, model="m", at=time.time() - queue.WORKER_REPORT_STALE_SEC - 1
        ),
    )
    db.commit()
    assert queue.queue_overview(db=db)["stalled"] is True  # three missed reports: gone


# ---------------------------------------------------------------------------
# Database limits
# ---------------------------------------------------------------------------


@requires_db
def test_every_app_connection_starts_with_its_limits(db):
    values = {
        name: db.execute(text(f"SHOW {name}")).scalar()
        for name in ("statement_timeout", "lock_timeout", "idle_in_transaction_session_timeout")
    }
    assert values == {
        "statement_timeout": "1min",
        "lock_timeout": "15s",
        "idle_in_transaction_session_timeout": "5min",
    }


def test_a_zero_limit_means_none():
    from app.config import Settings
    from app.db import session_options

    options = session_options(
        Settings(db_statement_timeout_ms=0, db_lock_timeout_ms=0, db_idle_in_transaction_timeout_ms=0)
    )
    assert options == "-c statement_timeout=0 -c lock_timeout=0 -c idle_in_transaction_session_timeout=0"


@requires_db
def test_a_held_migration_lock_is_waited_for_not_forever(monkeypatch):
    """It blocked in pg_advisory_lock with no limit: a lock left by a stuck
    instance was a deployment that hung with nothing in its log."""
    from app import migrate
    from app.db import get_engine

    monkeypatch.setattr(migrate, "LOCK_POLL_S", 0.05)
    holder = get_engine().connect()
    holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": migrate._LOCK_ID})
    try:
        with get_engine().connect() as conn:
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="migration lock"):
                migrate._take_lock(conn, wait_s=0.3)
            assert time.monotonic() - started < 3
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": migrate._LOCK_ID})
        holder.close()

    with get_engine().connect() as conn:
        migrate._take_lock(conn, wait_s=0.3)  # free now: taken at once
        conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": migrate._LOCK_ID})
        conn.commit()


# ---------------------------------------------------------------------------
# Error reports
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_sentry(monkeypatch):
    calls: dict = {}
    module = types.ModuleType("sentry_sdk")
    module.init = lambda **kwargs: calls.setdefault("init", kwargs)
    module.set_tag = lambda key, value: calls.setdefault("tags", {}).__setitem__(key, value)
    monkeypatch.setitem(sys.modules, "sentry_sdk", module)
    return calls


def _settings_with(monkeypatch, **env):
    from app.config import get_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_error_reports_stay_off_without_a_dsn(fake_sentry, monkeypatch):
    _settings_with(monkeypatch, SENTRY_DSN="")
    try:
        assert observability.init_error_reports("api") is False
        assert fake_sentry == {}
    finally:
        from app.config import get_settings

        get_settings.cache_clear()


def test_error_reports_send_no_bodies_cookies_or_tokens(fake_sentry, monkeypatch):
    _settings_with(monkeypatch, SENTRY_DSN="https://key@example.invalid/1", RAILWAY_GIT_COMMIT_SHA="abc123")
    try:
        assert observability.init_error_reports("worker") is True
    finally:
        from app.config import get_settings

        get_settings.cache_clear()
    init = fake_sentry["init"]
    assert init["send_default_pii"] is False
    assert init["max_request_body_size"] == "never"
    assert init["release"] == "abc123"
    assert init["traces_sample_rate"] == 0.0
    assert fake_sentry["tags"] == {"service": "worker"}

    token = app_logging.job_id.set("job-7")
    try:
        event = init["before_send"](
            {
                "request": {
                    "headers": {"Authorization": "Bearer eyJ...", "Cookie": "a=b", "Accept": "*/*"},
                    "cookies": {"a": "b"},
                    "data": {"password": "hunter2"},
                }
            },
            {},
        )
    finally:
        app_logging.job_id.reset(token)
    assert event["request"]["headers"] == {
        "Authorization": "[Filtered]",
        "Cookie": "[Filtered]",
        "Accept": "*/*",
    }
    assert "cookies" not in event["request"] and "data" not in event["request"]
    assert event["tags"] == {"job_id": "job-7"}


def test_a_missing_sdk_does_not_stop_the_service(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # import raises ImportError
    _settings_with(monkeypatch, SENTRY_DSN="https://key@example.invalid/1")
    try:
        assert observability.init_error_reports("api") is False
    finally:
        from app.config import get_settings

        get_settings.cache_clear()


@pytest.mark.skipif(importlib.util.find_spec("sentry_sdk") is None, reason="sentry-sdk is not installed")
def test_error_reports_start_with_the_real_sdk_and_this_starlette():
    """The fakes above prove what is asked for, not that the real SDK takes
    it. Its integrations patch Starlette's internals, and sentry-sdk 2.19.2
    did not start at all under Starlette 1.x: it imported
    `starlette.templating`, which since 1.0 raises without jinja2 whenever
    markupsafe is installed — and alembic's Mako installs it (audit DEP-1).
    `init_error_reports` swallowed that, as it should, so the API ran on
    without a single report and nothing said so but one log line.

    In a child process: the SDK's patches are global and would outlive this
    test. The DSN points at a closed local port; nothing is sent."""
    script = (
        "import json, sentry_sdk\n"
        "from app.observability import init_error_reports\n"
        "started = init_error_reports('api')\n"
        "names = sorted(sentry_sdk.get_client().integrations) if started else []\n"
        "print(json.dumps({'started': started, 'integrations': names}))\n"
    )
    env = {**os.environ, "SENTRY_DSN": "http://public@127.0.0.1:9/1"}
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    reply = json.loads(done.stdout.strip().splitlines()[-1])
    assert reply["started"] is True, done.stderr
    assert {"fastapi", "starlette"} <= set(reply["integrations"]), reply
