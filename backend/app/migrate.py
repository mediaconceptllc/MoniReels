"""Run Alembic migrations from inside the application.

Called by the API service at startup so a deployment cannot serve against a
schema it has not been migrated to. Guarded by a Postgres advisory lock:
Railway can start several instances at once, and two `alembic upgrade head`
runs racing on the same database is how a half-applied migration happens.

Two startups it used to refuse (audit OPS-2):

- **A rollback.** After a deploy that migrated, the previous code ran
  `upgrade head` with a script directory that does not know the database's
  revision — "Can't locate revision" — and the API would not start at all,
  the one moment a working build is needed most. A database AHEAD of the
  code is now left as it is: migrations here only ADD (see the
  expand/contract rule in docs/DEPLOY.md, held by tests/test_rollback.py),
  so older code runs on the newer schema.
- **A database that is briefly down.** The first refused connection killed
  the process; the platform's few restarts were spent in seconds. Startup
  now retries for `DB_STARTUP_WAIT_S`, then exits with the reason.

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
from sqlalchemy.exc import OperationalError

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


# Startup's retry delays: doubling from the first, never longer than the
# last, until DB_STARTUP_WAIT_S runs out.
DB_RETRY_FIRST_S = 1.0
DB_RETRY_MAX_S = 10.0


def _reason(exc: Exception) -> str:
    """The driver's own first line ("connection refused", "password
    authentication failed for user …"): no URL, no password, no SQLAlchemy
    boilerplate."""
    orig = getattr(exc, "orig", None) or exc
    return (str(orig).strip().splitlines() or [type(orig).__name__])[0]


def wait_for_database(wait_s: float) -> None:
    """Return once a connection succeeds; raise once `wait_s` has passed.

    The raise is the point as much as the wait: an API that never reaches
    its database must still EXIT — with the reason in the log — so the
    platform's restart and its failed-deploy signal both still happen.
    """
    deadline = time.monotonic() + wait_s
    delay = DB_RETRY_FIRST_S
    attempt = 0
    while True:
        attempt += 1
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except OperationalError as exc:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError(
                    f"Could not reach the database in {wait_s:.0f}s ({attempt} attempts): {_reason(exc)}. "
                    "Check DATABASE_URL and that Postgres is running; the platform's restart tries again."
                ) from exc
            pause = min(delay, left)
            logger.warning(
                "Database not reachable yet (attempt %d): %s — retrying in %.0fs",
                attempt, _reason(exc), pause,
            )
            time.sleep(pause)
            delay = min(delay * 2, DB_RETRY_MAX_S)
            continue
        if attempt > 1:
            logger.info("Database reachable after %d attempts", attempt)
        return


def upgrade_to_head() -> str:
    """API startup: reach the database, then bring it to this code's head.

    Returns the state it found (see schema_state). "behind" is migrated;
    "ready" is left alone; "ahead" — a rollback — is left alone with a
    warning instead of failing on a revision this code has never heard of.
    The state is read UNDER the lock: an instance that waited for another
    one's migration must see what that one did, not what it saw before.
    """
    settings = get_settings()
    wait_for_database(settings.db_startup_wait_s)
    engine = get_engine()
    with engine.connect() as conn:
        _take_lock(conn, settings.migration_lock_wait_s)
        try:
            state, current, heads = schema_state()
            now, head = ", ".join(sorted(current)) or "nothing", ", ".join(sorted(heads))
            if state == "ahead":
                logger.warning(
                    "The database is at %s, newer than this code's %s: not migrating (a rollback?). "
                    "Migrations only add, so this code runs on the newer schema — see docs/DEPLOY.md.",
                    now, head,
                )
            elif state == "ready":
                logger.info("Database is up to date (%s)", head)
            else:
                logger.info("Running database migrations (%s -> %s)", now, head)
                command.upgrade(_alembic_config(), "head")
                logger.info("Database is up to date")
            return state
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
