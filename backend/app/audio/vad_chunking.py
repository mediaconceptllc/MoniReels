"""Turns VAD speech segments into Chimege-sized request chunks and turns
Chimege's response back into a Transcript with real, gap-free timing.

Three concerns, deliberately kept in one module since they're one pipeline
step conceptually:

1. group_vad_segments_into_chunks - pure grouping, mirrors
   chimege_client.compute_pause_boundaries' merge/force-split rules but
   operates directly on "keep" (speech) intervals instead of deriving cut
   points from detected silence.
2. synthesize_segments_for_chunk / chunk_text_to_transcript - maps one
   chunk's single returned text string back onto the real VAD timestamps
   it was built from. When several VAD segments were merged into one
   request, every emitted segment's [start, end] is still one real,
   exact VAD segment (never a span crossing the dropped gap between two of
   them) - only the *text* split across segments is an estimate (by each
   segment's share of the chunk's word count), an unavoidable consequence
   of Chimege never returning per-segment text for a merged request.
3. extract_voice_only_wav - the actual "make only voice parts" audio step:
   slices and concatenates just the given segments out of a loaded
   waveform, dropping silence/music in between, so nothing but detected
   speech is ever sent to Chimege.

(1) and (2) are pure functions with no torch dependency, so they're
unit-testable without the optional ML dependencies installed; only (3)
needs torch/torchaudio, imported lazily inside the function that needs it.
"""
from __future__ import annotations

import asyncio
import uuid as uuid_lib
from pathlib import Path

from app.models import Segment, Transcript
from app.stt.chimege_client import (
    FORCED_CUT_OVERLAP_SEC,
    TARGET_CHUNK_MIN_SEC,
    synthesize_segments_from_text,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

VadSegment = tuple[float, float]


class VoiceExtractionError(Exception):
    pass


# --------------------------------------------------------------------------
# 1. Grouping VAD segments into Chimege-sized chunks.
# --------------------------------------------------------------------------


def group_vad_segments_into_chunks(
    segments: list[VadSegment],
    max_chunk_sec: float,
    min_chunk_sec: float = TARGET_CHUNK_MIN_SEC,
    overlap_sec: float = FORCED_CUT_OVERLAP_SEC,
) -> list[list[VadSegment]]:
    """Groups ordered, non-overlapping VAD speech segments into chunks whose
    *speech-only* duration (sum of segment durations, not wall-clock span)
    stays <= max_chunk_sec. Short adjacent segments are merged into the same
    chunk until min_chunk_sec of real speech is reached, so no chunk is
    rejected as too short by Chimege. A single segment longer than
    max_chunk_sec on its own (VAD found no pause during it) is force-split,
    with a small overlap, same fallback as compute_pause_boundaries.
    """
    if not segments:
        return []

    normalized: list[VadSegment] = []
    for start, end in segments:
        if end - start <= max_chunk_sec:
            normalized.append((start, end))
            continue
        cursor = start
        while end - cursor > max_chunk_sec:
            forced_end = cursor + max_chunk_sec
            normalized.append((cursor, forced_end))
            cursor = max(cursor, forced_end - overlap_sec)
        if end - cursor > 0:
            normalized.append((cursor, end))

    chunks: list[list[VadSegment]] = []
    current: list[VadSegment] = []
    current_speech_sec = 0.0
    for seg in normalized:
        seg_dur = seg[1] - seg[0]
        if current and current_speech_sec + seg_dur > max_chunk_sec:
            chunks.append(current)
            current = []
            current_speech_sec = 0.0
        current.append(seg)
        current_speech_sec += seg_dur
    if current:
        chunks.append(current)

    merged: list[list[VadSegment]] = []
    for chunk in chunks:
        chunk_dur = sum(e - s for s, e in chunk)
        if merged:
            prev_dur = sum(e - s for s, e in merged[-1])
            if chunk_dur < min_chunk_sec and prev_dur + chunk_dur <= max_chunk_sec:
                merged[-1] = merged[-1] + chunk
                continue
        merged.append(list(chunk))

    if len(merged) > 1:
        first_dur = sum(e - s for s, e in merged[0])
        if first_dur < min_chunk_sec:
            merged[1] = merged[0] + merged[1]
            merged = merged[1:]

    return merged


# --------------------------------------------------------------------------
# 2. Mapping one chunk's returned text back onto real VAD timestamps.
# --------------------------------------------------------------------------


def synthesize_segments_for_chunk(text: str, chunk_segments: list[VadSegment]) -> list[Segment]:
    """Chimege returns one text string per chunk, never per-sentence timing.

    When the chunk is exactly one VAD segment, that segment's boundaries
    are already exact - delegates to chimege_client.synthesize_segments_from_text
    (proportional-by-character sentence splitting) shifted onto that
    segment's real [start, end], same as the legacy pause-chunking path.

    When several VAD segments were merged into one chunk (to meet
    Chimege's minimum request size), splits the text on *word* boundaries
    and allocates each real segment a share of the words proportional to
    its own share of the chunk's total speech duration. Every emitted
    segment's [start, end] is therefore still one real, exact VAD
    boundary - it can never span the dropped gap between two segments the
    way a proportional-by-time split of the whole chunk would. Only the
    word-to-segment attribution is an estimate, an unavoidable consequence
    of Chimege never returning per-segment text for a merged request.
    """
    text = text.strip()
    if not text or not chunk_segments:
        return []

    total_speech_sec = sum(e - s for s, e in chunk_segments)
    if total_speech_sec <= 0:
        return []

    if len(chunk_segments) == 1:
        start, end = chunk_segments[0]
        return [
            Segment(id=s.id, start=start + s.start, end=start + s.end, text=s.text, words=[])
            for s in synthesize_segments_from_text(text, end - start)
        ]

    words = text.split()
    total_words = len(words) or 1
    segments: list[Segment] = []
    word_idx = 0
    for i, (seg_start, seg_end) in enumerate(chunk_segments):
        is_last = i == len(chunk_segments) - 1
        if is_last:
            end_word_idx = total_words
        else:
            share = (seg_end - seg_start) / total_speech_sec
            end_word_idx = min(total_words, word_idx + round(total_words * share))
        seg_words = words[word_idx:end_word_idx]
        word_idx = end_word_idx
        if seg_words:
            segments.append(
                Segment(
                    id=uuid_lib.uuid4().hex,
                    start=seg_start,
                    end=seg_end,
                    text=" ".join(seg_words),
                    words=[],
                )
            )

    return segments


def chunk_text_to_transcript(text: str, chunk_segments: list[VadSegment], language: str = "mn") -> Transcript:
    segments = synthesize_segments_for_chunk(text, chunk_segments)
    exact = len(chunk_segments) == 1 and len(segments) <= 1
    return Transcript(
        language=language, segments=segments, full_text=text.strip(), timings_estimated=not exact
    )


# --------------------------------------------------------------------------
# 3. "Make only voice parts": slice + concatenate just the given segments.
# --------------------------------------------------------------------------


def _extract_voice_only_wav_sync(wav_path: Path, segments: list[VadSegment], out_path: Path) -> None:
    import torch
    import torchaudio

    wav, sr = torchaudio.load(str(wav_path))
    pieces = []
    for start, end in segments:
        start_idx = max(0, int(round(start * sr)))
        end_idx = min(wav.shape[-1], int(round(end * sr)))
        if end_idx > start_idx:
            pieces.append(wav[:, start_idx:end_idx])
    if not pieces:
        raise ValueError("No non-empty segments to extract")
    torchaudio.save(str(out_path), torch.cat(pieces, dim=-1), sr)


async def extract_voice_only_wav(wav_path: Path, segments: list[VadSegment], out_path: Path) -> None:
    """Writes just `segments` from `wav_path`, concatenated with every gap
    between them dropped, to `out_path` - the source audio itself is only
    ever read, never modified.
    """
    try:
        await asyncio.to_thread(_extract_voice_only_wav_sync, wav_path, segments, out_path)
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as a typed error
        raise VoiceExtractionError(f"Voice-only extraction failed for {wav_path}: {e}") from e
