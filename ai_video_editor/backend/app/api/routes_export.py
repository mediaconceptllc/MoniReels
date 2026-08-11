"""Preview (10s) and full export renders, both run as background jobs."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_capabilities, get_ffmpeg_binaries
from app.export.pipeline import render_timeline
from app.jobs.manager import JobHandle, get_job_manager
from app.store import ProjectNotFound, load_project
from app.timeline.models import Clip
from app.utils.logging import get_logger
from app.utils.paths import new_job_workdir, project_dir, project_output_dir
from app.video.capabilities import Capabilities
from app.video.ffmpeg import FfmpegBinaries

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["export"])

PREVIEW_MAX_DURATION_SEC = 10.0


def _clamp_clips_to_duration(clips: list[Clip], max_duration: float) -> list[Clip]:
    """Trims a clip list so the sum of clip durations does not exceed max_duration,
    truncating (not dropping) the clip that straddles the boundary.
    """
    result: list[Clip] = []
    remaining = max_duration
    for clip in sorted(clips, key=lambda c: c.order):
        if remaining <= 0:
            break
        duration = clip.end - clip.start
        if duration <= remaining:
            result.append(clip)
            remaining -= duration
        else:
            result.append(clip.model_copy(update={"end": clip.start + remaining}))
            remaining = 0.0
    return result


def _load_project_with_clips(project_id: str):
    try:
        project = load_project(project_id)
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found") from e
    if not project.clips:
        raise HTTPException(status_code=400, detail="Project timeline has no clips")
    return project


@router.post("/{project_id}/preview")
async def preview_project(
    project_id: str,
    binaries: FfmpegBinaries = Depends(get_ffmpeg_binaries),  # noqa: B008 - standard FastAPI DI pattern
    capabilities: Capabilities = Depends(get_capabilities),  # noqa: B008 - standard FastAPI DI pattern
) -> dict:
    project = _load_project_with_clips(project_id)
    preview_clips = _clamp_clips_to_duration(project.clips, PREVIEW_MAX_DURATION_SEC)
    output_path = project_dir(project_id) / "preview.mp4"

    async def worker(handle: JobHandle) -> dict:
        await render_timeline(
            handle, binaries, preview_clips, project.transition,
            crf=project.export.crf, preset=project.export.preset,
            orientation=project.export.orientation, portrait_fill=project.export.portrait_fill,
            use_hwaccel=project.export.use_hwaccel,
            working_hwaccel_encoder=capabilities.working_hwaccel_encoder,
            supported_xfade=capabilities.xfade_transitions,
            workdir=new_job_workdir(handle.job_id), output_path=output_path,
        )
        return {"output_path": str(output_path)}

    job = get_job_manager().start(worker)
    return {"job_id": job.id}


@router.post("/{project_id}/export")
async def export_project(
    project_id: str,
    binaries: FfmpegBinaries = Depends(get_ffmpeg_binaries),  # noqa: B008 - standard FastAPI DI pattern
    capabilities: Capabilities = Depends(get_capabilities),  # noqa: B008 - standard FastAPI DI pattern
) -> dict:
    project = _load_project_with_clips(project_id)
    ext = "mov" if project.export.container == "mov" else "mp4"
    output_path = project_output_dir(project_id) / f"export_{int(time.time())}.{ext}"

    async def worker(handle: JobHandle) -> dict:
        await render_timeline(
            handle, binaries, project.clips, project.transition,
            crf=project.export.crf, preset=project.export.preset,
            orientation=project.export.orientation, portrait_fill=project.export.portrait_fill,
            use_hwaccel=project.export.use_hwaccel,
            working_hwaccel_encoder=capabilities.working_hwaccel_encoder,
            supported_xfade=capabilities.xfade_transitions,
            workdir=new_job_workdir(handle.job_id), output_path=output_path,
        )
        return {"output_path": str(output_path)}

    job = get_job_manager().start(worker)
    return {"job_id": job.id}
