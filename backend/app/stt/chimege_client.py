"""Chimege.mn STT client — the ONLY speech-to-text implementation (see HARD RULES §0).

Confirmed against the real Chimege OpenAPI spec (v1.2) provided by the user —
this is no longer a guess. Two endpoints are used:

- POST /transcribe: synchronous, plain-text response, capped at 3MB request
  body (~98s of 16kHz mono 16-bit PCM audio). Used for short clips.
- POST /stt-long + GET /stt-long-transcript: asynchronous push-then-poll by
  UUID, built for arbitrarily long audio ("хэдэн ч цагийн яриа" — any number
  of hours). Used for anything at/above CHIMEGE_MAX_AUDIO_SEC.

Chimege never returns word- or segment-level timestamps in either path.
/stt-long-transcript does return a time-ordered array of
`{done, transcription, duration}` chunks, though — no start/end per chunk,
but ordered with a known duration each, so exact offsets are reconstructed
by cumulative-summing durations and reusing the same shift/merge logic a
client-side-chunked provider would need. That's what `merge_transcripts`
below is for; it doesn't care whether the chunks came from our own splitting
or the provider's.

Every Chimege-specific detail lives in this one file, driven by
CHIMEGE_STT_URL / CHIMEGE_TOKEN / CHIMEGE_MAX_AUDIO_SEC from config. See
docs/CHIMEGE.md for the confirmed contract. Everything downstream of
`transcribe()` only ever sees a normalized `Transcript`.
"""
from __future__ import annotations

import asyncio
import re
import uuid as uuid_lib
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from app.models import Segment, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 1.0
BACKOFF_FACTOR = 2.0

# POST /transcribe hard cap (error 2001 above this). Used as a safety check
# even when duration-based routing already chose the short path.
TRANSCRIBE_MAX_BYTES = 3 * 1024 * 1024

POLL_INTERVAL_SEC = 1.5  # spec: poll no more often than every 1s
POLL_TIMEOUT_MIN_SEC = 180.0  # spec: ~1h of audio takes ~4min to transcribe

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")

# POST /transcribe error codes (Error-Code response header on 400s).
_TRANSCRIBE_ERROR_CODES = {
    2000: "Error receiving audio data — check connection/audio data.",
    2001: "Audio file is too large (max 3MB).",
    2002: "Audio file is too small (min 50KB wav / 2KB other).",
    2003: "Audio is too short (min 0.5s).",
    2004: "Invalid audio encoding — must be WAV.",
    2005: "Failed to convert audio to WAV.",
}
_TOKEN_ERROR_CODES = {
    1000: "Invalid API token.",
    1001: "API token is missing.",
    1002: "Inactive API token.",
    1003: "Suspended API token.",
}


class ChimegeError(Exception):
    pass


T = TypeVar("T")


# --------------------------------------------------------------------------
# Pure helpers: timestamp shifting, merging, no-timing-info synthesis.
# Kept dependency-free (no network, no subprocess) so they're unit-testable
# with canned data.
# --------------------------------------------------------------------------


def wav_duration_sec(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate) if rate else 0.0


def shift_transcript(transcript: Transcript, offset_sec: float) -> Transcript:
    """Returns a copy of `transcript` with every timestamp shifted by offset_sec.

    This offset addition is the most common bug in chunked STT pipelines —
    see test_chimege_client.py for the regression test.
    """
    shifted_segments = [
        Segment(
            id=seg.id,
            start=seg.start + offset_sec,
            end=seg.end + offset_sec,
            text=seg.text,
            speaker=seg.speaker,
            words=[],  # Chimege never returns word-level timings (see module docstring)
        )
        for seg in transcript.segments
    ]
    return Transcript(
        language=transcript.language,
        segments=shifted_segments,
        full_text=transcript.full_text,
        timings_estimated=transcript.timings_estimated,
    )


def merge_transcripts(chunk_results: list[tuple[float, Transcript]]) -> Transcript:
    """Merges per-chunk transcripts (each with timestamps relative to its own
    chunk start) into one Transcript with absolute timestamps, in chunk order.
    """
    if not chunk_results:
        return Transcript(language="", segments=[], full_text="", timings_estimated=False)

    all_segments: list[Segment] = []
    full_text_parts: list[str] = []
    any_estimated = False
    language = chunk_results[0][1].language

    for offset, transcript in chunk_results:
        shifted = shift_transcript(transcript, offset)
        all_segments.extend(shifted.segments)
        if shifted.full_text:
            full_text_parts.append(shifted.full_text)
        any_estimated = any_estimated or shifted.timings_estimated

    return Transcript(
        language=language,
        segments=all_segments,
        full_text=" ".join(full_text_parts),
        timings_estimated=any_estimated,
    )


def synthesize_segments_from_text(text: str, duration_sec: float) -> list[Segment]:
    """Chimege never returns word/segment timings (either endpoint): splits on
    sentence boundaries and allocates time proportional to each sentence's
    share of the total character count.
    """
    text = text.strip()
    if not text or duration_sec <= 0:
        return []

    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    total_chars = sum(len(s) for s in sentences) or 1
    segments: list[Segment] = []
    cursor = 0.0
    for sentence in sentences:
        share = len(sentence) / total_chars
        seg_duration = duration_sec * share
        segments.append(
            Segment(
                id=uuid_lib.uuid4().hex,
                start=cursor,
                end=min(duration_sec, cursor + seg_duration),
                text=sentence,
                words=[],
            )
        )
        cursor += seg_duration

    return segments


def text_to_transcript(text: str, duration_sec: float, language: str = "mn") -> Transcript:
    segments = synthesize_segments_from_text(text, duration_sec)
    return Transcript(language=language, segments=segments, full_text=text.strip(), timings_estimated=True)


def poll_timeout_sec(duration_sec: float) -> float:
    """~1h of audio transcribes in ~4min per the spec; scale with a healthy margin."""
    return max(POLL_TIMEOUT_MIN_SEC, duration_sec * 0.5)


# --------------------------------------------------------------------------
# The client itself.
# --------------------------------------------------------------------------


@dataclass
class ChimegeConfig:
    url: str  # base URL, e.g. https://api.chimege.com/v1.2
    token: str
    max_audio_sec: float


class ChimegeClient:
    def __init__(self, config: ChimegeConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=120.0)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def transcribe(self, wav_path: Path) -> Transcript:
        if not self._config.url or not self._config.token:
            raise ChimegeError("CHIMEGE_STT_URL / CHIMEGE_TOKEN are not configured")

        duration = wav_duration_sec(wav_path)
        size = wav_path.stat().st_size

        if duration <= self._config.max_audio_sec and size <= TRANSCRIBE_MAX_BYTES:
            text = await self._request_with_retry(self._transcribe_short, wav_path)
            return text_to_transcript(text, duration)

        return await self._transcribe_long(wav_path, duration)

    async def _transcribe_long(self, wav_path: Path, duration: float) -> Transcript:
        push_result = await self._request_with_retry(self._stt_long_push, wav_path)
        job_uuid = push_result["uuid"]

        loop = asyncio.get_running_loop()
        deadline = loop.time() + poll_timeout_sec(duration)
        while True:
            items = await self._request_with_retry(self._stt_long_poll, job_uuid)
            if items and all(item.get("done") for item in items):
                chunk_results: list[tuple[float, Transcript]] = []
                offset = 0.0
                for item in items:
                    chunk_duration = float(item.get("duration") or 0.0)
                    chunk_transcript = text_to_transcript(item.get("transcription", ""), chunk_duration)
                    chunk_results.append((offset, chunk_transcript))
                    offset += chunk_duration
                return merge_transcripts(chunk_results)

            if loop.time() > deadline:
                timeout = poll_timeout_sec(duration)
                raise ChimegeError(f"Chimege stt-long job {job_uuid} did not finish within {timeout:.0f}s")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def _request_with_retry(self, call: Callable[..., Awaitable[T]], *args: object) -> T:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await call(*args)
            except httpx.TimeoutException as e:
                last_error = e
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise ChimegeError(_describe_http_error(e)) from e
                last_error = e

            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_BASE_SEC * (BACKOFF_FACTOR ** (attempt - 1))
                logger.warning(
                    "Chimege request attempt %d/%d failed, retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

        raise ChimegeError(f"Chimege request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    async def _transcribe_short(self, wav_path: Path) -> str:
        headers = {
            "Token": self._config.token,
            "Content-Type": "application/octet-stream",
            "Punctuate": "true",
        }
        data = wav_path.read_bytes()
        response = await self._client.post(f"{self._config.url}/transcribe", headers=headers, content=data)
        response.raise_for_status()
        return response.text

    async def _stt_long_push(self, wav_path: Path) -> dict:
        headers = {"Token": self._config.token, "Content-Type": "audio/wav"}
        data = wav_path.read_bytes()
        response = await self._client.post(f"{self._config.url}/stt-long", headers=headers, content=data)
        response.raise_for_status()
        return response.json()

    async def _stt_long_poll(self, job_uuid: str) -> list[dict]:
        headers = {"Token": self._config.token, "UUID": job_uuid}
        response = await self._client.get(f"{self._config.url}/stt-long-transcript", headers=headers)
        response.raise_for_status()
        return response.json()


def _describe_http_error(e: httpx.HTTPStatusError) -> str:
    status = e.response.status_code
    error_code_header = e.response.headers.get("Error-Code")
    detail = None
    if error_code_header is not None:
        try:
            code = int(error_code_header)
            detail = _TRANSCRIBE_ERROR_CODES.get(code) or _TOKEN_ERROR_CODES.get(code)
        except ValueError:
            pass
    if detail:
        return f"Chimege request failed ({status}, Error-Code {error_code_header}): {detail}"
    body = e.response.text[:300]
    return f"Chimege request failed ({status}): {body}"
