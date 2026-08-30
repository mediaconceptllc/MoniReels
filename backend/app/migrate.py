"""Run Alembic migrations from inside the application.

Called by the API service at startup so a deployment cannot serve against a
schema it has not been migrated to. Guarded by a Postgres advisory lock:
Railway can start several instances at once, and two `alembic upgrade head`
runs racing on the same database is how a half-applied migration happens.

The worker deliberately does NOT migrate. One writer is enough, and a worker
that migrates would race the API on every deploy.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

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
    return config


def upgrade_to_head() -> None:
    engine = get_engine()
    with engine.connect() as conn:
        # Blocking lock, not try-lock: an instance that loses the race must
        # WAIT for the migration rather than start serving against the old
        # schema.
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": _LOCK_ID})
        conn.commit()
        try:
            logger.info("Running database migrations")
            command.upgrade(_alembic_config(), "head")
            logger.info("Database is up to date")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": _LOCK_ID})
            conn.commit()
