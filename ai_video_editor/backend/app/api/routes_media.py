"""Serves generated media (thumbnails, renders) back to Flutter.

Restricted to files under the app's data directory: this endpoint is only meant
to hand back files the backend itself produced, not to act as a general local
file server reachable from a renderer process.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.utils.paths import data_dir

router = APIRouter(tags=["media"])


@router.get("/files")
async def get_file(path: str) -> FileResponse:
    requested = Path(path).resolve()
    root = data_dir().resolve()
    if root not in requested.parents and requested != root:
        raise HTTPException(status_code=403, detail="Path is outside the managed data directory")
    if not requested.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {requested}")
    return FileResponse(requested)
