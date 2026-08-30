"""Scratch-space helpers.

Nothing under here survives. On Railway the container filesystem is ephemeral
and per-instance, so every path this module hands out is working space one
job is free to delete; anything worth keeping is uploaded to R2 (app.r2).

That inverts the desktop build's model, where the project directory was the
only copy of everything.
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class OutOfSpace(RuntimeError):
    pass


def work_dir() -> Path:
    d = get_settings().resolved_work_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_workdir(job_id: str) -> Path:
    """A dedicated ASCII-named scratch dir for one job.

    ASCII-only on purpose: ffmpeg's `subtitles=` filter option is parsed with
    ':' as a separator, so callers that need that filter set cwd here and
    reference files by bare name instead of an absolute path.
    """
    d = work_dir() / "jobs" / _ASCII_SAFE.sub("_", job_id)[:40]
    d.mkdir(parents=True, exist_ok=True)
    return d


def clear_job_workdir(job_id: str) -> None:
    shutil.rmtree(work_dir() / "jobs" / _ASCII_SAFE.sub("_", job_id)[:40], ignore_errors=True)


def clear_all_workdirs() -> None:
    """Called at worker startup. A process killed mid-job (OOM, redeploy)
    leaves its scratch behind, and on a small container a few of those fill
    the disk before any new job can run."""
    root = work_dir() / "jobs"
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)


def free_bytes(path: Path | None = None) -> int:
    return shutil.disk_usage(path or work_dir()).free


def ensure_free(need_bytes: int = 0) -> None:
    """Refuse to start a disk-heavy job that cannot finish.

    Every job that downloads a source video or writes a render calls this
    FIRST. Discovering there is no room halfway through costs the whole job
    and leaves a partial file behind; discovering it up front costs nothing
    and the job simply waits for the next attempt.
    """
    settings = get_settings()
    floor = settings.work_free_min_mb * 1024 * 1024
    available = free_bytes()
    if available < floor + need_bytes:
        raise OutOfSpace(
            f"Not enough scratch space: {available / 1e9:.2f} GB free, "
            f"need {(floor + need_bytes) / 1e9:.2f} GB"
        )


def disk_report() -> dict:
    usage = shutil.disk_usage(work_dir())
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "min_free_bytes": get_settings().work_free_min_mb * 1024 * 1024,
    }


def ascii_safe_filename(name: str) -> str:
    """Collapse a filename to ASCII, preserving the extension."""
    stem, _, ext = name.rpartition(".")
    stem = stem or name
    ext = f".{ext}" if ext and stem != name else (f".{ext}" if ext else "")
    safe_stem = _ASCII_SAFE.sub("_", stem).strip("_") or uuid.uuid4().hex[:8]
    return f"{safe_stem}{ext}"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    tmp.replace(path)
