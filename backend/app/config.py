"""Application configuration, loaded from environment / .env file.

All secrets and machine-specific paths live here — never hardcoded elsewhere.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> str:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return str(Path(appdata) / "AIVideoEditor")
    return str(Path.home() / ".aivideoeditor")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_host: str = "127.0.0.1"
    app_port: int = 0  # 0 => choose a free port at startup

    ffmpeg_path: str = ""  # explicit dir or exe path override

    chimege_stt_url: str = "https://api.chimege.com/v1.2"  # base URL; endpoints appended in chimege_client.py
    chimege_token: str = ""
    # Threshold (seconds) below which we use the synchronous /transcribe
    # endpoint; at/above it we use the async /stt-long push+poll flow instead.
    # /transcribe is capped at 3MB, which is ~98s of 16kHz mono 16-bit PCM
    # audio — this default leaves a safety margin under that hard cap.
    chimege_max_audio_sec: int = 60

    openai_api_key: str = ""
    openai_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    log_level: str = "INFO"
    data_dir: str = ""

    # Managed-mode heartbeat: exit if idle this long (spec: 60s while spawned by Flutter).
    idle_shutdown_sec: int = 60
    managed: bool = False

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else Path(_default_data_dir())


@lru_cache
def get_settings() -> Settings:
    return Settings()
