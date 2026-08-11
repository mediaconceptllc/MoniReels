"""AI suggestions: transcript -> OpenAI -> Suggestions, run as a background job."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.ai.openai_client import OpenAIClient, OpenAIConfig, OpenAIError
from app.ai.schema import SuggestionValidationError
from app.ai.suggest import generate_suggestions
from app.config import get_settings
from app.jobs.manager import JobHandle, get_job_manager
from app.store import ProjectNotFound, load_project, save_project
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["ai"])


@router.post("/{project_id}/suggest")
async def suggest_project(project_id: str) -> dict:
    try:
        project = load_project(project_id)
    except ProjectNotFound as e:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found") from e
    if project.video is None:
        raise HTTPException(status_code=400, detail="Project has no imported video")
    if project.transcript is None:
        raise HTTPException(status_code=400, detail="Project has no transcript yet — run /transcribe first")

    duration_sec = project.video.duration_sec

    async def worker(handle: JobHandle) -> dict:
        await handle.set_progress(0.1, stage="requesting", message="Asking OpenAI for suggestions")
        settings = get_settings()
        client = OpenAIClient(
            OpenAIConfig(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                base_url=settings.openai_base_url,
            )
        )
        try:
            current = load_project(project_id)
            assert current.transcript is not None
            suggestions = await generate_suggestions(client, current.transcript, duration_sec)
        except (OpenAIError, SuggestionValidationError) as e:
            raise RuntimeError(f"Suggestion generation failed: {e}") from e
        finally:
            await client.aclose()

        current = load_project(project_id)
        current.suggestions = suggestions
        save_project(current)

        await handle.set_progress(1.0, stage="done", message="Suggestions ready")
        return current.model_dump(mode="json")

    job = get_job_manager().start(worker)
    return {"job_id": job.id}
