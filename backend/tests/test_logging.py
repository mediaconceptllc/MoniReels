"""The API must keep its logging through startup.

Startup runs the Alembic migrations in-process (app.migrate). Alembic's
env.py used to call `fileConfig(alembic.ini)`, whose default
`disable_existing_loggers=True` switched off every logger that existed at
that moment — the app's, the request log, uvicorn's error log — and put the
root back at WARNING on stderr. Production ran like that: a deployment that
served more than a hundred requests wrote six log lines, none of them a
request and none of them an error.
"""
from __future__ import annotations

import logging

import pytest
from alembic import command

from app.migrate import _alembic_config
from app.utils.logging import setup_logging
from tests.conftest import requires_db

LOGGERS = ("app.main", "app.api.routes_projects", "app.jobs.queue", "uvicorn.error", "uvicorn.access")


@pytest.fixture
def logging_state():
    """Put logging back exactly as it was, whatever the test did to it — a
    regression here must fail this test, not every test after it."""
    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers))
    saved = {name: (logging.getLogger(name).disabled, logging.getLogger(name).level) for name in LOGGERS}
    yield
    root.setLevel(saved_root[0])
    root.handlers = saved_root[1]
    for name, (disabled, level) in saved.items():
        logger = logging.getLogger(name)
        logger.disabled = disabled
        logger.setLevel(level)


@requires_db
def test_running_the_migrations_leaves_the_apps_logging_alone(logging_state, capsys):
    setup_logging()
    root = logging.getLogger()
    for name in LOGGERS:
        logging.getLogger(name)  # exists before the migration, as in a real startup
    before = (root.level, list(root.handlers))

    # Offline mode renders the SQL instead of connecting: env.py runs in
    # full — including its logging setup — with no database to touch.
    command.upgrade(_alembic_config(), "head", sql=True)

    assert [name for name in LOGGERS if logging.getLogger(name).disabled] == []
    assert (root.level, root.handlers) == before
    assert "CREATE TABLE" in capsys.readouterr().out  # the migrations really ran


def test_uvicorn_writes_through_the_apps_handler_and_logs_no_second_access_line(monkeypatch):
    """`log_config=None`: uvicorn's own lines go to the app's stdout handler,
    not to stderr, which the platform files as severity "error".
    `access_log=False`: the `audit` middleware already logs each request,
    with the client address read safely; uvicorn's line would add the query
    string and the client-chosen first X-Forwarded-For hop."""
    import app.main as main_module

    seen: dict = {}
    monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))

    main_module.main()

    assert seen["log_config"] is None
    assert seen["access_log"] is False
    assert logging.getLogger().handlers, "logging must be set up before uvicorn starts"
