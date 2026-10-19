"""The worker waits for the API's migration before its first claim.

Found in production on the deploy that added jobs.started_at: Railway
starts both services of a deploy together, the worker was up 11 s before
the API had migrated, and every claim and the housekeeping pass failed with
"column jobs.started_at does not exist" until it had.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import text

from tests.conftest import requires_db

HEAD = ("ready", {"0004"}, {"0004"})
BEHIND = ("behind", {"0003"}, {"0004"})


@pytest.fixture
def worker(monkeypatch):
    from app import worker

    monkeypatch.setattr(worker, "SCHEMA_POLL_SEC", 0.01)
    worker._shutdown.clear()
    yield worker
    worker._shutdown.clear()


def _states(monkeypatch, worker, *answers):
    """schema_state answers these in turn, then the last one for ever."""
    seen: list[str] = []

    def fake():
        answer = answers[min(len(seen), len(answers) - 1)]
        seen.append(answer[0])
        return answer

    monkeypatch.setattr(worker.migrate, "schema_state", fake)
    return seen


@pytest.mark.asyncio
async def test_no_claim_and_no_housekeeping_before_the_schema_is_migrated(monkeypatch, worker):
    seen = _states(monkeypatch, worker, BEHIND, BEHIND, BEHIND, HEAD)
    first_touch: list[list[str]] = []

    def claim(_worker_id, _lanes=None):
        first_touch.append(list(seen))
        worker._shutdown.set()
        return None

    def reap():
        first_touch.append(list(seen))
        return worker.queue.Reaped()

    monkeypatch.setattr(worker.queue, "claim", claim)
    monkeypatch.setattr(worker.queue, "reap_stale", reap)
    monkeypatch.setattr(worker.queue, "purge_old", lambda *_: 0)
    monkeypatch.setattr(worker, "purge_trash", lambda: 0)
    monkeypatch.setattr(worker, "clear_all_workdirs", lambda: None)
    monkeypatch.setattr(worker, "validate_registry", lambda _h: None)
    monkeypatch.setattr(worker, "POLL_IDLE_SEC", 0.01)

    await asyncio.wait_for(worker.main(), timeout=10)

    assert first_touch, "the loop never claimed"
    for touched in first_touch:
        assert touched == ["behind", "behind", "behind", "ready"]


@pytest.mark.asyncio
async def test_waiting_is_said_once_not_on_every_poll(monkeypatch, worker, caplog):
    _states(monkeypatch, worker, *([BEHIND] * 20), HEAD)
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        await asyncio.wait_for(worker._wait_for_schema(), timeout=5)
    waits = [r.getMessage() for r in caplog.records if "Waiting for the API" in r.getMessage()]
    assert waits == [
        "Waiting for the API to migrate the database to 0004 (it is at 0003) before claiming jobs"
    ]


@pytest.mark.asyncio
async def test_a_newer_database_is_a_rollback_and_does_not_hold_the_worker(monkeypatch, worker, caplog):
    seen = _states(monkeypatch, worker, ("ahead", {"0005"}, {"0004"}))
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        await asyncio.wait_for(worker._wait_for_schema(), timeout=5)
    assert seen == ["ahead"]
    assert any("newer than this code" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_unreachable_database_is_waited_out(monkeypatch, worker):
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("connection refused")
        return HEAD

    monkeypatch.setattr(worker.migrate, "schema_state", flaky)
    await asyncio.wait_for(worker._wait_for_schema(), timeout=5)
    assert calls == 3


@pytest.mark.asyncio
async def test_shutdown_ends_the_wait(monkeypatch, worker):
    seen = _states(monkeypatch, worker, BEHIND)
    waiting = asyncio.create_task(worker._wait_for_schema())
    await asyncio.sleep(0.05)
    worker._shutdown.set()
    await asyncio.wait_for(waiting, timeout=2)
    assert set(seen) == {"behind"}  # it never saw "ready"; shutdown alone ended it


@requires_db
def test_schema_state_against_a_real_database():
    from app import migrate
    from app.db import get_engine

    head = migrate.schema_state()[2]
    assert head, "the code has no migration head"
    engine = get_engine()

    def stamp(revision: str | None) -> tuple[str, set[str]]:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
            if revision is not None:
                conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)"))
                conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})
        state, current, _ = migrate.schema_state()
        return state, current

    try:
        # A new database the API has not reached: nothing to read yet.
        assert stamp(None) == ("behind", set())
        assert stamp("0003_usage_events") == ("behind", {"0003_usage_events"})
        (only,) = head
        assert stamp(only) == ("ready", {only})
        assert stamp("9999_from_newer_code") == ("ahead", {"9999_from_newer_code"})
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@requires_db
def test_reading_the_revision_is_quiet_and_a_real_migration_is_not(caplog):
    """A worker waiting out a deploy built a MigrationContext every poll, and
    each one logged "Context impl PostgresqlImpl." and "Will assume
    transactional DDL." — two lines every three seconds over the one that
    said what it was waiting for. A real migration keeps them."""
    from alembic.runtime.migration import MigrationContext

    from app import migrate
    from app.db import get_engine

    def alembic_lines() -> list[str]:
        return [r.getMessage() for r in caplog.records if r.name == "alembic.runtime.migration"]

    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        for _ in range(3):
            migrate.schema_state()
        assert alembic_lines() == []

        # What `command.upgrade` builds, in the same thread, after the reads.
        with get_engine().connect() as conn:
            MigrationContext.configure(conn)
        assert any(line.startswith("Context impl") for line in alembic_lines())
