"""Rotating file logger (%APPDATA%/AIVideoEditor/logs/) + console."""
from __future__ import annotations

import logging
import logging.handlers
import sys

from app.config import get_settings
from app.utils.paths import logs_dir

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Spawned headless by the Flutter shell, sys.stdout's encoding defaults to
    # the legacy Windows codepage (cp1252) rather than UTF-8. Any log message
    # containing non-Latin1 text (Mongolian/Cyrillic suggestion titles, e.g.
    # render_all_ideas logging a title) then raises UnicodeEncodeError out of
    # the handler; logging's fallback error path tries to print that
    # traceback to stderr, which nothing ever drains on the Dart side - the
    # write blocks on the full pipe and freezes the entire single-threaded
    # event loop. Reconfiguring to UTF-8 with a replacing error handler means
    # a log write can never raise on its own text again.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir() / "backend.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
