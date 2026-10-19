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

One JSON object per line (LOG_FORMAT=json, the default), carrying the
request or job it belongs to. A plain line said what happened but not to
whom: two exports failing at once interleaved their tracebacks, and "which
request was that 500" had no answer but the timestamp. Railway parses the
JSON, files each line by its `level` and makes every field searchable
(`@request_id:…`, `@job_id:…`). LOG_FORMAT=text keeps the old line for a
terminal.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

_CONFIGURED = False

#: The HTTP request this code is serving (set by app.main's middleware), or
#: the job it is running (set by app.worker). Context variables, so two
#: requests or two jobs running side by side never borrow each other's.
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id: ContextVar[str | None] = ContextVar("job_id", default=None)
job_kind: ContextVar[str | None] = ContextVar("job_kind", default=None)


class _Context(logging.Filter):
    """Stamps every record with the request or job it belongs to — on the
    handler, so a record from any library logger carries it too."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        record.job_id = job_id.get()
        record.job_kind = job_kind.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            # Lowercase: the field name and the values Railway reads it by.
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("request_id", "job_id", "job_kind"):
            value = getattr(record, name, None)
            if value:
                line[name] = value
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            line["exception"] = record.exc_text
        return json.dumps(line, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        ids = " ".join(
            f"{name}={value}"
            for name in ("request_id", "job_id")
            if (value := getattr(record, name, None))
        )
        return f"{text} [{ids}]" if ids else text


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
    handler.addFilter(_Context())
    if os.environ.get("LOG_FORMAT", "json").strip().lower() == "text":
        handler.setFormatter(
            _TextFormatter(
                "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
            )
        )
    else:
        handler.setFormatter(JsonFormatter())
    root.handlers = [handler]

    # These two are extremely chatty at INFO and drown out everything else.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
