"""Reads/writes backend/.env directly, preserving existing lines/order/comments.

Backs the Settings endpoint so users can save API keys from the Flutter UI
without hand-editing a file — this does programmatically exactly what
editing it by hand would, then the caller clears the settings cache so the
change takes effect immediately.
"""
from __future__ import annotations

import sys
from pathlib import Path


def env_file_path() -> Path:
    """Resolved by file/executable location, not cwd, so writes always land
    next to the running backend regardless of what working directory it was
    launched with (dev `python -m app.main` vs. the packaged sidecar exe).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    # backend/app/utils/env_file.py -> backend/.env
    return Path(__file__).resolve().parents[2] / ".env"


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Updates KEY=value lines in place; appends any keys not already present.

    Values are written verbatim, unquoted — matches this project's existing
    .env / .env.example style. Written atomically (.tmp then replace).
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                new_lines.append(f"{key}={remaining.pop(key)}")
                continue
        new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={value}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
