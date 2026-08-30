"""Unit tests for the provider-agnostic chunking math (app.stt.chunking).

These moved wholesale from the old Chimege adapter's test file, unchanged:
none of them ever tested Chimege. They test the arithmetic that decides where
audio is cut and how the returned text is placed back onto those cuts, which
is identical whichever provider answers the request.

The offset-merge test earns its place specifically: forgetting to add each
chunk's start offset before merging is the single most common bug in a
chunked STT pipeline, and it produces a transcript that looks correct until
someone checks a timestamp.
"""
from __future__ import annotations

import wave

import pytest

from app.models import Segment, Transcript
from app.stt.chunking import (
    MIN_CHUNK_SEC,
    TARGET_CHUNK_MIN_SEC,
    compute_pause_boundaries,
    merge_transcripts,
    parse_silencedetect_output,
    shift_transcript,
    split_into_sentences,
    synthesize_segments_from_text,
    text_to_transcript,
    wav_duration_sec,
)


def _segment(start: float, end: float, text: str) -> Segment:
    return Segment(id="seg", start=start, end=end, text=text)


def _write_wav(path, duration_sec: float, framerate: int = 1000) -> None:
    """Writes a WAV whose duration is `duration_sec` at a fake low framerate,
    so tests stay fast without needing realistic audio data.
    """
    nframes = int(duration_sec * framerate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(b"\x00\x00" * nframes)


# --------------------------------------------------------------------------
# Pause-boundary computation — the "almost sentence by sentence" split.
# --------------------------------------------------------------------------


def test_compute_pause_boundaries_short_audio_single_chunk():
    assert compute_pause_boundaries(30.0, silences=[], max_chunk_sec=55.0) == [(0.0, 30.0)]


def test_compute_pause_boundaries_cuts_at_silence_midpoints():
    # Two clean pauses in a 30s clip -> three "sentence" chunks.
    boundaries = compute_pause_boundaries(
        30.0, silences=[(9.0, 9.4), (19.0, 19.6)], max_chunk_sec=55.0
    )
    assert boundaries == [(0.0, 9.2), (9.2, 19.3), (19.3, 30.0)]


def test_compute_pause_boundaries_covers_whole_duration_no_gaps():
    boundaries = compute_pause_boundaries(30.0, silences=[(9.0, 9.4), (19.0, 19.6)], max_chunk_sec=55.0)
    assert boundaries[0][0] == 0.0
    assert boundaries[-1][1] == 30.0
    for (_s1, e1), (s2, _e2) in zip(boundaries, boundaries[1:], strict=False):
        assert s2 == e1  # genuine pause cuts have no overlap, unlike forced cuts


def test_compute_pause_boundaries_forces_cut_when_no_pause_for_too_long():
    # No silences at all in 100s -> forced cuts every max_chunk_sec, with overlap.
    boundaries = compute_pause_boundaries(100.0, silences=[], max_chunk_sec=40.0)
    assert boundaries[0] == (0.0, 40.0)
    assert boundaries[1][0] == 40.0 - 0.4  # FORCED_CUT_OVERLAP_SEC
    assert boundaries[-1][1] == 100.0
    for start, end in boundaries:
        assert end - start <= 40.0 + 1e-9


def test_compute_pause_boundaries_merges_cuts_closer_than_min_chunk():
    # Two pauses only 1s apart (< MIN_CHUNK_SEC=2.0) collapse into one cut.
    boundaries = compute_pause_boundaries(30.0, silences=[(9.0, 9.2), (10.0, 10.2)], max_chunk_sec=55.0)
    assert len(boundaries) == 2
    assert boundaries[0][1] == boundaries[1][0]
    assert boundaries[0][1] - boundaries[0][0] >= MIN_CHUNK_SEC or boundaries[0][0] == 0.0


def test_compute_pause_boundaries_merges_too_short_first_chunk_forward():
    # A pause at 0.5s would make the first chunk shorter than MIN_CHUNK_SEC on
    # its own, with nothing before it to merge into — must merge forward.
    boundaries = compute_pause_boundaries(30.0, silences=[(0.4, 0.6)], max_chunk_sec=55.0)
    assert boundaries[0][0] == 0.0
    assert boundaries[0][1] - boundaries[0][0] >= MIN_CHUNK_SEC


def test_compute_pause_boundaries_zero_duration():
    assert compute_pause_boundaries(0.0, silences=[], max_chunk_sec=55.0) == []


def test_compute_pause_boundaries_forced_cut_tail_stays_short_instead_of_ballooning():
    """A forced cut can leave a leftover tail shorter than min_chunk_sec.
    Folding it into the previous chunk would push that chunk past
    max_chunk_sec — it must stay a short trailing chunk instead.
    """
    # No pauses at all -> one forced cut at max_chunk_sec, leaving a short
    # tail below TARGET_CHUNK_MIN_SEC but still above Chimege's real minimum.
    # This is a pure-function test of compute_pause_boundaries itself, so
    # max_chunk_sec here is just a test value, not tied to any client config.
    max_chunk_sec = 8.0
    total = max_chunk_sec + 2.4
    boundaries = compute_pause_boundaries(
        total, silences=[], max_chunk_sec=max_chunk_sec, min_chunk_sec=TARGET_CHUNK_MIN_SEC
    )

    assert boundaries[0] == (0.0, max_chunk_sec)
    assert boundaries[-1][1] == total
    for start, end in boundaries:
        assert end - start <= max_chunk_sec + 1e-9


def test_compute_pause_boundaries_every_chunk_at_least_min_chunk_sec():
    boundaries = compute_pause_boundaries(
        60.0,
        silences=[(5.0, 5.1), (5.5, 5.6), (40.0, 40.2)],
        max_chunk_sec=55.0,
    )
    for start, end in boundaries:
        assert end - start >= MIN_CHUNK_SEC - 1e-9


def test_parse_silencedetect_output_basic():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 12.34\n"
        "[silencedetect @ 0x1] silence_end: 13.01 | silence_duration: 0.67\n"
        "[silencedetect @ 0x1] silence_start: 45.0\n"
        "[silencedetect @ 0x1] silence_end: 45.9 | silence_duration: 0.9\n"
    )
    assert parse_silencedetect_output(stderr) == [(12.34, 13.01), (45.0, 45.9)]


def test_parse_silencedetect_output_unclosed_interval_dropped():
    assert parse_silencedetect_output("[silencedetect @ 0x1] silence_start: 12.34\n") == []


def test_parse_silencedetect_output_no_matches():
    assert parse_silencedetect_output("nothing relevant here\n") == []


# --------------------------------------------------------------------------
# Offset shifting / merging — the critical regression coverage.
# --------------------------------------------------------------------------


def test_shift_transcript_adds_offset_to_segments():
    transcript = Transcript(language="mn", full_text="hello", segments=[_segment(0.0, 1.5, "hello")])
    shifted = shift_transcript(transcript, offset_sec=10.0)
    assert shifted.segments[0].start == 10.0
    assert shifted.segments[0].end == 11.5


def test_shift_transcript_zero_offset_is_noop():
    transcript = Transcript(language="mn", full_text="x", segments=[_segment(1.0, 2.0, "x")])
    shifted = shift_transcript(transcript, 0.0)
    assert shifted.segments[0].start == 1.0
    assert shifted.segments[0].end == 2.0


def test_merge_transcripts_offsets_each_chunk_independently():
    """Three 10s chunks, each with a segment at [0, 2] relative to its own
    start. After merging, each segment must land at [chunk_start, chunk_start+2],
    not all piled up at [0, 2] and not double-offset.
    """
    chunk0 = Transcript(language="mn", full_text="a", segments=[_segment(0.0, 2.0, "a")])
    chunk1 = Transcript(language="mn", full_text="b", segments=[_segment(0.0, 2.0, "b")])
    chunk2 = Transcript(language="mn", full_text="c", segments=[_segment(0.0, 2.0, "c")])

    merged = merge_transcripts([(0.0, chunk0), (10.0, chunk1), (20.0, chunk2)])

    starts = [s.start for s in merged.segments]
    ends = [s.end for s in merged.segments]
    assert starts == [0.0, 10.0, 20.0]
    assert ends == [2.0, 12.0, 22.0]
    assert merged.full_text == "a b c"


def test_merge_transcripts_empty_list():
    merged = merge_transcripts([])
    assert merged.segments == []
    assert merged.full_text == ""


def test_merge_transcripts_propagates_timings_estimated():
    estimated = Transcript(language="mn", full_text="x", segments=[], timings_estimated=True)
    exact = Transcript(language="mn", full_text="y", segments=[], timings_estimated=False)
    merged = merge_transcripts([(0.0, exact), (5.0, estimated)])
    assert merged.timings_estimated is True


# --------------------------------------------------------------------------
# Sentence-boundary proportional segment synthesis — for the rare case a
# single pause-bounded chunk still contains more than one sentence.
# --------------------------------------------------------------------------


def test_synthesize_segments_proportional_to_char_count():
    text = "Hi. This is a much longer sentence indeed."
    segments = synthesize_segments_from_text(text, duration_sec=10.0)
    assert len(segments) == 2
    assert segments[0].text == "Hi."
    assert segments[1].text == "This is a much longer sentence indeed."
    assert segments[0].end - segments[0].start < segments[1].end - segments[1].start
    assert segments[0].start == 0.0
    assert abs(segments[-1].end - 10.0) < 1e-6


def test_synthesize_segments_single_sentence_no_terminal_punctuation():
    segments = synthesize_segments_from_text("just one clause", duration_sec=5.0)
    assert len(segments) == 1
    assert segments[0].start == 0.0
    assert segments[0].end == 5.0


def test_synthesize_segments_empty_text():
    assert synthesize_segments_from_text("", duration_sec=5.0) == []


def test_synthesize_segments_zero_duration():
    assert synthesize_segments_from_text("hello.", duration_sec=0.0) == []


def test_split_into_sentences_basic():
    assert split_into_sentences("Hi. This is a test.") == ["Hi.", "This is a test."]


def test_split_into_sentences_no_terminal_punctuation_returns_whole_text():
    assert split_into_sentences("just one clause") == ["just one clause"]


def test_split_into_sentences_empty_text():
    assert split_into_sentences("") == []


def test_text_to_transcript_marks_timings_estimated():
    transcript = text_to_transcript("Сайн байна уу.", duration_sec=3.0)
    assert transcript.timings_estimated is True
    assert transcript.full_text == "Сайн байна уу."
    assert transcript.language == "mn"


# --------------------------------------------------------------------------
# WAV duration reading.
# --------------------------------------------------------------------------


def test_wav_duration_sec(tmp_path):
    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 3)  # 3 seconds of silence

    assert wav_duration_sec(path) == pytest.approx(3.0)


# --------------------------------------------------------------------------
# transcribe() orchestration against a fake httpx transport. Chunk
# extraction/silence-detection (real ffmpeg calls) are monkeypatched here so
# this suite stays hermetic; see the ad hoc script for a real-ffmpeg check.
# --------------------------------------------------------------------------

