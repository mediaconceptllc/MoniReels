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
    # The ceiling for pause-based audio chunking (chimege_client.py) - a
    # chunk only gets force-cut once a pause-free stretch reaches this many
    # seconds; real pauses drive splitting well below it. Also documents
    # Chimege's real /transcribe hard cap: 3MB, ~98s of 16kHz mono 16-bit PCM
    # audio — this default leaves a safety margin under that.
    chimege_max_audio_sec: int = 60

    # Which provider app.ai.suggest uses for suggestion generation. "openai"
    # or "anthropic" - both clients stay configured/switchable so the app
    # isn't locked to whichever one happens to be having a bad day.
    ai_provider: str = "openai"

    openai_api_key: str = ""
    openai_model: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    anthropic_api_key: str = ""
    anthropic_model: str = ""

    log_level: str = "INFO"
    data_dir: str = ""

    # Managed-mode heartbeat: exit if idle this long (spec: 60s while spawned by Flutter).
    idle_shutdown_sec: int = 60
    managed: bool = False

    # Vocal separation (app/audio/separation.py) - htdemucs weights are
    # downloaded on first use via torch.hub and cached under
    # resolved_data_dir/models, never bundled with the installer.
    demucs_model: str = "htdemucs"

    # VAD (app/audio/vad.py) - Silero VAD, same download-on-first-use caching.
    # Threshold is the model's own speech-probability cutoff (0..1); the two
    # durations mirror Silero's get_speech_timestamps knobs. min_silence_ms
    # is the main "how finely does this split" lever: lowered from an
    # original 350ms so shorter pauses (a quick breath between phrases, not
    # just full sentence-scale gaps) also count as a split point, producing
    # more/smaller VAD segments - which flows through to smaller Chimege
    # chunks too, since a chunk can only ever be cut at a segment boundary.
    vad_threshold: float = 0.5
    vad_min_speech_ms: int = 250
    # Lowered again (150ms -> 100ms): a speaker with few full-sentence-scale
    # pauses still produces long single VAD segments (observed: 25s of
    # continuous speech as one segment), and any segment spanning multiple
    # sentences falls back to proportional-by-character/word estimated
    # splitting (app.audio.vad_chunking.synthesize_segments_for_chunk) for
    # its internal cut points instead of a real detected boundary - that
    # estimate is what AI-suggested cuts inherit their imprecision from.
    # 100ms is close to Silero VAD's practical floor before normal phoneme-
    # level gaps (stops/plosives) start getting misread as sentence breaks.
    vad_min_silence_ms: int = 100
    vad_speech_pad_ms: int = 100

    @property
    def resolved_data_dir(self) -> Path:
        return Path(self.data_dir) if self.data_dir else Path(_default_data_dir())

    @property
    def resolved_model_cache_dir(self) -> Path:
        return self.resolved_data_dir / "models"


@lru_cache
def get_settings() -> Settings:
    return Settings()
