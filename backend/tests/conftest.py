"""Shared test setup.

Runs before any test module imports app code, because several modules call
get_logger() at import time and a few read settings at import time too.

A REAL Postgres is required, not SQLite: the project document is JSONB, and
the queue's claim path is `SELECT ... FOR UPDATE SKIP LOCKED`. Neither exists
in SQLite, so a SQLite suite would pass while the two things most worth
testing went unexercised.

    createdb monireels_test
    DATABASE_URL=postgresql://.../monireels_test pytest
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("WORK_DIR", tempfile.mkdtemp(prefix="monireels_test_"))
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-key")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
# Off unless a test opts in: the R2 client would otherwise try to sign real
# requests against a nonexistent account.
os.environ.pop("R2_ACCESS_KEY_ID", None)
os.environ.pop("R2_SECRET_ACCESS_KEY", None)
os.environ.pop("R2_BUCKET", None)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import get_engine, get_session_factory  # noqa: E402
from app.dbmodels import Base  # noqa: E402

DB_CONFIGURED = bool(os.environ.get("DATABASE_URL"))
requires_db = pytest.mark.skipif(not DB_CONFIGURED, reason="DATABASE_URL is not set")


@pytest.fixture(scope="session", autouse=True)
def _schema():
    if not DB_CONFIGURED:
        yield
        return
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(autouse=True)
def _clean_tables(request):
    """Every DB test starts from empty tables.

    The list comes from the metadata, not from a literal here. A hand-written
    one has to be remembered every time a table is added, and forgetting it
    does not fail loudly — rows leak into the next test and it passes or
    fails depending on the ORDER the suite happened to run in.

    `sorted_tables` is in dependency order (parents first), so reversed puts
    children first and the deletes never trip a foreign key. TRUNCATE
    ... CASCADE would also work, and would also silently empty tables a test
    did not expect to lose.
    """
    if not DB_CONFIGURED or "db" not in request.fixturenames:
        yield
        return
    yield
    engine = get_engine()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'DELETE FROM "{table.name}"'))


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    from app import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()
