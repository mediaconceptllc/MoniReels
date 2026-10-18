"""Alembic environment. Reads the URL from DATABASE_URL, never from alembic.ini."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.db import normalize_url
from app.dbmodels import Base

config = context.config
# Only the CLI (`alembic upgrade head`) takes its logging from alembic.ini.
# The API runs these migrations IN-PROCESS at startup (app.migrate), after
# setup_logging() and after uvicorn configured its own loggers — and
# fileConfig, with its default disable_existing_loggers=True, disables every
# logger that already exists and puts the root back at WARNING on stderr.
# That is how the production API went silent: no request lines, no
# tracebacks, not even "API ready".
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return normalize_url(url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # compare_type catches a column whose Python type changed but whose
        # name did not — the failure mode that otherwise ships silently.
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
