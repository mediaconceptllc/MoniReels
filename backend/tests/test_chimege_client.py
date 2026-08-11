"""Chimege adapter unit tests — no network, no real ffmpeg/API calls.

The offset-merge test is the one the spec explicitly calls out as covering
the most common bug in chunked STT pipelines: forgetting to add each chunk's
start offset to its returned timestamps before merging.
"""
from __future__ import annotations

import httpx
import pytest

from app.models import Segment, Transcript, Word
from app.stt.chimege_client import (
    ChimegeClient,
    ChimegeConfig,
    ChimegeError,
    compute_chunk_boundaries,
    merge_transcripts,
    parse_silencedetect_output,
    shift_transcript,
    synthesize_segments_from_text,
    wav_duration_sec,
)


def _segment(
    start: float, end: float, text: str, words: list[tuple[float, float, str]] | None = None
) -> Segment:
    return Segment(
        id="seg",
        start=start,
        end=end,
        text=text,
        words=[Word(start=s, end=e, text=t) for s, e, t in (words or [])],
    )


# --------------------------------------------------------------------------
# Offset shifting / merging — the critical regression coverage.
# --------------------------------------------------------------------------


def test_shift_transcript_adds_offset_to_segments_and_words():
    transcript = Transcript(
        language="mn",
        full_text="hello world",
        segments=[_segment(0.0, 1.5, "hello", [(0.0, 0.5, "hel"), (0.5, 1.5, "lo")])],
    )
    shifted = shift_transcript(transcript, offset_sec=10.0)

    seg = shifted.segments[0]
    assert seg.start == 10.0
    assert seg.end == 11.5
    assert seg.words[0].start == 10.0
    assert seg.words[0].end == 10.5
    assert seg.words[1].start == 10.5
    assert seg.words[1].end == 11.5


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


def test_merge_transcripts_preserves_word_offsets_too():
    chunk = Transcript(
        language="mn", full_text="hi", segments=[_segment(0.0, 1.0, "hi", [(0.0, 1.0, "hi")])]
    )
    merged = merge_transcripts([(50.0, chunk)])
    assert merged.segments[0].words[0].start == 50.0
    assert merged.segments[0].words[0].end == 51.0


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
# Chunk boundary computation.
# --------------------------------------------------------------------------


def test_compute_chunk_boundaries_short_audio_single_chunk():
    boundaries = compute_chunk_boundaries(total_duration=30.0, max_chunk_sec=55.0)
    assert boundaries == [(0.0, 30.0)]


def test_compute_chunk_boundaries_cuts_in_silence_near_target():
    # 100s audio, 40s max chunks. Silence at [39, 40.4] should be used as the
    # first cut instead of a hard cut at 40.0.
    boundaries = compute_chunk_boundaries(
        total_duration=100.0, max_chunk_sec=40.0, silences=[(39.0, 40.4)]
    )
    assert boundaries[0] == (0.0, 39.7)  # midpoint of the silence interval, no overlap needed
    assert boundaries[1][0] == 39.7  # second chunk picks up exactly where the first left off
    # chunks must cover the whole file with no gaps (overlap from later fallback cuts is fine)
    assert boundaries[-1][1] == 100.0
    for (_s1, e1), (s2, _e2) in zip(boundaries, boundaries[1:], strict=False):
        assert s2 <= e1


def test_compute_chunk_boundaries_falls_back_to_overlap_without_silence():
    boundaries = compute_chunk_boundaries(total_duration=90.0, max_chunk_sec=40.0, silences=[])
    # first chunk hard-cuts at 40.0, second chunk starts 0.5s earlier (overlap)
    assert boundaries[0] == (0.0, 40.0)
    assert boundaries[1][0] == 39.5
    assert boundaries[-1][1] == 90.0


def test_compute_chunk_boundaries_covers_full_duration_no_gaps():
    boundaries = compute_chunk_boundaries(total_duration=257.3, max_chunk_sec=55.0, silences=[])
    assert boundaries[0][0] == 0.0
    assert boundaries[-1][1] == 257.3
    for (_s1, e1), (s2, _e2) in zip(boundaries, boundaries[1:], strict=False):
        assert s2 <= e1  # overlap allowed, gaps are not


def test_compute_chunk_boundaries_zero_duration():
    assert compute_chunk_boundaries(0.0, 55.0) == []


# --------------------------------------------------------------------------
# Sentence-boundary proportional segment synthesis (no-timings fallback).
# --------------------------------------------------------------------------


def test_synthesize_segments_proportional_to_char_count():
    text = "Hi. This is a much longer sentence indeed."
    segments = synthesize_segments_from_text(text, duration_sec=10.0)
    assert len(segments) == 2
    assert segments[0].text == "Hi."
    assert segments[1].text == "This is a much longer sentence indeed."
    # shorter sentence gets proportionally less time
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


# --------------------------------------------------------------------------
# silencedetect stderr parsing.
# --------------------------------------------------------------------------


def test_parse_silencedetect_output_basic():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 12.34\n"
        "[silencedetect @ 0x1] silence_end: 13.01 | silence_duration: 0.67\n"
        "[silencedetect @ 0x1] silence_start: 45.0\n"
        "[silencedetect @ 0x1] silence_end: 45.9 | silence_duration: 0.9\n"
    )
    intervals = parse_silencedetect_output(stderr)
    assert intervals == [(12.34, 13.01), (45.0, 45.9)]


def test_parse_silencedetect_output_unclosed_interval_dropped():
    stderr = "[silencedetect @ 0x1] silence_start: 12.34\n"
    assert parse_silencedetect_output(stderr) == []


def test_parse_silencedetect_output_no_matches():
    assert parse_silencedetect_output("nothing relevant here\n") == []


# --------------------------------------------------------------------------
# WAV duration reading.
# --------------------------------------------------------------------------


def test_wav_duration_sec(tmp_path):
    import wave

    path = tmp_path / "test.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 3)  # 3 seconds of silence

    assert wav_duration_sec(path) == pytest.approx(3.0)


# --------------------------------------------------------------------------
# Retry policy against a fake httpx transport (no real network).
# --------------------------------------------------------------------------


def _fake_client(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


@pytest.mark.asyncio
async def test_transcribe_short_audio_single_request(tmp_path):
    import wave

    wav_path = tmp_path / "short.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000 * 2)  # 2 seconds

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, json={"language": "mn", "text": "sain baina uu"})

    client = ChimegeClient(
        ChimegeConfig(url="https://fake.chimege.mn/asr", token="tok", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
    transcript = await client.transcribe(wav_path)
    await client.aclose()

    assert call_count["n"] == 1
    assert transcript.timings_estimated is True
    assert transcript.full_text == "sain baina uu"


@pytest.mark.asyncio
async def test_transcribe_retries_on_503_then_succeeds(tmp_path):
    import wave

    wav_path = tmp_path / "short.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"language": "mn", "text": "ok"})

    client = ChimegeClient(
        ChimegeConfig(url="https://fake.chimege.mn/asr", token="tok", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
    # Speed the test up: patch the sleep used between retries.
    import app.stt.chimege_client as mod

    orig_sleep = mod.asyncio.sleep
    mod.asyncio.sleep = lambda _s: orig_sleep(0)
    try:
        transcript = await client.transcribe(wav_path)
    finally:
        mod.asyncio.sleep = orig_sleep
        await client.aclose()

    assert attempts["n"] == 3
    assert transcript.full_text == "ok"


@pytest.mark.asyncio
async def test_transcribe_does_not_retry_on_400(tmp_path):
    import wave

    wav_path = tmp_path / "short.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = ChimegeClient(
        ChimegeConfig(url="https://fake.chimege.mn/asr", token="tok", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
    with pytest.raises(ChimegeError):
        await client.transcribe(wav_path)
    await client.aclose()

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_transcribe_missing_config_raises():
    client = ChimegeClient(ChimegeConfig(url="", token="", max_audio_sec=55))
    with pytest.raises(ChimegeError):
        await client.transcribe(__import__("pathlib").Path("unused.wav"))
    await client.aclose()
