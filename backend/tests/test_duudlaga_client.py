"""duudlaga.dev STT client.

Written against the published contract. The error codes get the most
attention here because three of them need behaviour a status-code check
alone gets wrong, and each one costs something real when it is wrong: a
killed hour-long job, three duplicate failures hiding the actual reason, or
a worker slot spent retrying a cap that only the clock will clear.
"""
from __future__ import annotations

import wave

import httpx
import pytest

from app.stt.duudlaga_client import TRANSCRIBE_PATH, DuudlagaClient, DuudlagaConfig, DuudlagaError


def _client(handler, **cfg) -> DuudlagaClient:
    base = {"base_url": "https://api.duudlaga.dev/v1", "api_key": "dk_live_test", "max_audio_sec": 60.0}
    base.update(cfg)
    return DuudlagaClient(
        DuudlagaConfig(**base),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _wav(path, seconds: float = 2.0) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))


def _error(status: int, code: str, message: str = "", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status, json={"code": code, "message": message or code}, headers=headers or {}
    )


@pytest.fixture
def chunk(tmp_path):
    path = tmp_path / "chunk.wav"
    _wav(path)
    return path


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    slept: list[float] = []

    async def _no_sleep(seconds):
        slept.append(seconds)

    import asyncio

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return slept


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posts_the_file_to_the_documented_route(chunk):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["ctype"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={
                "id": "tr_8f2c1a",
                "text": "Сайн байна уу, өнөөдрийн хурлыг эхэлье.",
                "duration_seconds": 12.4,
                "model": "duudlaga-stt-1",
            },
        )

    text = await _client(handler).transcribe_chunk_text(chunk)

    assert text == "Сайн байна уу, өнөөдрийн хурлыг эхэлье."
    assert seen["url"] == f"https://api.duudlaga.dev/v1{TRANSCRIBE_PATH}"
    assert seen["auth"] == "Bearer dk_live_test"
    assert seen["ctype"].startswith("multipart/form-data")
    assert b'name="file"' in seen["body"]


@pytest.mark.asyncio
async def test_model_is_omitted_unless_configured(chunk):
    """The documented request carries only the file. An unexpected field
    risks a 400 invalid_request for no gain."""
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(200, json={"text": "x"})

    await _client(handler).transcribe_chunk_text(chunk)
    assert b'name="model"' not in bodies[0]

    await _client(handler, model="duudlaga-stt-1").transcribe_chunk_text(chunk)
    assert b'name="model"' in bodies[1]


# ---------------------------------------------------------------------------
# no_speech is an outcome, not a failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_speech_returns_empty_text_rather_than_failing(chunk):
    """VAD chooses the windows we send and does mis-fire on a music transient
    or a cough. One empty two-second window must not end an hour-long
    transcription."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _error(422, "no_speech")

    assert await _client(handler).transcribe_chunk_text(chunk) == ""
    # And it is not retried — the answer will not change.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_silent_chunks_are_skipped_but_the_rest_still_transcribe(tmp_path, monkeypatch):
    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=30.0)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")

    async def _silences(ffmpeg, path):
        return [(10.0, 10.5), (20.0, 20.5)]

    async def _extract(ffmpeg, src, out, start, end):
        out.write_bytes(b"chunk")

    monkeypatch.setattr(mod, "detect_silences", _silences)
    monkeypatch.setattr(mod, "extract_chunk", _extract)

    responses = iter(
        [
            httpx.Response(200, json={"text": "эхлэл."}),
            _error(422, "no_speech"),
            httpx.Response(200, json={"text": "төгсгөл."}),
        ]
    )
    result = await _client(lambda r: next(responses)).transcribe(audio)

    assert result.full_text == "эхлэл. төгсгөл."
    # The surviving segments keep their own real offsets — the skipped middle
    # chunk must not shift the last one onto the gap it left.
    assert [round(s.start, 2) for s in result.segments] == [0.0, 20.25]


# ---------------------------------------------------------------------------
# Retry policy — decided by the code, not the status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_is_retried_and_honours_retry_after(chunk, _fast_backoff):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return _error(429, "rate_limit_exceeded", headers={"Retry-After": "7"})
        return httpx.Response(200, json={"text": "болсон"})

    assert await _client(handler).transcribe_chunk_text(chunk) == "болсон"
    assert len(calls) == 2
    # The server knows when its window reopens and we do not, so its own
    # number wins over the local backoff curve.
    assert _fast_backoff == [7.0]


@pytest.mark.asyncio
async def test_retry_after_is_capped(chunk, _fast_backoff):
    """A job holding its worker slot for ten minutes on one chunk is worse
    than failing and being requeued."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _error(429, "rate_limit_exceeded", headers={"Retry-After": "3600"})

    with pytest.raises(DuudlagaError):
        await _client(handler).transcribe_chunk_text(chunk)
    assert max(_fast_backoff) <= 60.0


@pytest.mark.asyncio
async def test_an_http_date_retry_after_falls_back_to_backoff(chunk, _fast_backoff):
    """Parsing the date format wrong and sleeping for hours is worse than
    ignoring the header."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _error(429, "rate_limit_exceeded", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})

    with pytest.raises(DuudlagaError):
        await _client(handler).transcribe_chunk_text(chunk)
    assert _fast_backoff == [1.0, 2.0]


@pytest.mark.asyncio
async def test_concurrency_limit_is_retried(chunk):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return _error(429, "concurrency_limit_exceeded")
        return httpx.Response(200, json={"text": "ok"})

    assert await _client(handler).transcribe_chunk_text(chunk) == "ok"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_daily_spend_cap_is_not_retried(chunk):
    """It is a 429 like the other two, but only the clock clears it — three
    attempts spend a worker slot to reach the same answer."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _error(429, "daily_spend_cap_exceeded")

    with pytest.raises(DuudlagaError, match="өдрийн зарлагын хязгаар"):
        await _client(handler).transcribe_chunk_text(chunk)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_insufficient_credits_is_not_retried_and_says_so(chunk):
    """Retrying a spent balance produces three identical failures and buries
    the one line the operator needs to read."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _error(402, "insufficient_credits", "Balance is 0.00")

    with pytest.raises(DuudlagaError) as excinfo:
        await _client(handler).transcribe_chunk_text(chunk)

    assert len(calls) == 1
    message = str(excinfo.value)
    assert "кредит дууссан" in message
    # The server's own detail is appended, not replaced — it carries what no
    # local table can.
    assert "Balance is 0.00" in message


@pytest.mark.asyncio
async def test_invalid_request_is_not_retried(chunk):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _error(400, "invalid_request", "file is required")

    with pytest.raises(DuudlagaError):
        await _client(handler).transcribe_chunk_text(chunk)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_internal_error_is_retried(chunk):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 2:
            return _error(500, "internal_error")
        return httpx.Response(200, json={"text": "ok"})

    assert await _client(handler).transcribe_chunk_text(chunk) == "ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_an_unknown_code_is_retried_only_when_the_status_is_transient(chunk):
    """A code the API adds later must not be assumed safe to repeat: these
    requests are billed."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _error(409, "some_future_code")

    with pytest.raises(DuudlagaError):
        await _client(handler).transcribe_chunk_text(chunk)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Configuration and diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_key_and_wrong_path_name_the_variable_to_fix(chunk):
    with pytest.raises(DuudlagaError, match="DUUDLAGA_API_KEY"):
        await _client(lambda r: httpx.Response(401, text="")).transcribe_chunk_text(chunk)

    with pytest.raises(DuudlagaError, match="DUUDLAGA_TRANSCRIBE_PATH"):
        await _client(lambda r: httpx.Response(404, text="")).transcribe_chunk_text(chunk)

    with pytest.raises(DuudlagaError, match="DUUDLAGA_MAX_AUDIO_SEC"):
        await _client(lambda r: httpx.Response(413, text="")).transcribe_chunk_text(chunk)


@pytest.mark.asyncio
async def test_missing_config_fails_before_any_request(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the network unconfigured")

    audio = tmp_path / "a.wav"
    _wav(audio)
    with pytest.raises(DuudlagaError, match="DUUDLAGA_BASE_URL"):
        await _client(handler, api_key="").transcribe(audio)
    with pytest.raises(DuudlagaError, match="DUUDLAGA_BASE_URL"):
        await _client(handler, api_key="").transcribe_chunk_text(audio)


@pytest.mark.asyncio
async def test_account_info_reports_the_balance():
    """Lets "out of credits" be answered BEFORE a job downloads a video and
    spends a worker slot to find out."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/me")
        return httpx.Response(200, json={"key": "dk_live_...", "balance": 12.5, "limits": {}})

    info = await _client(handler).account_info()
    assert info["balance"] == 12.5


def test_parse_response_rejects_an_unknown_shape():
    """Silently returning "" would be indistinguishable from no_speech — the
    one outcome that already has its own explicit signal."""
    with pytest.raises(DuudlagaError, match="Unrecognised"):
        DuudlagaClient._parse_response({"unexpected": {"nested": 1}})


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"id": "tr_1", "text": "нэг", "duration_seconds": 1.0}, "нэг"),
        ({"transcription": "хоёр"}, "хоёр"),
        ({"segments": [{"text": "гурав"}, {"text": "дөрөв"}]}, "гурав дөрөв"),
    ],
)
def test_parse_response_reads_the_documented_field_and_tolerates_others(payload, expected):
    assert DuudlagaClient._parse_response(payload) == expected


@pytest.mark.asyncio
async def test_transcribe_cleans_up_its_chunk_files(tmp_path, monkeypatch):
    """Scratch space on a container is small and shared; a job that leaves
    chunks behind starves the next one."""
    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=8.0)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")

    async def _silences(ffmpeg, path):
        return []

    async def _extract(ffmpeg, src, out, start, end):
        out.write_bytes(b"chunk")

    monkeypatch.setattr(mod, "detect_silences", _silences)
    monkeypatch.setattr(mod, "extract_chunk", _extract)

    await _client(lambda r: httpx.Response(200, json={"text": "x"})).transcribe(audio)
    assert not (tmp_path / "audio_chunks").exists()
