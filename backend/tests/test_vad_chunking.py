"""app.audio.vad_chunking unit tests.

group_vad_segments_into_chunks / synthesize_segments_for_chunk /
chunk_text_to_transcript are pure functions (no torch import at module
scope) so this suite runs without the optional ML dependencies installed.
extract_voice_only_wav needs real torch/torchaudio - it's exercised with
real (tiny, synthetic) audio since this dev environment has them installed;
skip that class if torch/torchaudio aren't importable elsewhere.
"""
from __future__ import annotations

import pytest

from app.audio.vad_chunking import (
    chunk_text_to_transcript,
    extract_voice_only_wav,
    group_vad_segments_into_chunks,
    synthesize_segments_for_chunk,
)
from app.stt.chunking import MIN_CHUNK_SEC, TARGET_CHUNK_MIN_SEC

torch = pytest.importorskip("torch")
torchaudio = pytest.importorskip("torchaudio")


# --------------------------------------------------------------------------
# group_vad_segments_into_chunks
# --------------------------------------------------------------------------


def test_group_empty_segments():
    assert group_vad_segments_into_chunks([], max_chunk_sec=55.0) == []


def test_group_single_short_segment_is_its_own_chunk():
    chunks = group_vad_segments_into_chunks([(1.0, 2.0)], max_chunk_sec=55.0)
    assert chunks == [[(1.0, 2.0)]]


def test_group_merges_short_adjacent_segments_to_meet_min_chunk():
    # Three short bursts of speech (0.5s each) well under
    # TARGET_CHUNK_MIN_SEC=5s - must be merged into one chunk.
    segments = [(0.0, 0.5), (2.0, 2.5), (4.0, 4.5)]
    chunks = group_vad_segments_into_chunks(segments, max_chunk_sec=55.0, min_chunk_sec=5.0)
    assert len(chunks) == 1
    assert chunks[0] == segments


def test_group_does_not_merge_past_max_chunk_sec():
    # Two 6s-long speech segments - each is already big enough alone
    # (>= min_chunk_sec) and merging them would exceed max_chunk_sec=10.
    segments = [(0.0, 6.0), (10.0, 16.0)]
    chunks = group_vad_segments_into_chunks(segments, max_chunk_sec=10.0, min_chunk_sec=5.0)
    assert chunks == [[(0.0, 6.0)], [(10.0, 16.0)]]


def test_group_force_splits_a_single_segment_longer_than_max_chunk():
    # One continuous 25s speech burst, no internal pause - VAD alone can't
    # find a safe cut, so it gets force-split at max_chunk_sec with overlap.
    segments = [(0.0, 25.0)]
    chunks = group_vad_segments_into_chunks(segments, max_chunk_sec=10.0, min_chunk_sec=5.0)
    flat = [seg for chunk in chunks for seg in chunk]
    for start, end in flat:
        assert end - start <= 10.0 + 1e-9
    assert flat[0][0] == 0.0
    assert flat[-1][1] == 25.0


def test_group_merges_too_short_first_chunk_forward():
    # First chunk (one 0.3s burst) has nothing before it to merge into.
    segments = [(0.0, 0.3), (10.0, 16.0)]
    chunks = group_vad_segments_into_chunks(segments, max_chunk_sec=55.0, min_chunk_sec=5.0)
    assert len(chunks) == 1
    assert chunks[0] == segments


def test_group_every_chunk_meets_provider_min_or_stands_alone():
    segments = [(0.0, 1.0), (2.0, 3.0), (20.0, 21.0), (40.0, 60.0)]
    chunks = group_vad_segments_into_chunks(
        segments, max_chunk_sec=25.0, min_chunk_sec=TARGET_CHUNK_MIN_SEC
    )
    for chunk in chunks:
        chunk_dur = sum(e - s for s, e in chunk)
        assert chunk_dur >= MIN_CHUNK_SEC - 1e-9 or len(chunks) == 1


def test_group_preserves_all_segments_in_order():
    segments = [(0.0, 1.0), (3.0, 4.0), (10.0, 30.0), (35.0, 36.0)]
    chunks = group_vad_segments_into_chunks(segments, max_chunk_sec=15.0, min_chunk_sec=5.0)
    flat = [seg for chunk in chunks for seg in chunk]
    assert flat[0][0] == 0.0
    assert flat[-1][1] == 36.0


# --------------------------------------------------------------------------
# synthesize_segments_for_chunk / chunk_text_to_transcript
# --------------------------------------------------------------------------


def test_synthesize_single_segment_single_sentence_is_exact():
    """The core case this module exists for: one VAD segment, one sentence
    -> the segment's real, detected boundaries, not an estimate.
    """
    segments = synthesize_segments_for_chunk("hello there", [(12.5, 14.0)])
    assert len(segments) == 1
    assert segments[0].start == 12.5
    assert segments[0].end == 14.0
    assert segments[0].text == "hello there"


def test_chunk_text_to_transcript_exact_case_not_marked_estimated():
    transcript = chunk_text_to_transcript("hello there", [(12.5, 14.0)])
    assert transcript.timings_estimated is False
    assert transcript.segments[0].start == 12.5
    assert transcript.segments[0].end == 14.0


def test_chunk_text_to_transcript_multi_sentence_marked_estimated():
    transcript = chunk_text_to_transcript("Hi. This is longer.", [(0.0, 10.0)])
    assert transcript.timings_estimated is True
    assert len(transcript.segments) == 2


def test_synthesize_maps_across_merged_segments_not_into_the_gap():
    """Two merged VAD segments with a real gap between them (e.g. 5.0-8.0
    silence dropped) - a sentence's estimated position must land inside one
    of the two real segments, never inside the dropped gap.
    """
    chunk_segments = [(0.0, 5.0), (8.0, 10.0)]  # 7s of real speech total
    segments = synthesize_segments_for_chunk("First sentence. Second one.", chunk_segments)
    for seg in segments:
        in_first = 0.0 <= seg.start <= 5.0 and 0.0 <= seg.end <= 5.0
        in_second = 8.0 <= seg.start <= 10.0 and 8.0 <= seg.end <= 10.0
        assert in_first or in_second, f"segment {seg.start}-{seg.end} falls in the dropped gap"


def test_synthesize_empty_text_returns_no_segments():
    assert synthesize_segments_for_chunk("", [(0.0, 5.0)]) == []


def test_synthesize_empty_chunk_segments_returns_no_segments():
    assert synthesize_segments_for_chunk("hello", []) == []


def test_synthesize_covers_full_speech_span():
    chunk_segments = [(1.0, 3.0), (5.0, 6.0)]
    segments = synthesize_segments_for_chunk("one two three", chunk_segments)
    assert segments[0].start == 1.0
    assert segments[-1].end == 6.0


# --------------------------------------------------------------------------
# extract_voice_only_wav - real (tiny) torchaudio round trip.
# --------------------------------------------------------------------------


def _write_test_wav(path, duration_sec: float, sr: int = 16000):
    import torch as _torch

    n_samples = int(duration_sec * sr)
    wav = _torch.zeros(1, n_samples)
    torchaudio.save(str(path), wav, sr)


@pytest.mark.asyncio
async def test_extract_voice_only_wav_drops_gaps_and_concatenates(tmp_path):
    src = tmp_path / "vocals.wav"
    _write_test_wav(src, duration_sec=10.0)

    out = tmp_path / "voice_only.wav"
    # Two 1s segments out of a 10s file with a gap between them - result
    # should be exactly 2s (1s + 1s), the gap dropped entirely.
    await extract_voice_only_wav(src, [(1.0, 2.0), (7.0, 8.0)], out)

    wav, sr = torchaudio.load(str(out))
    assert wav.shape[-1] == pytest.approx(2.0 * sr, abs=sr * 0.01)


@pytest.mark.asyncio
async def test_extract_voice_only_wav_raises_on_no_segments(tmp_path):
    from app.audio.vad_chunking import VoiceExtractionError

    src = tmp_path / "vocals.wav"
    _write_test_wav(src, duration_sec=2.0)

    with pytest.raises(VoiceExtractionError):
        await extract_voice_only_wav(src, [], tmp_path / "out.wav")
