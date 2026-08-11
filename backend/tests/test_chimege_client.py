"""Chimege adapter unit tests — no network, no real ffmpeg/API calls.

Confirmed against the real Chimege OpenAPI spec (v1.2). The offset-merge test
covers the most common bug in chunked STT pipelines: forgetting to add each
chunk's start offset to its returned timestamps before merging — here the
chunks come from Chimege's own /stt-long-transcript response array rather
than client-side splitting, but the bug (and the fix) is identical.
"""
from __future__ import annotations

import wave

import httpx
import pytest

from app.models import Segment, Transcript
from app.stt.chimege_client import (
    ChimegeClient,
    ChimegeConfig,
    ChimegeError,
    merge_transcripts,
    poll_timeout_sec,
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
# Sentence-boundary proportional segment synthesis — Chimege never returns
# word/segment timings on either endpoint, so this is the only timing source.
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


def test_poll_timeout_scales_with_duration_but_has_a_floor():
    assert poll_timeout_sec(10.0) == 180.0  # floor
    assert poll_timeout_sec(3600.0) == 1800.0  # 1h audio -> 30min budget (spec: ~4min actual)


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
# /transcribe (short, synchronous) path against a fake httpx transport.
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
async def test_transcribe_long_pushes_then_polls_and_merges_with_correct_offsets(tmp_path):
    """The core regression test: /stt-long-transcript returns a time-ordered
    array of chunks with durations but no start/end — merging must place the
    second chunk's segment at the first chunk's duration, not at 0.
    """
    wav_path = tmp_path / "long.wav"
    _write_wav(wav_path, duration_sec=10.0)

    poll_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stt-long"):
            assert request.headers.get("token") == "tok-123"
            assert request.headers.get("content-type") == "audio/wav"
            return httpx.Response(200, json={"uuid": "job-abc", "duration": 10.0})
        if request.url.path.endswith("/stt-long-transcript"):
            assert request.headers.get("uuid") == "job-abc"
            poll_calls["n"] += 1
            if poll_calls["n"] < 2:
                return httpx.Response(200, json=[{"done": False, "transcription": "", "duration": 0}])
            return httpx.Response(
                200,
                json=[
                    {"done": True, "transcription": "Эхний хэсэг.", "duration": 4.0},
                    {"done": True, "transcription": "Хоёр дахь хэсэг.", "duration": 6.0},
                ],
            )
        raise AssertionError(f"unexpected request to {request.url}")

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok-123", max_audio_sec=2),
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

    assert poll_calls["n"] == 2
    assert len(transcript.segments) == 2
    assert transcript.segments[0].start == 0.0
    assert transcript.segments[0].text == "Эхний хэсэг."
    assert transcript.segments[1].start == 4.0  # offset by chunk 1's duration, not 0
    assert transcript.segments[1].text == "Хоёр дахь хэсэг."


@pytest.mark.asyncio
async def test_transcribe_long_times_out_if_never_done(tmp_path):
    wav_path = tmp_path / "long.wav"
    _write_wav(wav_path, duration_sec=10.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/stt-long"):
            return httpx.Response(200, json={"uuid": "job-abc", "duration": 10.0})
        return httpx.Response(200, json=[{"done": False, "transcription": "", "duration": 0}])

    client = ChimegeClient(
        ChimegeConfig(url="https://api.chimege.com/v1.2", token="tok", max_audio_sec=2),
        http_client=_fake_client(handler),
    )
    import app.stt.chimege_client as mod

    orig_sleep = mod.asyncio.sleep
    mod.asyncio.sleep = lambda _s: orig_sleep(0)
    orig_poll_timeout = mod.poll_timeout_sec
    mod.poll_timeout_sec = lambda _d: 0.0  # force immediate timeout
    try:
        with pytest.raises(ChimegeError, match="did not finish"):
            await client.transcribe(wav_path)
    finally:
        mod.asyncio.sleep = orig_sleep
        mod.poll_timeout_sec = orig_poll_timeout
        await client.aclose()


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
