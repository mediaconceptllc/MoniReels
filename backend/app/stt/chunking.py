"""Provider-agnostic audio chunking and transcript assembly.

Extracted verbatim from the old Chimege client, where this logic sat mixed
in with that one vendor's transport. None of it is vendor-specific: every
Mongolian STT API this project has met takes a bounded WAV and returns text
with no timing, so "cut at real pauses, remember exactly where each cut was,
put the text back on those known boundaries" is the shape regardless of who
answers the request.

Keeping it here is what makes swapping the provider a transport change
instead of a rewrite — and it keeps these pure functions unit-testable with
canned data, no network and no ffmpeg.

The one rule worth restating: a chunk's [start, end] is known EXACTLY,
because we made the cut. Timing therefore comes from our own boundaries and
never from the provider. Only the split of text *within* one chunk is an
estimate.
"""
from __future__ import annotations

import asyncio
import re
import uuid as uuid_lib
import wave
from pathlib import Path

from app.models import Segment, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)


class ChunkingError(Exception):
    pass


# Pause detection (ffmpeg silencedetect) — tuned for sentence-scale pauses,
# not just big gaps between paragraphs.
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DURATION_SEC = 0.35

# Absolute floor for one request. Most STT APIs reject audio shorter than
# ~0.5s or smaller than a few tens of KB outright.
MIN_CHUNK_SEC = 2.0

# Target minimum for pause-based splitting: candidate cuts closer together
# than this are merged away, so real pauses still drive fine-grained
# splitting without producing chunks the API will refuse. The UPPER bound is
# deliberately the configured max, not a small constant — a low forced-cut
# ceiling chops continuous speech into awkward pieces for no benefit. A
# forced cut is the fallback for genuinely pause-free stretches only.
TARGET_CHUNK_MIN_SEC = 5.0

# Applied only on a forced (no-pause-found) cut, so a word straddling that
# boundary is not lost. Genuine pause cuts need no overlap — the silence is
# itself the buffer.
FORCED_CUT_OVERLAP_SEC = 0.4

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


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
    too short for the provider's own minimum request size.
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
    see test_chunking.py for the regression test.
    """
    shifted_segments = [
        Segment(
            id=seg.id,
            start=seg.start + offset_sec,
            end=seg.end + offset_sec,
            text=seg.text,
            speaker=seg.speaker,
            words=[],  # no provider here returns word-level timings (see module docstring)
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
    apportions one chunk's returned text across sub-positions by character
    count (see synthesize_segments_from_text and
    app.audio.vad_chunking.synthesize_segments_for_chunk).
    """
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return sentences or [text]


def synthesize_segments_from_text(text: str, duration_sec: float) -> list[Segment]:
    """No provider here returns word/segment timings: splits a chunk's text on
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


# ---------------------------------------------------------------------------
# ffmpeg-backed helpers. Separated from the pure functions above so those stay
# testable without a binary on PATH.
# ---------------------------------------------------------------------------


async def detect_silences(ffmpeg: Path, wav_path: Path) -> list[tuple[float, float]]:
    """Silence intervals in `wav_path`, or [] when detection is unavailable.

    Returning [] rather than raising is deliberate: with no pauses found,
    compute_pause_boundaries falls back to fixed-length chunks, which is
    worse but still works. Losing the transcription entirely is not a
    reasonable answer to "the pause detector didn't run".
    """
    args = [
        str(ffmpeg), "-hide_banner", "-i", str(wav_path),
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_DURATION_SEC}",
        "-f", "null", "-",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    return parse_silencedetect_output(stderr.decode(errors="replace"))


async def extract_chunk(ffmpeg: Path, wav_path: Path, out_path: Path, start: float, end: float) -> None:
    """Cut [start, end] out of a WAV with a stream copy — no re-encode, so
    the bytes an STT provider receives are the source's own samples."""
    args = [
        str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
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
        raise ChunkingError(f"Audio chunk extraction failed: {stderr.decode(errors='replace')[-300:]}")
