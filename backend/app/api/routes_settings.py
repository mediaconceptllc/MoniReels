"""Read/update backend settings (API credentials, thresholds) from the Flutter UI.

Writes go directly to backend/.env and clear the settings cache so they take
effect immediately — no backend restart needed. Secret fields are never
echoed back in responses, only whether they're currently set.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.utils.env_file import env_file_path, update_env_file

router = APIRouter(prefix="/settings", tags=["settings"])

_ENV_KEYS = {
    "chimege_stt_url": "CHIMEGE_STT_URL",
    "chimege_token": "CHIMEGE_TOKEN",
    "chimege_max_audio_sec": "CHIMEGE_MAX_AUDIO_SEC",
    "openai_api_key": "OPENAI_API_KEY",
    "openai_model": "OPENAI_MODEL",
    "openai_base_url": "OPENAI_BASE_URL",
}


class SettingsUpdateRequest(BaseModel):
    chimege_stt_url: str | None = None
    chimege_token: str | None = None
    chimege_max_audio_sec: int | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None


class SettingsResponse(BaseModel):
    chimege_stt_url: str
    chimege_token_set: bool
    chimege_max_audio_sec: int
    openai_model: str
    openai_base_url: str
    openai_api_key_set: bool


def _to_response(settings: Settings) -> SettingsResponse:
    return SettingsResponse(
        chimege_stt_url=settings.chimege_stt_url,
        chimege_token_set=bool(settings.chimege_token),
        chimege_max_audio_sec=settings.chimege_max_audio_sec,
        openai_model=settings.openai_model,
        openai_base_url=settings.openai_base_url,
        openai_api_key_set=bool(settings.openai_api_key),
    )


@router.get("")
async def get_settings_route() -> SettingsResponse:
    return _to_response(get_settings())


@router.put("")
async def update_settings_route(body: SettingsUpdateRequest) -> SettingsResponse:
    updates = {_ENV_KEYS[field]: str(value) for field, value in body.model_dump(exclude_none=True).items()}
    if updates:
        update_env_file(env_file_path(), updates)
        get_settings.cache_clear()
    return _to_response(get_settings())
