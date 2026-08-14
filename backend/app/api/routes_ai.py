"""AI suggestions: transcript -> Suggestions, run as a background job. Uses
whichever provider Settings.ai_provider selects (OpenAI or Anthropic)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.ai.anthropic_client import AnthropicClient, AnthropicConfig
from app.ai.llm_client import LLMClient, LLMError
from app.ai.openai_client import OpenAIClient, OpenAIConfig
from app.ai.schema import SuggestionValidationError
from app.ai.suggest import generate_suggestions
from app.config import Settings, get_settings
from app.jobs.manager import JobHandle, get_job_manager
from app.store import ProjectNotFound, load_project, save_project
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["ai"])


def _build_client(settings: Settings, provider: str) -> LLMClient:
    if provider == "anthropic":
        anthropic_config = AnthropicConfig(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
        return AnthropicClient(anthropic_config)
    openai_config = OpenAIConfig(
        api_key=settings.openai_api_key, model=settings.openai_model, base_url=settings.openai_base_url
    )
    return OpenAIClient(openai_config)


@router.post("/{project_id}/suggest")
async def suggest_project(project_id: str, provider: str | None = None) -> dict:
    # `provider` lets a single regeneration override Settings.ai_provider
    # (e.g. "try the same transcript with the other provider") without
    # changing the persisted default for future runs.
    if provider is not None and provider not in ("openai", "anthropic"):
        detail = f"Unknown provider {provider!r} - must be 'openai' or 'anthropic'"
        raise HTTPException(status_code=400, detail=detail)
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
        settings = get_settings()
        effective_provider = provider or settings.ai_provider
        provider_label = "Claude" if effective_provider == "anthropic" else "OpenAI"
        await handle.set_progress(0.1, stage="requesting", message=f"Asking {provider_label} for suggestions")
        client = _build_client(settings, effective_provider)
        try:
            current = load_project(project_id)
            assert current.transcript is not None
            suggestions = await generate_suggestions(client, current.transcript, duration_sec)
        except (LLMError, SuggestionValidationError) as e:
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
