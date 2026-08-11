"""Chimege.mn STT client — the ONLY speech-to-text implementation (see HARD RULES §0).

Every Chimege-specific assumption (endpoint shape, auth header, request/response
JSON) lives in this one file, driven by CHIMEGE_STT_URL / CHIMEGE_TOKEN /
CHIMEGE_MAX_AUDIO_SEC from config. The exact contract implemented against is
documented in docs/CHIMEGE.md — correct that doc (and this file) together if
the real API differs; nothing outside this file should need to change.

Everything downstream of `transcribe()` only ever sees a normalized `Transcript`.
"""
from __future__ import annotations

import asyncio
import re
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.models import Segment, Transcript, Word
from app.utils.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 1.0
BACKOFF_FACTOR = 2.0

DEFAULT_CHUNK_OVERLAP_SEC = 0.5
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DURATION_SEC = 0.5

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


class ChimegeError(Exception):
    pass


# --------------------------------------------------------------------------
# Pure helpers: chunk boundary math, timestamp shifting, merging, synthesis.
# Kept dependency-free (no network, no subprocess) so they're unit-testable
# with a fake client / canned data.
# --------------------------------------------------------------------------


def wav_duration_sec(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate) if rate else 0.0


def compute_chunk_boundaries(
    total_duration: float,
    max_chunk_sec: float,
    silences: list[tuple[float, float]] | None = None,
    overlap_sec: float = DEFAULT_CHUNK_OVERLAP_SEC,
) -> list[tuple[float, float]]:
    """Splits [0, total_duration] into chunks no longer than max_chunk_sec.

    Prefers to cut inside a detected silence interval near each boundary
    (silence chosen: midpoint of the interval that contains, or is closest
    before, the target cut point). Falls back to a fixed-length cut with
    `overlap_sec` of overlap into the next chunk when no usable silence
    exists near the boundary.
    """
    if total_duration <= 0:
        return []
    if total_duration <= max_chunk_sec:
        return [(0.0, total_duration)]

    silences = sorted(silences or [])
    boundaries: list[tuple[float, float]] = []
    start = 0.0

    while start < total_duration:
        target_end = start + max_chunk_sec
        if target_end >= total_duration:
            boundaries.append((start, total_duration))
            break

        cut = _find_silence_cut(silences, start, target_end)
        if cut is None:
            # Fixed-length fallback: hard cut at target_end, next chunk backs
            # up by overlap_sec so words spoken right at the cut aren't lost.
            end = target_end
            next_start = max(start, target_end - overlap_sec)
        else:
            end = cut
            next_start = cut

        boundaries.append((start, end))
        start = next_start

    return boundaries


def _find_silence_cut(
    silences: list[tuple[float, float]], chunk_start: float, target_end: float
) -> float | None:
    """Picks the silence interval whose midpoint is <= target_end and closest
    to it, among silences that start after chunk_start. None if none qualify.
    """
    best: float | None = None
    for s_start, s_end in silences:
        if s_start <= chunk_start:
            continue
        midpoint = (s_start + s_end) / 2
        if midpoint > target_end:
            continue
        if best is None or midpoint > best:
            best = midpoint
    return best


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
            words=[Word(start=w.start + offset_sec, end=w.end + offset_sec, text=w.text) for w in seg.words],
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
    """Used when Chimege returns plain text with no word/segment timings:
    splits on sentence boundaries and allocates time proportional to each
    sentence's share of the total character count.
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
                id=uuid.uuid4().hex,
                start=cursor,
                end=min(duration_sec, cursor + seg_duration),
                text=sentence,
                words=[],
            )
        )
        cursor += seg_duration

    return segments


def parse_silencedetect_output(stderr_text: str) -> list[tuple[float, float]]:
    """Parses ffmpeg `silencedetect` filter stderr into (start, end) intervals.

    Lines look like:
      [silencedetect @ 0x...] silence_start: 12.34
      [silencedetect @ 0x...] silence_end: 13.01 | silence_duration: 0.67
    A trailing silence_start with no matching silence_end is dropped (the
    interval never closes, so it isn't usable as a cut point).
    """
    intervals: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in stderr_text.splitlines():
        start_m = re.search(r"silence_start:\s*([\d.]+)", line)
        if start_m:
            pending_start = float(start_m.group(1))
            continue
        end_m = re.search(r"silence_end:\s*([\d.]+)", line)
        if end_m and pending_start is not None:
            intervals.append((pending_start, float(end_m.group(1))))
            pending_start = None
    return intervals


def _to_transcript(raw: dict, chunk_duration: float) -> Transcript:
    """Normalizes one Chimege API response into a Transcript with
    chunk-relative timestamps (offset shifting happens later, in merge).

    Assumed shape (see docs/CHIMEGE.md):
        {"language": "mn", "segments": [{"start", "end", "text", "words": [...]}], "text": "..."}
    or, when the provider has no timing info:
        {"language": "mn", "text": "..."}
    """
    language = raw.get("language") or "mn"
    raw_segments = raw.get("segments")

    if raw_segments:
        segments = [
            Segment(
                id=uuid.uuid4().hex,
                start=float(s["start"]),
                end=float(s["end"]),
                text=s["text"],
                words=[
                    Word(start=float(w["start"]), end=float(w["end"]), text=w["text"])
                    for w in s.get("words", [])
                ],
            )
            for s in raw_segments
        ]
        full_text = raw.get("text") or " ".join(s.text for s in segments)
        return Transcript(language=language, segments=segments, full_text=full_text, timings_estimated=False)

    text = raw.get("text", "")
    segments = synthesize_segments_from_text(text, chunk_duration)
    return Transcript(language=language, segments=segments, full_text=text, timings_estimated=True)


# --------------------------------------------------------------------------
# The client itself.
# --------------------------------------------------------------------------


@dataclass
class ChimegeConfig:
    url: str
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
        if duration <= self._config.max_audio_sec:
            raw = await self._transcribe_chunk_with_retry(wav_path)
            return _to_transcript(raw, duration)

        silences = await self._detect_silences(wav_path)
        boundaries = compute_chunk_boundaries(duration, self._config.max_audio_sec, silences)

        chunk_results: list[tuple[float, Transcript]] = []
        tmp_dir = wav_path.parent / f"{wav_path.stem}_chunks"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            for i, (start, end) in enumerate(boundaries):
                chunk_path = tmp_dir / f"chunk_{i:03d}.wav"
                await self._extract_chunk(wav_path, chunk_path, start, end)
                raw = await self._transcribe_chunk_with_retry(chunk_path)
                chunk_transcript = _to_transcript(raw, end - start)
                chunk_results.append((start, chunk_transcript))
        finally:
            for f in tmp_dir.glob("*.wav"):
                f.unlink(missing_ok=True)
            tmp_dir.rmdir()

        return merge_transcripts(chunk_results)

    async def _transcribe_chunk_with_retry(self, wav_path: Path) -> dict:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await self._post_chunk(wav_path)
            except httpx.TimeoutException as e:
                last_error = e
            except httpx.HTTPStatusError as e:
                if e.response.status_code not in RETRYABLE_STATUS_CODES:
                    raise ChimegeError(f"Chimege request failed: {e}") from e
                last_error = e

            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_BASE_SEC * (BACKOFF_FACTOR ** (attempt - 1))
                logger.warning(
                    "Chimege request attempt %d/%d failed, retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, delay,
                )
                await asyncio.sleep(delay)

        raise ChimegeError(f"Chimege request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    async def _post_chunk(self, wav_path: Path) -> dict:
        headers = {"Authorization": f"Bearer {self._config.token}"}
        with wav_path.open("rb") as f:
            files = {"audio": (wav_path.name, f, "audio/wav")}
            response = await self._client.post(self._config.url, headers=headers, files=files)
        response.raise_for_status()
        return response.json()

    async def _detect_silences(self, wav_path: Path) -> list[tuple[float, float]]:
        from app.video.ffmpeg import discover_ffmpeg

        binaries = discover_ffmpeg()
        if binaries.ffmpeg is None:
            return []
        args = [
            str(binaries.ffmpeg), "-hide_banner", "-i", str(wav_path),
            "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_DURATION_SEC}",
            "-f", "null", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        return parse_silencedetect_output(stderr.decode(errors="replace"))

    async def _extract_chunk(self, wav_path: Path, out_path: Path, start: float, end: float) -> None:
        from app.video.ffmpeg import discover_ffmpeg

        binaries = discover_ffmpeg()
        if binaries.ffmpeg is None:
            raise ChimegeError("FFmpeg not available for audio chunking")
        args = [
            str(binaries.ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(wav_path),
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-c", "copy",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ChimegeError(f"Audio chunk extraction failed: {stderr.decode(errors='replace')[-300:]}")
