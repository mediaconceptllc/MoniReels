"""Windows-safe path helpers for data, temp, and per-job working directories."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.config import get_settings

_ASCII_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def data_dir() -> Path:
    d = get_settings().resolved_data_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def projects_dir() -> Path:
    d = data_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir(project_id: str) -> Path:
    d = projects_dir() / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_output_dir(project_id: str) -> Path:
    d = project_dir(project_id) / "output"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def jobs_tmp_root() -> Path:
    d = data_dir() / "tmp" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_job_workdir(job_id: str) -> Path:
    """A dedicated, ASCII-only-named temp dir for one job's intermediate files.

    Kept ASCII-only and drive-relative-safe on purpose: FFmpeg filtergraphs
    (notably `subtitles=`) mishandle Windows drive-letter paths, so callers
    that need that filter should set cwd to this dir and reference files by
    bare filename instead of an absolute path.
    """
    d = jobs_tmp_root() / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def ascii_safe_filename(name: str) -> str:
    """Collapse a filename to ASCII-only characters, preserving the extension."""
    stem, _, ext = name.rpartition(".")
    stem = stem or name
    ext = f".{ext}" if ext and stem != name else (f".{ext}" if ext else "")
    safe_stem = _ASCII_SAFE.sub("_", stem).strip("_") or uuid.uuid4().hex[:8]
    return f"{safe_stem}{ext}"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a .tmp sibling then os.replace, so readers never see a partial file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    tmp.replace(path)
