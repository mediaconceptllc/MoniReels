"""duudlaga.dev — Mongolian speech-to-text. The only STT provider.

Replaces Chimege. Everything that was NOT Chimege-specific already moved to
app.stt.chunking; what remains here is transport only.

┌───────────────────────────────────────────────────────────────────────────┐
│ VERIFY THE WIRE FORMAT BEFORE PRODUCTION                                   │
│                                                                           │
│ duudlaga.dev is unreachable from the environment this client was written   │
│ in (the egress policy blocks the domain), so the request/response shape    │
│ below follows the near-universal OpenAI-compatible audio-transcription     │
│ convention rather than a spec that was actually read:                      │
│                                                                           │
│     POST {base}/audio/transcriptions                                       │
│     Authorization: Bearer <key>                                            │
│     multipart/form-data: file=<wav>, model=…, language=mn                  │
│     -> {"text": "..."}                                                     │
│                                                                           │
│ If the real API differs, ONLY `_build_request` and `_parse_response` need  │
│ to change — they are the entire surface that touches the wire. Nothing     │
│ else in this file, and nothing outside it, encodes the format.             │
│ `DUUDLAGA_TRANSCRIBE_PATH` also lets the path be corrected from the        │
│ environment without a deploy.                                              │
└───────────────────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from app.models import Transcript
from app.stt.base import SttProvider
from app.stt.chunking import (
    TARGET_CHUNK_MIN_SEC,
    compute_pause_boundaries,
    detect_silences,
    extract_chunk,
    merge_transcripts,
    text_to_transcript,
    wav_duration_sec,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 1.0
BACKOFF_FACTOR = 2.0
REQUEST_TIMEOUT_SEC = 180.0

# Overridable without a redeploy — see the box above.
TRANSCRIBE_PATH = os.environ.get("DUUDLAGA_TRANSCRIBE_PATH", "/audio/transcriptions")


class DuudlagaError(Exception):
    pass


@dataclass
class DuudlagaConfig:
    base_url: str
    api_key: str
    max_audio_sec: float
    model: str = ""
    language: str = "mn"


class DuudlagaClient(SttProvider):
    def __init__(self, config: DuudlagaConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def max_audio_sec(self) -> float:
        return self._config.max_audio_sec

    # -- public API --------------------------------------------------------

    async def transcribe_chunk_text(self, chunk_wav_path: Path) -> str:
        """One already-cut chunk in, raw text out — no timing attached.

        This is the single network primitive. Both this class's own
        pause-based chunking and the VAD pipeline in app.stt.pipeline build
        on it, so neither has to know the wire format.
        """
        return await self._with_retry(self._post_chunk, chunk_wav_path)

    async def transcribe(self, wav_path: Path) -> Transcript:
        """Whole file in, Transcript out.

        `wav_path` must already be 16 kHz mono 16-bit PCM. The file is ALWAYS
        pause-split first, even when it is shorter than max_audio_sec: a whole
        file is never sent as one request, and compute_pause_boundaries
        degrades to a single [0, duration] chunk on its own for short clips.
        """
        if not self._config.base_url or not self._config.api_key:
            raise DuudlagaError("DUUDLAGA_BASE_URL / DUUDLAGA_API_KEY are not configured")

        ffmpeg = _require_ffmpeg()
        total_duration = wav_duration_sec(wav_path)
        silences = await detect_silences(ffmpeg, wav_path)
        boundaries = compute_pause_boundaries(
            total_duration, silences, self._config.max_audio_sec, min_chunk_sec=TARGET_CHUNK_MIN_SEC
        )
        logger.info(
            "Splitting %.1fs of audio into %d pause-based chunk(s): %s",
            total_duration,
            len(boundaries),
            ", ".join(f"[{s:.1f}-{e:.1f}]" for s, e in boundaries),
        )

        workdir = wav_path.parent / f"{wav_path.stem}_chunks"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            results: list[tuple[float, Transcript]] = []
            for i, (start, end) in enumerate(boundaries):
                chunk_path = workdir / f"chunk_{i:03d}.wav"
                await extract_chunk(ffmpeg, wav_path, chunk_path, start, end)
                logger.info("Sending chunk %d/%d (%.1f-%.1fs)", i + 1, len(boundaries), start, end)
                text = await self.transcribe_chunk_text(chunk_path)
                results.append((start, text_to_transcript(text, end - start, self._config.language)))
            return merge_transcripts(results)
        finally:
            for f in workdir.glob("*.wav"):
                f.unlink(missing_ok=True)
            workdir.rmdir()

    # -- wire format: the only two methods that know the protocol ----------

    def _build_request(self, wav_path: Path) -> tuple[str, dict, dict, dict]:
        """Returns (url, headers, files, data) for one transcription request."""
        url = f"{self._config.base_url.rstrip('/')}{TRANSCRIBE_PATH}"
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        files = {"file": (wav_path.name, wav_path.read_bytes(), "audio/wav")}
        data: dict[str, str] = {"language": self._config.language, "response_format": "json"}
        if self._config.model:
            data["model"] = self._config.model
        return url, headers, files, data

    @staticmethod
    def _parse_response(payload: object) -> str:
        """Pull the transcript text out of whatever shape came back.

        Deliberately tolerant across the handful of conventions in use, so a
        small difference from the assumed spec surfaces as slightly-off
        parsing rather than a hard failure on the first real call. An
        unrecognised shape still raises — silently returning "" would look
        exactly like "this audio had no speech".
        """
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, list):
            return " ".join(DuudlagaClient._parse_response(item) for item in payload).strip()
        if isinstance(payload, dict):
            for key in ("text", "transcription", "transcript", "result"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value.strip()
                if isinstance(value, (list, dict)):
                    return DuudlagaClient._parse_response(value)
            segments = payload.get("segments")
            if isinstance(segments, list):
                return " ".join(
                    s.get("text", "") for s in segments if isinstance(s, dict)
                ).strip()
        raise DuudlagaError(f"Unrecognised duudlaga.dev response shape: {str(payload)[:300]}")

    async def _post_chunk(self, wav_path: Path) -> str:
        url, headers, files, data = self._build_request(wav_path)
        response = await self._client.post(url, headers=headers, files=files, data=data)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError:
            # Some endpoints answer text/plain for a plain transcript.
            return response.text.strip()
        return self._parse_response(payload)

    # -- retry -------------------------------------------------------------

    async def _with_retry(self, call: Callable[..., Awaitable[T]], *args: object) -> T:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await call(*args)
            except httpx.TimeoutException as e:
                last_error = e
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise DuudlagaError(_describe_http_error(e)) from e
                last_error = e

            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_BASE_SEC * (BACKOFF_FACTOR ** (attempt - 1))
                logger.warning(
                    "duudlaga.dev attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, type(last_error).__name__, delay,
                )
                await asyncio.sleep(delay)

        raise DuudlagaError(f"duudlaga.dev request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def build_client(settings) -> DuudlagaClient:
    return DuudlagaClient(
        DuudlagaConfig(
            base_url=settings.duudlaga_base_url,
            api_key=settings.duudlaga_api_key,
            max_audio_sec=settings.duudlaga_max_audio_sec,
            model=settings.duudlaga_model,
        )
    )


def _require_ffmpeg() -> Path:
    from app.video.ffmpeg import discover_ffmpeg

    binaries = discover_ffmpeg()
    if binaries.ffmpeg is None:
        raise DuudlagaError("FFmpeg is required for audio chunking but was not found")
    return binaries.ffmpeg


def _describe_http_error(e: httpx.HTTPStatusError) -> str:
    status = e.response.status_code
    if status in (401, 403):
        return f"duudlaga.dev rejected the API key ({status}). Check DUUDLAGA_API_KEY."
    if status == 404:
        return (
            f"duudlaga.dev returned 404 for {e.request.url}. "
            "The transcription path may differ — set DUUDLAGA_TRANSCRIBE_PATH."
        )
    if status == 413:
        return "Audio chunk was rejected as too large. Lower DUUDLAGA_MAX_AUDIO_SEC."
    return f"duudlaga.dev request failed ({status}): {e.response.text[:300]}"
