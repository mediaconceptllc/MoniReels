"""Project CRUD + import (probe/thumbnail runs as a background job)."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_ffmpeg_binaries
from app.jobs.manager import JobHandle, get_job_manager
from app.models import Project, VideoMeta
from app.store import ProjectNotFound, delete_project, list_projects, load_project, save_project
from app.utils.logging import get_logger
from app.utils.paths import project_dir
from app.video.ffmpeg import FfmpegBinaries
from app.video.probe import generate_thumbnail, probe_video

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    video_path: str
    name: str | None = None


class CreateProjectResponse(BaseModel):
    project_id: str
    job_id: str


@router.post("", response_model=CreateProjectResponse)
async def create_project(
    body: CreateProjectRequest,
    binaries: FfmpegBinaries = Depends(get_ffmpeg_binaries),  # noqa: B008 - standard FastAPI DI pattern
) -> CreateProjectResponse:
    video_path = Path(body.video_path)
    if not video_path.is_file():
        raise HTTPException(status_code=400, detail=f"Video file not found: {video_path}")

    project = Project(name=body.name or video_path.stem)
    save_project(project)

    async def worker(handle: JobHandle) -> dict:
        await handle.set_progress(0.1, stage="probing", message="Reading video metadata")
        raw = await probe_video(binaries.ffprobe, video_path)  # type: ignore[arg-type]

        # Copies the source video into the project directory so the project
        # is self-contained (still works if the original file is moved,
        # renamed, or the source drive is unavailable later) — everything
        # downstream (thumbnail, transcription, export) reads project.video.path,
        # so pointing it at the copy is transparent to the rest of the app.
        await handle.set_progress(0.3, stage="copying", message="Copying video into project")
        dest_video_path = project_dir(project.id) / f"source{video_path.suffix}"
        await asyncio.to_thread(shutil.copy2, video_path, dest_video_path)

        thumb_path = project_dir(project.id) / "thumbnail.jpg"
        await handle.set_progress(0.7, stage="thumbnail", message="Extracting thumbnail")
        seek = min(1.0, max(0.0, raw["duration_sec"] / 2))
        await generate_thumbnail(binaries.ffmpeg, dest_video_path, thumb_path, at_sec=seek)  # type: ignore[arg-type]

        video_meta = VideoMeta(
            path=str(dest_video_path),
            duration_sec=raw["duration_sec"],
            width=raw["width"],
            height=raw["height"],
            fps=raw["fps"],
            has_audio=raw["has_audio"],
            codec=raw["codec"],
            thumbnail_path=str(thumb_path),
        )

        current = load_project(project.id)
        current.video = video_meta
        save_project(current)

        await handle.set_progress(1.0, stage="done", message="Import complete")
        return current.model_dump(mode="json")

    job = get_job_manager().start(worker)
    return CreateProjectResponse(project_id=project.id, job_id=job.id)


@router.get("")
async def get_projects() -> list[dict]:
    return [p.model_dump(mode="json") for p in list_projects()]


@router.get("/{project_id}")
async def get_project(project_id: str) -> dict:
    try:
        return load_project(project_id).model_dump(mode="json")
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found") from e


@router.put("/{project_id}")
async def put_project(project_id: str, body: dict) -> dict:
    if body.get("id") != project_id:
        raise HTTPException(status_code=400, detail="Body 'id' must match URL project_id")
    try:
        project = Project.model_validate(body)
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as a typed 422
        raise HTTPException(status_code=422, detail=f"Invalid project payload: {e}") from e
    save_project(project)
    return project.model_dump(mode="json")


@router.delete("/{project_id}")
async def remove_project(project_id: str) -> dict:
    try:
        delete_project(project_id)
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found") from e
    return {"deleted": True}
