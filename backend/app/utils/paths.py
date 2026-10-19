"""Scratch-space helpers.

Nothing under here survives. On Railway the container filesystem is ephemeral
and per-instance, so every path this module hands out is working space one
job is free to delete; anything worth keeping is uploaded to R2 (app.r2).

That inverts the desktop build's model, where the project directory was the
only copy of everything.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class OutOfSpace(RuntimeError):
    """Not enough room on the work disk now; there may be later, when
    another job's scratch is cleared."""


class NeverFits(OutOfSpace):
    """More than the work disk holds in all: waiting cannot help."""


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


def work_dir() -> Path:
    d = get_settings().resolved_work_dir
    try:
        d.mkdir(parents=True, exist_ok=True)
        writable = os.access(d, os.W_OK | os.X_OK)
    except PermissionError:
        writable = False
    if not writable:
        # The image runs as an unprivileged user (audit OPS-7), so a WORK_DIR
        # outside its reach is a configuration error — named, not a bare
        # Errno 13 in the middle of the first job.
        raise PermissionError(
            f"WORK_DIR {d} is not writable by uid {os.getuid()}. Point WORK_DIR under /tmp "
            "(the image's default is /tmp/monireels), or set RAILWAY_RUN_UID=0 to run as root."
        )
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


# Directories clear_all_workdirs has retired but not yet deleted.
TRASH_PREFIX = ".trash-"


def clear_all_workdirs() -> None:
    """Called at worker startup. A process killed mid-job (OOM, redeploy)
    leaves its scratch behind, and on a small container a few of those fill
    the disk before any new job can run.

    Renames rather than deletes. This runs before the worker's first claim,
    and rmtree over a volume holding a killed job's scratch - a source video
    and a few hundred audio chunks - is slow enough to matter: every queued
    job waits behind it with nothing in the log to say why. A rename is
    atomic and instant, so the fresh directory is there immediately and
    `purge_trash` clears the old tree off the claim path.
    """
    root = work_dir() / "jobs"
    if root.is_dir():
        try:
            root.rename(root.parent / f"{TRASH_PREFIX}{uuid.uuid4().hex}")
        except OSError:
            # Same filesystem is a given here, but a rename can still fail
            # (permissions, a name already taken). Deleting inline is slower
            # than the rename, never wrong.
            logger.warning("Could not retire %s; deleting it inline instead", root)
            shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)


def purge_trash() -> int:
    """Deletes what clear_all_workdirs retired. Belongs to background
    housekeeping, never to the path a job is waiting on."""
    root = get_settings().resolved_work_dir
    if not root.is_dir():
        return 0
    removed = 0
    for path in root.glob(f"{TRASH_PREFIX}*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed


def free_bytes(path: Path | None = None) -> int:
    return shutil.disk_usage(path or work_dir()).free


def ensure_free(need_bytes: int = 0) -> None:
    """Refuse to start a disk-heavy job that cannot finish.

    Every job that downloads a source video or writes a render calls this
    FIRST. Discovering there is no room halfway through costs the whole job
    and leaves a partial file behind; discovering it up front costs nothing,
    and the worker puts the job back to wait for room (OutOfSpace) — or
    fails it at once when the disk could never hold it (NeverFits).

    The messages are the job's error, which the page shows as it is.
    """
    settings = get_settings()
    floor = settings.work_free_min_mb * 1024 * 1024
    usage = shutil.disk_usage(work_dir())
    want = floor + need_bytes
    if want > usage.total:
        raise NeverFits(
            f"Энэ ажилд {_gb(want)} түр зай хэрэгтэй, worker-ийн диск нийтдээ {_gb(usage.total)}. "
            "Видеог хэсэглэж хуулах эсвэл админаар worker-ийн дискийг томруулна уу."
        )
    if usage.free < want:
        raise OutOfSpace(
            f"Серверийн түр зай хүрэлцэхгүй байна: {_gb(usage.free)} сул, {_gb(want)} хэрэгтэй."
        )


def disk_report() -> dict:
    usage = shutil.disk_usage(work_dir())
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "min_free_bytes": get_settings().work_free_min_mb * 1024 * 1024,
    }


#: A file name is at most 255 bytes, and callers put a prefix in front of
#: this one ("reel_1_…"). The stem is often a title the model wrote, and
#: nothing bounds how long that is (audit REL-9).
FILENAME_STEM_MAX = 96


def ascii_safe_filename(name: str) -> str:
    """Collapse a filename to ASCII, preserving the extension, with a stem of
    at most FILENAME_STEM_MAX characters."""
    stem, _, ext = name.rpartition(".")
    stem = stem or name
    ext = f".{ext}" if ext and stem != name else (f".{ext}" if ext else "")
    safe_stem = _ASCII_SAFE.sub("_", stem).strip("_")[:FILENAME_STEM_MAX].rstrip("_.-")
    return f"{safe_stem or uuid.uuid4().hex[:8]}{ext}"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    tmp.replace(path)
