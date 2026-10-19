"""Run Alembic migrations from inside the application.

Called by the API service at startup so a deployment cannot serve against a
schema it has not been migrated to. Guarded by a Postgres advisory lock:
Railway can start several instances at once, and two `alembic upgrade head`
runs racing on the same database is how a half-applied migration happens.

The worker deliberately does NOT migrate. One writer is enough, and a worker
that migrates would race the API on every deploy. It WAITS instead
(schema_state): Railway starts both services of a deploy at once, and the
worker that came up first claimed against the old schema until the API had
migrated it.
"""
from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Arbitrary but fixed: any value works as long as every instance uses the
# same one.
_LOCK_ID = 8021977


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "app" / "migrations"))
    # This process already has its logging (setup_logging, uvicorn); env.py
    # must not replace it with alembic.ini's. See app/migrations/env.py.
    config.attributes["configure_logger"] = False
    return config


LOCK_POLL_S = 2.0


def _take_lock(conn, wait_s: float) -> None:
    """Wait for the migration lock, but not forever.

    An instance that loses the race must WAIT for the migration rather than
    start serving against the old schema — so it polls. It used to block in
    `pg_advisory_lock` with no limit: a lock left by a stuck instance was a
    deployment that hung with nothing in its log. Now it gives up with a
    reason, and the platform's restart tries again.
    """
    deadline = time.monotonic() + wait_s
    while True:
        if conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": _LOCK_ID}).scalar():
            conn.commit()
            return
        conn.commit()
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Another instance has held the migration lock for over {wait_s:.0f}s. "
                "Check for a stuck API instance (pg_locks, advisory lock "
                f"{_LOCK_ID}) before deploying again."
            )
        time.sleep(LOCK_POLL_S)


def upgrade_to_head() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        _take_lock(conn, get_settings().migration_lock_wait_s)
        try:
            logger.info("Running database migrations")
            command.upgrade(_alembic_config(), "head")
            logger.info("Database is up to date")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
            conn.commit()


def schema_state() -> tuple[str, set[str], set[str]]:
    """Where the database is against this code's migrations.

    Returns (state, the database's revisions, the code's heads), the state
    one of:

    - "ready": at this code's head.
    - "behind": not migrated yet — an older revision, or none at all (a new
      database the API has not reached yet). The API is about to migrate it;
      a worker waits.
    - "ahead": a revision this code does not know: the database was
      migrated by NEWER code, so this is a rollback. Migrations here only
      add, so older code runs on a newer schema — and a worker that waited
      for it would wait forever.
    """
    script = ScriptDirectory.from_config(_alembic_config())
    heads = set(script.get_heads())
    with get_engine().connect() as conn:
        current = set(MigrationContext.configure(conn).get_current_heads())
    if current == heads:
        return "ready", current, heads
    for revision in current:
        try:
            script.get_revision(revision)
        except CommandError:
            return "ahead", current, heads
    return "behind", current, heads
