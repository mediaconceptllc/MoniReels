"""Chimege.mn STT client — the ONLY speech-to-text implementation (see HARD RULES §0).

Confirmed against the real Chimege OpenAPI spec (v1.2) provided by the user,
and against a real account: `/transcribe` (the synchronous endpoint) 403'd
with Error-Code 1000 "Invalid API token" on a token that `/stt-long` (the
async push+poll endpoint) accepted immediately — some accounts/tokens are
evidently only authorized for the async path. So this client always uses:

    POST /stt-long              — push audio, get back {"uuid", "duration"}
    GET  /stt-long-transcript   — poll by UUID until {"done": true, ...}

Every file is *also* split at detected pauses (ffmpeg `silencedetect`)
before sending — an "almost sentence by sentence" split, since a pause is
the only sentence-boundary signal available before we have a transcript.
This always happens, even for audio shorter than CHIMEGE_MAX_AUDIO_SEC:
nothing is ever sent to Chimege as one whole-file request. Chunks target a
minimum of TARGET_CHUNK_MIN_SEC (5s, so real pauses still drive fine-grained
splitting); the ceiling is CHIMEGE_MAX_AUDIO_SEC itself, not a separate
small constant — a forced cut only kicks in as the fallback for a genuinely
pause-free stretch (someone talking continuously for a while), not as the
common case. Each chunk still goes through its own full push+poll round
trip — see STT_LONG_POLL_INTERVAL_SEC below for why that's an acceptable
trade-off.

`/transcribe` is kept implemented nowhere on purpose: since it 403s outright
for at least one real account, there is no fallback value in keeping dead
code for it. If a token that *does* accept `/transcribe` turns up later and
the async round-trip overhead matters, that's a reason to revisit this.

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

# Some real Chimege accounts/tokens are only authorized for the async
# push+poll endpoint, not the synchronous one - confirmed by probing both
# directly with a real token: /transcribe returned 403 Error-Code 1000
# ("Invalid API token") while /stt-long accepted the exact same token and
# returned a real job UUID. Spec says "no more often than every 1 second"
# and ~1h of audio takes ~4 minutes to process - even a chunk at the full
# CHIMEGE_MAX_AUDIO_SEC ceiling is a small fraction of that, so this timeout
# stays comfortable even though chunks are no longer capped tiny.
STT_LONG_POLL_INTERVAL_SEC = 1.5
STT_LONG_POLL_TIMEOUT_SEC = 30.0

# Pause detection (ffmpeg silencedetect) — tuned for sentence-scale pauses,
# not just big gaps. Will likely need tuning against real speech.
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DURATION_SEC = 0.35

# Chimege's own /transcribe minimums are 0.5s duration and 50KB (~1.56s in
# our WAV format) — absolute floor a chunk must never go below, regardless
# of target size, or it gets rejected as "too short" (error 2003) or "too
# small" (error 2002).
MIN_CHUNK_SEC = 2.0

# Minimum chunk target for pause-based splitting, applied to every file
# regardless of length: candidate cuts closer together than this are merged
# away, so a chunk is never rejected as "too short"/"too small" and real
# speech pauses still drive fine-grained splitting when they occur ("almost
# sentence by sentence"). The *upper* bound is intentionally NOT a small
# constant here — it's CHIMEGE_MAX_AUDIO_SEC (self._config.max_audio_sec),
# since a low forced-cut ceiling chops continuous, pause-free speech (some
# people just talk for a while without stopping) into awkward pieces for no
# benefit; a forced cut should only ever be the fallback for genuinely
# pause-free stretches, not the common case.
TARGET_CHUNK_MIN_SEC = 5.0

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
        if boundaries and cp - start < min_chunk_sec and cp - boundaries[-1][0] <= max_chunk_sec:
            boundaries[-1][1] = cp  # too short on its own - fold into the previous chunk
        else:
            # Folding in would push the previous chunk past max_chunk_sec (can
            # happen right after a forced cut, when the leftover tail is both
            # too short to stand alone by the target and too big to absorb) -
            # keep it as its own short trailing chunk instead.
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


def split_into_sentences(text: str) -> list[str]:
    """Splits on sentence-ending punctuation; a bare fallback of the whole
    text as one "sentence" if none is found. Shared by every place that
    apportions one chunk's Chimege text across sub-positions by character
    count (see synthesize_segments_from_text and
    app.audio.vad_chunking.synthesize_segments_for_chunk).
    """
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return sentences or [text]


def synthesize_segments_from_text(text: str, duration_sec: float) -> list[Segment]:
    """Chimege never returns word/segment timings: splits a chunk's text on
    sentence boundaries and allocates that chunk's (exactly known) duration
    proportional to each sentence's share of the character count — only
    needed when a single pause-bounded chunk still contains >1 sentence.
    """
    text = text.strip()
    if not text or duration_sec <= 0:
        return []

    sentences = split_into_sentences(text)
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

    @property
    def max_audio_sec(self) -> float:
        return self._config.max_audio_sec

    async def transcribe_chunk_text(self, chunk_wav_path: Path) -> str:
        """Pushes one already-cut audio chunk and returns its raw
        transcribed text, with no timing attached. The shared network
        primitive (push + poll + retry) that both this client's own
        silence-based chunking below and an external VAD-based chunking
        pipeline (see app.stt.pipeline) build on.
        """
        return await self._request_with_retry(self._transcribe_chunk, chunk_wav_path)

    async def transcribe(self, wav_path: Path) -> Transcript:
        if not self._config.url or not self._config.token:
            raise ChimegeError("CHIMEGE_STT_URL / CHIMEGE_TOKEN are not configured")

        total_duration = wav_duration_sec(wav_path)

        # Always pause-split before sending anything to Chimege, even audio
        # shorter than CHIMEGE_MAX_AUDIO_SEC — a single request per file is
        # never sent whole; compute_pause_boundaries degrades to one chunk
        # covering [0, total_duration] on its own for very short clips.
        return await self._transcribe_in_pause_chunks(wav_path, total_duration)

    async def _transcribe_in_pause_chunks(self, wav_path: Path, total_duration: float) -> Transcript:
        silences = await self._detect_silences(wav_path)
        boundaries = compute_pause_boundaries(
            total_duration, silences, self._config.max_audio_sec, min_chunk_sec=TARGET_CHUNK_MIN_SEC
        )
        logger.info(
            "Splitting %.1fs audio into %d pause-based chunks before sending to Chimege: %s",
            total_duration,
            len(boundaries),
            ", ".join(f"[{s:.1f}-{e:.1f}]" for s, e in boundaries),
        )

        workdir = wav_path.parent / f"{wav_path.stem}_chunks"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            chunk_results: list[tuple[float, Transcript]] = []
            for i, (start, end) in enumerate(boundaries):
                chunk_path = workdir / f"chunk_{i:03d}.wav"
                await self._extract_chunk(wav_path, chunk_path, start, end)
                logger.info("Sending chunk %d/%d (%.1fs-%.1fs) to Chimege", i + 1, len(boundaries), start, end)
                text = await self.transcribe_chunk_text(chunk_path)
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

    async def _transcribe_chunk(self, wav_path: Path) -> str:
        """Pushes one chunk via /stt-long, then polls /stt-long-transcript
        until done. Used for every chunk, not just ones over CHIMEGE_MAX_AUDIO_SEC
        — some tokens are only authorized for this async endpoint, not the
        synchronous /transcribe one (see STT_LONG_POLL_INTERVAL_SEC above).
        """
        push_headers = {"Token": self._config.token, "Content-Type": "application/octet-stream"}
        data = wav_path.read_bytes()
        push_response = await self._client.post(f"{self._config.url}/stt-long", headers=push_headers, content=data)
        push_response.raise_for_status()
        try:
            job_uuid = push_response.json()["uuid"]
        except (ValueError, KeyError) as e:
            raise ChimegeError(f"Unexpected /stt-long response shape: {push_response.text[:200]!r}") from e

        poll_headers = {"Token": self._config.token, "UUID": job_uuid}
        elapsed = 0.0
        while elapsed < STT_LONG_POLL_TIMEOUT_SEC:
            await asyncio.sleep(STT_LONG_POLL_INTERVAL_SEC)
            elapsed += STT_LONG_POLL_INTERVAL_SEC
            poll_response = await self._client.get(f"{self._config.url}/stt-long-transcript", headers=poll_headers)
            poll_response.raise_for_status()
            payload = poll_response.json()
            # Spec says this is always an array, but a real account has been
            # observed returning a bare object for a single-segment job, and
            # separately a list of plain strings (not objects) mid-processing
            # — normalize defensively rather than trust either shape.
            items = payload if isinstance(payload, list) else [payload]
            if items and all(isinstance(item, dict) and item.get("done") for item in items):
                return "".join(item.get("transcription", "") for item in items if isinstance(item, dict))

        raise ChimegeError(f"Chimege stt-long job {job_uuid} did not complete within {STT_LONG_POLL_TIMEOUT_SEC:.0f}s")

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
