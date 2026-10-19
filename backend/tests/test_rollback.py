"""The API starts on a database AHEAD of its code, and waits out one that is
briefly down (audit OPS-2).

Both used to be refusals. After a deploy that migrated, the previous build
ran `alembic upgrade head` against a revision its script directory did not
know — "Can't locate revision" — and would not start, the one moment a
working build is needed most. And the first refused connection at startup
killed the process, so a database restarting with a deploy spent the
platform's few restarts in seconds.

The test database is built by create_all, with no alembic_version table:
each test stamps the revision it needs and drops the table afterwards.
"""
from __future__ import annotations

import ast
import logging
import threading
import time
from pathlib import Path

import pytest
from alembic.util.exc import CommandError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.video.capabilities import Capabilities
from tests.conftest import must_finish, requires_db

VERSIONS = Path(__file__).resolve().parents[1] / "app" / "migrations" / "versions"
NEWER = "9999_from_newer_code"
# A socket that is not there: refused at once, and the URL carries a
# password the error must never repeat.
UNREACHABLE = "postgresql://monireels:s3cret-pw@/nodb?host=/var/run/postgresql&port=1"


@pytest.fixture
def head():
    from alembic.script import ScriptDirectory

    from app.migrate import _alembic_config

    (only,) = ScriptDirectory.from_config(_alembic_config()).get_heads()
    return only


@pytest.fixture
def stamp():
    from app.db import get_engine

    engine = get_engine()

    def _stamp(revision: str | None) -> None:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
            if revision is not None:
                conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
                conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})

    yield _stamp
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def upgrades(monkeypatch):
    """`command.upgrade`, recorded instead of run: on a create_all database a
    real upgrade would fail creating tables that are already there."""
    from app import migrate

    calls: list[str] = []
    monkeypatch.setattr(migrate.command, "upgrade", lambda _cfg, revision: calls.append(revision))
    return calls


def _api(monkeypatch):
    import app.main as main

    async def _caps(_ffmpeg):
        return Capabilities(ffmpeg_available=False, ffmpeg_version="", xfade_transitions=[])

    monkeypatch.setattr(main, "build_capabilities", _caps)
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    get_settings.cache_clear()
    return main.app


# ---------------------------------------------------------------------------
# A database ahead of the code
# ---------------------------------------------------------------------------


@requires_db
def test_the_old_way_could_not_start_on_a_newer_database(stamp):
    """The failure, as it was: `upgrade head` from code that does not know
    the database's revision."""
    from alembic import command

    from app.migrate import _alembic_config

    stamp(NEWER)
    with pytest.raises(CommandError, match="Can't locate revision"):
        command.upgrade(_alembic_config(), "head")


@requires_db
def test_a_rollback_starts_on_the_newer_database(stamp, upgrades, caplog):
    from app import migrate

    stamp(NEWER)
    with caplog.at_level(logging.WARNING, logger="app.migrate"):
        assert migrate.upgrade_to_head() == "ahead"
    assert upgrades == []
    assert any("newer than this code" in r.getMessage() for r in caplog.records)


@requires_db
def test_the_api_serves_on_a_newer_database(stamp, monkeypatch):
    """Through the real lifespan and the real alembic: nothing patched that
    would hide the old "Can't locate revision"."""
    stamp(NEWER)
    app = _api(monkeypatch)
    try:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    finally:
        get_settings.cache_clear()


@requires_db
def test_a_database_behind_is_still_migrated(stamp, upgrades):
    from app import migrate

    stamp("0003_usage_events")
    assert migrate.upgrade_to_head() == "behind"
    stamp(None)  # a new database the API reaches first
    assert migrate.upgrade_to_head() == "behind"
    assert upgrades == ["head", "head"]


@requires_db
def test_a_database_at_head_is_left_alone(stamp, upgrades, head):
    from app import migrate

    stamp(head)
    assert migrate.upgrade_to_head() == "ready"
    assert upgrades == []


@requires_db
def test_the_state_is_read_under_the_lock(stamp, upgrades, head, monkeypatch):
    """Two instances of a deploy: the one that waited for the lock must see
    the other's migration, not the state from before it started waiting."""
    from app import migrate
    from app.db import get_engine

    monkeypatch.setattr(migrate, "LOCK_POLL_S", 0.05)
    stamp("0003_usage_events")
    holder = get_engine().connect()
    holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": migrate._LOCK_ID})
    holder.commit()
    result: list[str] = []
    waiting = threading.Thread(target=lambda: result.append(migrate.upgrade_to_head()))
    try:
        waiting.start()
        time.sleep(0.3)  # it is polling for the lock now
        stamp(head)  # the other instance migrates...
    finally:
        holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": migrate._LOCK_ID})
        holder.commit()
        holder.close()  # ...and lets go
    waiting.join(timeout=10)
    assert result == ["ready"]
    assert upgrades == []


# ---------------------------------------------------------------------------
# A database that is briefly down
# ---------------------------------------------------------------------------


def _engines(monkeypatch, *, bad_first: int | None):
    """migrate.get_engine answering an unreachable engine `bad_first` times
    (None: always), then the real one. Returns the call counter."""
    from app import db, migrate

    bad = create_engine(UNREACHABLE)
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        if bad_first is None or calls["n"] <= bad_first:
            return bad
        return db.get_engine()

    monkeypatch.setattr(migrate, "get_engine", fake)
    monkeypatch.setattr(migrate, "DB_RETRY_FIRST_S", 0.02)
    return calls


@requires_db
def test_a_database_that_comes_back_is_waited_for(monkeypatch, caplog):
    from app import migrate

    calls = _engines(monkeypatch, bad_first=2)
    with caplog.at_level(logging.INFO, logger="app.migrate"):
        migrate.wait_for_database(5)
    assert calls["n"] == 3
    notes = [r.getMessage() for r in caplog.records]
    assert sum("not reachable yet" in m for m in notes) == 2
    assert any("reachable after 3 attempts" in m for m in notes)


def test_a_database_that_never_comes_back_stops_with_the_reason(monkeypatch):
    from app import migrate

    calls = _engines(monkeypatch, bad_first=None)
    with must_finish("wait_for_database(0.3)", 3):
        with pytest.raises(RuntimeError, match="Could not reach the database") as raised:
            migrate.wait_for_database(0.3)
    assert calls["n"] >= 2, "it retried before giving up"
    message = str(raised.value)
    assert "s3cret-pw" not in message and "DATABASE_URL" in message


def test_the_api_exits_when_the_database_never_answers(monkeypatch):
    """The platform must still see a failed start: a process that hung or
    came up without its database would pass for healthy."""
    _engines(monkeypatch, bad_first=None)
    monkeypatch.setenv("DB_STARTUP_WAIT_S", "0")
    app = _api(monkeypatch)
    try:
        with must_finish("the API's startup", 5):
            with pytest.raises(RuntimeError, match="Could not reach the database"):
                with TestClient(app):
                    pass
    finally:
        get_settings.cache_clear()


def test_every_connection_has_a_connect_timeout(monkeypatch):
    """Without one a host that drops packets holds a connect for the kernel's
    TCP timeout, past the startup bound."""
    from app import db

    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/x")
    monkeypatch.setenv("DB_CONNECT_TIMEOUT_S", "7")
    get_settings.cache_clear()
    monkeypatch.setattr(db, "_engine", None)
    captured = {}
    monkeypatch.setattr(db, "create_engine", lambda *a, **kw: captured.update(kw) or object())
    try:
        db.get_engine()
    finally:
        get_settings.cache_clear()
    assert captured["connect_args"]["connect_timeout"] == 7


# ---------------------------------------------------------------------------
# The rule that makes a rollback safe: migrations only add
# ---------------------------------------------------------------------------


def destructive_ops(source: str) -> list[str]:
    """What in a migration's upgrade() would break the PREVIOUS code running
    on the migrated schema (the rollback above): dropping or renaming what it
    reads, or a NOT NULL column it does not know to fill.

    A migration that does this on purpose — the CONTRACT half of a change,
    shipped a release after the code stopped using the thing — says so with
    a module-level `CONTRACT = "<why>"`.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "CONTRACT" for t in node.targets
        ):
            return []
    upgrade = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
    )
    if upgrade is None:
        return []
    found = []
    for call in ast.walk(upgrade):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        name = call.func.attr
        kwargs = {k.arg: k.value for k in call.keywords}
        if name in {"drop_table", "drop_column", "rename_table", "execute"}:
            found.append(name)
        elif name == "alter_column" and (
            "new_column_name" in kwargs
            or "type_" in kwargs
            or (isinstance(kwargs.get("nullable"), ast.Constant) and kwargs["nullable"].value is False)
        ):
            found.append("alter_column")
        elif name == "add_column":
            for column in (a for a in call.args if isinstance(a, ast.Call)):
                ckw = {k.arg: k.value for k in column.keywords}
                not_null = isinstance(ckw.get("nullable"), ast.Constant) and ckw["nullable"].value is False
                if not_null and "server_default" not in ckw:
                    found.append("add_column NOT NULL without server_default")
    return found


def test_every_migration_only_adds():
    offenders = {
        path.name: ops
        for path in sorted(VERSIONS.glob("*.py"))
        if (ops := destructive_ops(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        "A migration removes or tightens what the previous release still uses, so rolling the "
        f"code back would break: {offenders}. Split it (expand now, contract a release later) "
        "and mark the contract half with CONTRACT = '...' — see docs/DEPLOY.md."
    )


@pytest.mark.parametrize(
    ("body", "flagged"),
    [
        ("op.drop_column('jobs', 'lane')", ["drop_column"]),
        ("op.drop_table('jobs')", ["drop_table"]),
        ("op.alter_column('jobs', 'lane', new_column_name='queue')", ["alter_column"]),
        ("op.alter_column('jobs', 'lane', nullable=False)", ["alter_column"]),
        ("op.execute('ALTER TABLE jobs DROP COLUMN lane')", ["execute"]),
        (
            "op.add_column('jobs', sa.Column('x', sa.Integer(), nullable=False))",
            ["add_column NOT NULL without server_default"],
        ),
        ("op.add_column('jobs', sa.Column('x', sa.Integer(), nullable=False, server_default='0'))", []),
        ("op.add_column('jobs', sa.Column('x', sa.Integer(), nullable=True))", []),
        ("op.create_index('ix', 'jobs', ['lane'])", []),
    ],
)
def test_the_rule_sees_what_breaks_a_rollback(body, flagged):
    assert destructive_ops(f"def upgrade():\n    {body}\n") == flagged
    contract = f"CONTRACT = 'the code stopped using it in 2.3'\ndef upgrade():\n    {body}\n"
    assert destructive_ops(contract) == []
