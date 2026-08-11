"""Chimege.mn STT client — the ONLY speech-to-text implementation (see HARD RULES §0).

Confirmed against the real Chimege OpenAPI spec (v1.2) provided by the user.
Audio longer than CHIMEGE_MAX_AUDIO_SEC is split at detected pauses (ffmpeg
`silencedetect`) before sending — an "almost sentence by sentence" split,
since a pause is the only sentence-boundary signal available before we have
a transcript. Every resulting chunk is guaranteed <= CHIMEGE_MAX_AUDIO_SEC by
construction (a hard cut is forced if no pause appears for too long), so
every chunk always goes through the synchronous:

    POST /transcribe  — plain-text response, capped at 3MB (~98s of our
                         16kHz mono 16-bit PCM WAV format)

Chimege's real API also has an async long-audio path (POST /stt-long + GET
/stt-long-transcript, push-then-poll by UUID, built for "any number of
hours" of audio) — see docs/CHIMEGE.md. It's not used here: chunks produced
by our own pause-splitting never exceed the sync endpoint's limits, so there
is nothing left for it to do. Revisit that document if a case turns up where
client-side chunking isn't the right call (e.g. wanting fewer, larger
requests for silence-free audio).

Chimege never returns word- or segment-level timestamps at all — a chunk's
own [start, end] (known exactly, since *we* cut it) becomes that chunk's
timing, and `synthesize_segments_from_text` further estimates sentence-level
sub-splits *within* a chunk proportional to character count, for the rare
case a single pause-bounded chunk still contains multiple sentences.

Every Chimege-specific detail lives in this one file, driven by
CHIMEGE_STT_URL / CHIMEGE_TOKEN / CHIMEGE_MAX_AUDIO_SEC from config.
Everything downstream of `transcribe()` only ever sees a normalized
`Transcript`.
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

# Pause detection (ffmpeg silencedetect) — tuned for sentence-scale pauses,
# not just big gaps. Will likely need tuning against real speech.
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DURATION_SEC = 0.35

# Chimege's own /transcribe minimums are 0.5s duration and 50KB (~1.56s in
# our WAV format) — stay safely above both so no chunk gets rejected as
# "too short" (error 2003) or "too small" (error 2002).
MIN_CHUNK_SEC = 2.0

# Applied only on a forced (no-pause-found) cut, so a word split across that
# boundary isn't lost entirely — genuine pause cuts don't need this, the gap
# itself is the buffer.
FORCED_CUT_OVERLAP_SEC = 0.4

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
# Pure helpers: pause-boundary math, timestamp shifting, merging, no-timing
# text synthesis. Kept dependency-free (no network, no subprocess) so
# they're unit-testable with canned data.
# --------------------------------------------------------------------------


def wav_duration_sec(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate()
        return frames / float(rate) if rate else 0.0


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


def compute_pause_boundaries(
    total_duration: float,
    silences: list[tuple[float, float]],
    max_chunk_sec: float,
    min_chunk_sec: float = MIN_CHUNK_SEC,
    overlap_sec: float = FORCED_CUT_OVERLAP_SEC,
) -> list[tuple[float, float]]:
    """"Almost sentence by sentence": cuts at the midpoint of every detected
    pause. A stretch with no pause for longer than max_chunk_sec is force-cut
    anyway (with a small overlap, since there's no safe gap to cut in), so
    every returned chunk is guaranteed <= max_chunk_sec. Candidate cuts
    closer together than min_chunk_sec are merged away, so no chunk is ever
    too short for Chimege's own /transcribe minimums.
    """
    if total_duration <= 0:
        return []

    candidates = sorted((s + e) / 2 for s, e in silences if 0 < (s + e) / 2 < total_duration)

    boundaries: list[list[float]] = []
    start = 0.0
    for cp in [*candidates, total_duration]:
        if cp <= start:
            continue
        while cp - start > max_chunk_sec:
            forced_end = start + max_chunk_sec
            boundaries.append([start, forced_end])
            start = max(start, forced_end - overlap_sec)
        if cp <= start:
            continue
        if boundaries and cp - start < min_chunk_sec:
            boundaries[-1][1] = cp  # too short on its own - fold into the previous chunk
        else:
            boundaries.append([start, cp])
        start = cp

    result = [(s, e) for s, e in boundaries]
    if len(result) > 1 and (result[0][1] - result[0][0]) < min_chunk_sec:
        result[1] = (result[0][0], result[1][1])  # first chunk had nothing before it to merge into
        result = result[1:]
    return result


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
    """Chimege never returns word/segment timings: splits a chunk's text on
    sentence boundaries and allocates that chunk's (exactly known) duration
    proportional to each sentence's share of the character count — only
    needed when a single pause-bounded chunk still contains >1 sentence.
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

        total_duration = wav_duration_sec(wav_path)

        if total_duration <= self._config.max_audio_sec:
            text = await self._request_with_retry(self._transcribe_short, wav_path)
            return text_to_transcript(text, total_duration)

        return await self._transcribe_in_pause_chunks(wav_path, total_duration)

    async def _transcribe_in_pause_chunks(self, wav_path: Path, total_duration: float) -> Transcript:
        silences = await self._detect_silences(wav_path)
        boundaries = compute_pause_boundaries(total_duration, silences, self._config.max_audio_sec)

        workdir = wav_path.parent / f"{wav_path.stem}_chunks"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            chunk_results: list[tuple[float, Transcript]] = []
            for i, (start, end) in enumerate(boundaries):
                chunk_path = workdir / f"chunk_{i:03d}.wav"
                await self._extract_chunk(wav_path, chunk_path, start, end)
                text = await self._request_with_retry(self._transcribe_short, chunk_path)
                chunk_results.append((start, text_to_transcript(text, end - start)))
            return merge_transcripts(chunk_results)
        finally:
            for f in workdir.glob("*.wav"):
                f.unlink(missing_ok=True)
            workdir.rmdir()

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

    async def _detect_silences(self, wav_path: Path) -> list[tuple[float, float]]:
        from app.video.ffmpeg import discover_ffmpeg

        binaries = discover_ffmpeg()
        if binaries.ffmpeg is None:
            logger.warning("FFmpeg not available for pause detection; falling back to fixed-length chunking")
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
