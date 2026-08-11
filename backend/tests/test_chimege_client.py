"""Chimege adapter unit tests — no network, no real ffmpeg/API calls (a
separate live-ffmpeg integration check is run ad hoc; see project notes).

Confirmed against the real Chimege OpenAPI spec (v1.2). The offset-merge test
covers the most common bug in chunked STT pipelines: forgetting to add each
chunk's start offset to its returned timestamps before merging.
"""
from __future__ import annotations

import wave

import httpx
import pytest

from app.models import Segment, Transcript
from app.stt.chimege_client import (
    MIN_CHUNK_SEC,
    ChimegeClient,
    ChimegeConfig,
    ChimegeError,
    compute_pause_boundaries,
    merge_transcripts,
    parse_silencedetect_output,
    shift_transcript,
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


def _fake_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_transcribe_short_uses_transcribe_endpoint_and_token_header(tmp_path):
    wav_path = tmp_path / "short.wav"
    _write_wav(wav_path, duration_sec=2.0)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("token")
        seen["punctuate"] = request.headers.get("punctuate")
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, text="sain baina uu")

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok-123", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
    transcript = await client.transcribe(wav_path)
    await client.aclose()

    assert seen["url"] == "https://api.chimege.com/v1.2/transcribe"
    assert seen["token"] == "tok-123"  # raw token, not "Bearer tok-123"
    assert seen["punctuate"] == "true"
    assert seen["content_type"] == "application/octet-stream"
    assert transcript.timings_estimated is True
    assert transcript.full_text == "sain baina uu"


@pytest.mark.asyncio
async def test_transcribe_long_audio_splits_into_pause_chunks_with_correct_offsets(tmp_path, monkeypatch):
    """The core regression test: each pause-bounded chunk must be transcribed
    and merged at its own real (not estimated) [start, end] — the second
    chunk's text must land at the first chunk's exact end, not at 0.
    """
    wav_path = tmp_path / "long.wav"
    _write_wav(wav_path, duration_sec=10.0)

    # Fake a single detected pause at the midpoint -> two chunks: [0,5), [5,10).
    async def fake_detect_silences(self, path):
        return [(4.9, 5.1)]

    extracted = []

    async def fake_extract_chunk(self, wav_path, out_path, start, end):
        extracted.append((start, end))
        out_path.write_bytes(b"fake-chunk-audio")

    monkeypatch.setattr(ChimegeClient, "_detect_silences", fake_detect_silences)
    monkeypatch.setattr(ChimegeClient, "_extract_chunk", fake_extract_chunk)

    call_texts = iter(["Эхний өгүүлбэр.", "Хоёр дахь өгүүлбэр."])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(call_texts))

    client = ChimegeClient(
        # max_audio_sec is both "trigger chunking above this" and the
        # per-chunk max_chunk_sec — needs to be > 5.0 or the forced-cut
        # fallback would add extra cuts before reaching the fake pause.
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok", max_audio_sec=6),
        http_client=_fake_client(handler),
    )
    transcript = await client.transcribe(wav_path)
    await client.aclose()

    assert extracted == [(0.0, 5.0), (5.0, 10.0)]
    assert len(transcript.segments) == 2
    assert transcript.segments[0].start == 0.0
    assert transcript.segments[0].text == "Эхний өгүүлбэр."
    assert transcript.segments[1].start == 5.0  # offset by chunk 1's real end, not 0
    assert transcript.segments[1].text == "Хоёр дахь өгүүлбэр."


@pytest.mark.asyncio
async def test_transcribe_pause_chunks_cleans_up_temp_files(tmp_path, monkeypatch):
    wav_path = tmp_path / "long.wav"
    _write_wav(wav_path, duration_sec=10.0)

    async def fake_detect_silences(self, path):
        return []  # no pauses -> forced cuts, still exercises the chunk workdir

    async def fake_extract_chunk(self, wav_path, out_path, start, end):
        out_path.write_bytes(b"fake")

    monkeypatch.setattr(ChimegeClient, "_detect_silences", fake_detect_silences)
    monkeypatch.setattr(ChimegeClient, "_extract_chunk", fake_extract_chunk)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok", max_audio_sec=4),
        http_client=_fake_client(handler),
    )
    await client.transcribe(wav_path)
    await client.aclose()

    workdir = wav_path.parent / f"{wav_path.stem}_chunks"
    assert not workdir.exists()


# --------------------------------------------------------------------------
# Retry policy and error mapping.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcribe_retries_on_503_then_succeeds(tmp_path):
    wav_path = tmp_path / "short.wav"
    _write_wav(wav_path, duration_sec=1.0)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
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
    wav_path = tmp_path / "short.wav"
    _write_wav(wav_path, duration_sec=1.0)

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, headers={"Error-Code": "2003"}, text="audio too short")

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
    with pytest.raises(ChimegeError, match="too short"):
        await client.transcribe(wav_path)
    await client.aclose()

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_transcribe_403_maps_invalid_token_error_code(tmp_path):
    wav_path = tmp_path / "short.wav"
    _write_wav(wav_path, duration_sec=1.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Error-Code": "1000"}, text="forbidden")

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="bad-tok", max_audio_sec=55),
        http_client=_fake_client(handler),
    )
    with pytest.raises(ChimegeError, match="Invalid API token"):
        await client.transcribe(wav_path)
    await client.aclose()


@pytest.mark.asyncio
async def test_transcribe_missing_config_raises():
    client = ChimegeClient(ChimegeConfig(url="", token="", max_audio_sec=55))
    with pytest.raises(ChimegeError):
        await client.transcribe(__import__("pathlib").Path("unused.wav"))
    await client.aclose()
