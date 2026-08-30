"""Logging to stdout only.

The desktop build also wrote a rotating file under %APPDATA%. On a container
that is the wrong place twice over: the filesystem is ephemeral, so the file
is lost on every restart, and the platform already collects stdout — a log
that only exists inside a dead container is a log nobody can read.

Dropping the file handler also removes a genuine import cycle: the file path
came from app.utils.paths, which needs a logger of its own.

Reading the level from the environment directly rather than through
app.config keeps this module importable by everything, including config
itself, with no cycle to reason about.
"""
from __future__ import annotations

import logging
import os
import sys

_CONFIGURED = False


def setup_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)

    # Titles and transcripts are Mongolian Cyrillic. If the stream's encoding
    # cannot represent them, the logging call itself raises out of the
    # handler — a log line must never be able to fail the operation it is
    # describing.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
        )
    )
    root.handlers = [handler]

    # These two are extremely chatty at INFO and drown out everything else.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
