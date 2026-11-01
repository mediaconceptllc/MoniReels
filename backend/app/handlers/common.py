"""What every handler needs: the job's project, and ffmpeg.

Package-internal: the handlers in app.handlers import these, nothing else
should.
"""
from __future__ import annotations

from app.jobs.queue import JobHandle
from app.video.ffmpeg import discover_ffmpeg


def _project_id(handle: JobHandle) -> str:
    if not handle.project_id:
        raise RuntimeError(f"Job {handle.job_id} ({handle.kind}) has no project")
    return handle.project_id


def _require_ffmpeg():
    binaries = discover_ffmpeg()
    if not binaries.available:
        raise RuntimeError("FFmpeg is not installed in this image")
    return binaries
