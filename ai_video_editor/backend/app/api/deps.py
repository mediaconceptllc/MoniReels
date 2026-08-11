"""Shared FastAPI dependencies for reading app.state set up during lifespan startup."""
from __future__ import annotations

from fastapi import HTTPException, Request

from app.video.capabilities import Capabilities
from app.video.ffmpeg import FfmpegBinaries


def get_ffmpeg_binaries(request: Request) -> FfmpegBinaries:
    binaries: FfmpegBinaries = request.app.state.ffmpeg_binaries
    if not binaries.available:
        raise HTTPException(
            status_code=503,
            detail="FFmpeg not found. Set FFMPEG_PATH, place binaries in backend/bin/, or add to PATH.",
        )
    return binaries


def get_capabilities(request: Request) -> Capabilities:
    return request.app.state.capabilities
