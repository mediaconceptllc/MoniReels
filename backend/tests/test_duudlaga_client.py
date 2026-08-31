"""duudlaga.dev STT client.

Written against the published contract. The error codes get the most
attention here because three of them need behaviour a status-code check
alone gets wrong, and each one costs something real when it is wrong: a
killed hour-long job, three duplicate failures hiding the actual reason, or
a worker slot spent retrying a cap that only the clock will clear.
"""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import httpx
import pytest

from app.stt.duudlaga_client import TRANSCRIBE_PATH, DuudlagaClient, DuudlagaConfig, DuudlagaError

# Captured before the autouse fixture below replaces asyncio.sleep with a
# no-op. Retry backoff must not cost the suite real seconds, but the
# concurrency tests need time to actually pass for requests to overlap.
_REAL_SLEEP = asyncio.sleep


def _client(handler, **cfg) -> DuudlagaClient:
    # Compression off unless a test asks for it: encoding placeholder bytes
    # would shell out to a real ffmpeg, and these tests are hermetic.
    base = {
        "base_url": "https://api.duudlaga.dev/v1",
        "api_key": "dk_live_test",
        "max_audio_sec": 60.0,
        "upload_bitrate": "",
    }
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


# ---------------------------------------------------------------------------
# A refused chunk is halved, not repeated
# ---------------------------------------------------------------------------


def _split_fixture(tmp_path, monkeypatch, seconds: float):
    """A whole-file transcribe with ffmpeg stubbed out and no pauses, so the
    file arrives as one chunk of the given length."""
    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=seconds)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")

    async def _silences(ffmpeg, path):
        return []

    async def _extract(ffmpeg, src, out, start, end):
        out.write_bytes(b"chunk")

    monkeypatch.setattr(mod, "detect_silences", _silences)
    monkeypatch.setattr(mod, "extract_chunk", _extract)
    return audio


@pytest.mark.asyncio
async def test_a_chunk_the_server_refuses_is_halved_rather_than_repeated(
    tmp_path, monkeypatch, _fast_backoff
):
    """Production sent a 59s chunk, got a bare 500, sent the identical bytes
    twice more, and lost a job that had already transcribed and paid for six
    chunks. The API documents no size limit, so the limit has to be found by
    halving rather than read."""
    audio = _split_fixture(tmp_path, monkeypatch, seconds=40.0)
    sizes: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # The first three requests are the full chunk and its two retries;
        # everything after is a half.
        sizes.append(len(sizes))
        if len(sizes) <= 3:
            return _error(500, "internal_error")
        return httpx.Response(200, json={"text": "тал."})

    result = await _client(handler, max_audio_sec=60).transcribe(audio)

    assert result.full_text == "тал. тал."
    # Two halves, at the start of the file and at its midpoint.
    assert [round(s.start, 1) for s in result.segments] == [0.0, 20.0]


@pytest.mark.asyncio
async def test_halving_gives_up_instead_of_recursing_without_end(
    tmp_path, monkeypatch, _fast_backoff
):
    """Each level doubles the request count, so a span the server will never
    accept has to stop costing money at some point."""
    from app.stt.duudlaga_client import DuudlagaError

    audio = _split_fixture(tmp_path, monkeypatch, seconds=40.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error(500, "internal_error")

    with pytest.raises(DuudlagaError):
        await _client(handler, max_audio_sec=60).transcribe(audio)

    # Bounded, not unbounded: without a floor this never returns at all.
    assert calls < 200


@pytest.mark.asyncio
async def test_a_rate_limit_is_never_answered_by_sending_more_requests(
    tmp_path, monkeypatch, _fast_backoff
):
    """Splitting doubles the request count — exactly what a rate, concurrency
    or spend limit is asking you to stop doing."""
    from app.stt.duudlaga_client import DuudlagaError

    audio = _split_fixture(tmp_path, monkeypatch, seconds=40.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error(429, "rate_limit_exceeded")

    with pytest.raises(DuudlagaError):
        await _client(handler, max_audio_sec=60).transcribe(audio)

    # The three ordinary retries and not one request more.
    assert calls == 3


@pytest.mark.asyncio
async def test_a_spent_balance_is_not_split_either(tmp_path, monkeypatch, _fast_backoff):
    from app.stt.duudlaga_client import DuudlagaError

    audio = _split_fixture(tmp_path, monkeypatch, seconds=40.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error(402, "insufficient_credits")

    with pytest.raises(DuudlagaError):
        await _client(handler, max_audio_sec=60).transcribe(audio)

    # Fatal: not retried, not split.
    assert calls == 1


@pytest.mark.asyncio
async def test_a_cloudflare_origin_error_is_treated_like_the_500_it_stands_for(
    tmp_path, monkeypatch, _fast_backoff
):
    """duudlaga.dev sits behind Cloudflare, which answers for a failing origin
    with a bodiless 520-527. Production sent a 50.5s chunk, got 500, then 520
    — and 520 was in neither the retry list nor the split list, so the job
    died one request after a condition the 500 path already handled."""
    audio = _split_fixture(tmp_path, monkeypatch, seconds=40.0)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # The whole chunk and its retries are refused by the edge; the halves
        # reach the origin.
        if calls <= 3:
            return httpx.Response(520)
        return httpx.Response(200, json={"text": "тал."})

    result = await _client(handler, max_audio_sec=60).transcribe(audio)

    assert result.full_text == "тал. тал."
    # Retried like a 500 rather than raised on the first response.
    assert calls == 5


@pytest.mark.parametrize("status", [500, 502, 503, 520, 521, 524])
def test_every_server_side_status_is_retried_and_splittable(status):
    """The lesson of the 520: the condition was identical to the 500 and only
    the digits were new, so the rule is the class, not a list of numbers."""
    from app.stt.duudlaga_client import DuudlagaError

    error = DuudlagaError("boom", status=status)
    assert error.retryable is True
    assert error.blames_the_payload is True


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_a_client_side_status_is_neither_retried_nor_split(status):
    from app.stt.duudlaga_client import DuudlagaError

    error = DuudlagaError("boom", status=status)
    assert error.retryable is False
    assert error.blames_the_payload is False


# ---------------------------------------------------------------------------
# Compressed upload
# ---------------------------------------------------------------------------


def _fake_encoder(monkeypatch, sizes: list[int] | None = None):
    """Stand in for ffmpeg: writes a small file where the Opus would go."""
    import app.stt.duudlaga_client as mod

    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: Path("ffmpeg"))

    async def _encode(ffmpeg, src, out, bitrate):
        out.write_bytes(b"OggS" + b"\0" * 16)
        if sizes is not None:
            sizes.append(len(src.read_bytes()))

    monkeypatch.setattr(mod, "encode_for_upload", _encode)


@pytest.mark.asyncio
async def test_a_chunk_is_uploaded_compressed(tmp_path, monkeypatch, chunk):
    """16 kHz mono PCM is 32 kB per second on the wire, and every chunk the
    API refused was one of the large ones."""
    _fake_encoder(monkeypatch)
    sent: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        # The multipart body carries the filename and the content type.
        sent.append(
            (
                "audio/ogg" if b"audio/ogg" in body else "audio/wav",
                "opus" if b".opus" in body else "wav",
            )
        )
        return httpx.Response(200, json={"text": "болсон"})

    text = await _client(handler, upload_bitrate="32k").transcribe_chunk_text(chunk)

    assert text == "болсон"
    assert sent == [("audio/ogg", "opus")]


@pytest.mark.asyncio
async def test_a_rejected_format_falls_back_to_wav_and_stops_guessing(
    tmp_path, monkeypatch, chunk, _fast_backoff
):
    """The documented request shows a .wav and the accepted formats are
    written down nowhere, so compression is a guess. Taking it back has to
    rescue the chunk, and it has to happen once — not on all 55."""
    _fake_encoder(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        compressed = b"audio/ogg" in request.content
        seen.append("opus" if compressed else "wav")
        if compressed:
            return _error(400, "invalid_request")
        return httpx.Response(200, json={"text": "болсон"})

    client = _client(handler, upload_bitrate="32k")

    assert await client.transcribe_chunk_text(chunk) == "болсон"
    assert await client.transcribe_chunk_text(chunk) == "болсон"

    # One rejected attempt, then WAV for good — not one wasted encode and
    # round trip per chunk for the rest of the job.
    assert seen == ["opus", "wav", "wav"]


@pytest.mark.asyncio
async def test_an_encoder_that_is_not_there_is_not_fatal(tmp_path, monkeypatch, chunk):
    """ffmpeg without libopus is a build detail, not a reason to lose the
    transcription."""
    import app.stt.duudlaga_client as mod
    from app.stt.chunking import ChunkingError

    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: Path("ffmpeg"))

    async def _encode(ffmpeg, src, out, bitrate):
        raise ChunkingError("no libopus in this build")

    monkeypatch.setattr(mod, "encode_for_upload", _encode)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append("opus" if b"audio/ogg" in request.content else "wav")
        return httpx.Response(200, json={"text": "болсон"})

    assert await _client(handler, upload_bitrate="32k").transcribe_chunk_text(chunk) == "болсон"
    assert seen == ["wav"]


@pytest.mark.asyncio
async def test_a_422_without_a_code_is_still_no_speech(tmp_path, monkeypatch):
    """Production returned `422 (No speech was detected in the audio.)` with
    no `code` field, so the code-only check raised instead of skipping and
    killed the job at chunk 2 of 68 — over an outcome the pipeline already
    knew how to handle."""
    audio = _split_fixture(tmp_path, monkeypatch, seconds=10.0)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "No speech was detected in the audio."})

    result = await _client(handler).transcribe(audio)

    assert result.full_text == ""
    assert result.segments == []


@pytest.mark.asyncio
async def test_a_silent_verdict_on_compressed_audio_is_checked_against_the_original(
    tmp_path, monkeypatch, chunk
):
    """The first span the API called silent had transcribed fine the run
    before, uncompressed — so "no speech" on a compressed chunk could equally
    be this encode having destroyed it. The original bytes settle which."""
    _fake_encoder(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        compressed = b"audio/ogg" in request.content
        seen.append("opus" if compressed else "wav")
        if compressed:
            return _error(422, "no_speech")
        return httpx.Response(200, json={"text": "энд яриа бий"})

    client = _client(handler, upload_bitrate="32k")
    assert await client.transcribe_chunk_text(chunk) == "энд яриа бий"

    assert seen == ["opus", "wav"]
    # Not a format rejection, so compression stays on for the next chunk.
    assert client._send_compressed is True


@pytest.mark.asyncio
async def test_a_genuinely_silent_chunk_costs_one_extra_request_and_no_more(
    tmp_path, monkeypatch, chunk
):
    _fake_encoder(monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _error(422, "no_speech")

    assert await _client(handler, upload_bitrate="32k").transcribe_chunk_text(chunk) == ""
    assert calls == 2


def test_compression_is_off_unless_a_bitrate_is_set():
    """Measured against production, compression cost accuracy and bought
    nothing that was still needed: two chunks came back `no_speech` that the
    same audio as WAV transcribed, and a 30s one hung for 180s twice. The
    machinery stays for a future re-test; the default does not."""
    from app.config import Settings

    assert Settings(_env_file=None).duudlaga_upload_bitrate == ""


@pytest.mark.asyncio
async def test_with_compression_off_the_wav_goes_straight_out(tmp_path, monkeypatch, chunk):
    """No encode, no wasted round trip — the WAV is the request."""
    import app.stt.duudlaga_client as mod

    def _never(*args, **kwargs):
        raise AssertionError("the encoder must not run with compression off")

    monkeypatch.setattr(mod, "encode_for_upload", _never)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append("opus" if b"audio/ogg" in request.content else "wav")
        return httpx.Response(200, json={"text": "болсон"})

    assert await _client(handler).transcribe_chunk_text(chunk) == "болсон"
    assert seen == ["wav"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def _three_chunk_fixture(tmp_path, monkeypatch):
    """A file with two pauses, so it splits into exactly three chunks."""
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
    return audio


@pytest.mark.asyncio
async def test_the_transcript_keeps_chunk_order_when_a_later_chunk_finishes_first(
    tmp_path, monkeypatch
):
    """merge_transcripts joins the text in list order and does not sort, so
    taking results as they complete would shuffle the interview. The first
    chunk here is made the slowest precisely so completion order and chunk
    order disagree."""
    audio = _three_chunk_fixture(tmp_path, monkeypatch)
    # Keyed on the chunk's own filename, not on arrival order: the word has to
    # follow the audio it belongs to, or the test scrambles the transcript by
    # itself and proves nothing about the code.
    words = {b"chunk_000": "нэг", b"chunk_001": "хоёр", b"chunk_002": "гурав"}

    async def handler(request: httpx.Request) -> httpx.Response:
        name = next(k for k in words if k in request.content)
        # The first chunk answers last.
        await _REAL_SLEEP(0.05 if name == b"chunk_000" else 0.0)
        return httpx.Response(200, json={"text": words[name]})

    result = await _client(handler, concurrency=3).transcribe(audio)

    assert result.full_text == "нэг хоёр гурав"
    assert [round(s.start, 2) for s in result.segments] == [0.0, 10.25, 20.25]


@pytest.mark.asyncio
async def test_chunks_really_do_overlap(tmp_path, monkeypatch):
    """The whole point: three chunks in flight together, not one after
    another. The provider is ~0.45x realtime and that wait is the wall clock."""
    audio = _three_chunk_fixture(tmp_path, monkeypatch)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await _REAL_SLEEP(0.02)
        in_flight -= 1
        return httpx.Response(200, json={"text": "үг"})

    await _client(handler, concurrency=3).transcribe(audio)

    assert peak == 3, f"chunks were not concurrent (peak in flight: {peak})"


@pytest.mark.asyncio
async def test_concurrency_is_a_ceiling_not_a_target(tmp_path, monkeypatch):
    """A key whose limit is lower than this setting is served by lowering it
    to 1, which has to actually serialise."""
    audio = _three_chunk_fixture(tmp_path, monkeypatch)
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await _REAL_SLEEP(0.02)
        in_flight -= 1
        return httpx.Response(200, json={"text": "үг"})

    await _client(handler, concurrency=1).transcribe(audio)

    assert peak == 1


@pytest.mark.asyncio
async def test_a_fatal_chunk_still_fails_the_job(tmp_path, monkeypatch, _fast_backoff):
    """Concurrency must not turn a spent balance into a partial transcript
    that looks complete."""
    from app.stt.duudlaga_client import DuudlagaError

    audio = _three_chunk_fixture(tmp_path, monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        return _error(402, "insufficient_credits")

    with pytest.raises(DuudlagaError):
        await _client(handler, concurrency=3).transcribe(audio)


# --------------------------------------------------------------------------
# A verdict about the ACCOUNT ends the run. Production hit 402 with an empty
# balance and sent all 62 chunks anyway — free, because a rejected request
# transcribes nothing, but the same shape means credits running out midway
# fires every remaining chunk at a wall.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_balance_stops_the_remaining_chunks(tmp_path, monkeypatch):
    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=120.0)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")

    async def _silences(ffmpeg, path):
        return [(float(t), t + 0.5) for t in range(10, 120, 10)]

    async def _extract(ffmpeg, src, out, start, end):
        out.write_bytes(b"chunk")

    monkeypatch.setattr(mod, "detect_silences", _silences)
    monkeypatch.setattr(mod, "extract_chunk", _extract)

    sent = 0

    def handler(_request):
        nonlocal sent
        sent += 1
        if sent <= 2:
            return httpx.Response(200, json={"text": "яриа."})
        return _error(402, "insufficient_credits", "Not enough credits")

    with pytest.raises(mod.DuudlagaError) as raised:
        # concurrency=1 so the order is deterministic: two succeed, the third
        # is refused, and everything after it must never be sent.
        await _client(handler, concurrency=1).transcribe(audio)

    assert sent == 3, f"{sent - 3} chunk(s) were sent after the balance ran out"
    assert raised.value.status == 402
    assert "2/" in str(raised.value)  # says how many were already paid for


@pytest.mark.asyncio
async def test_a_402_without_a_code_still_ends_the_run(tmp_path, monkeypatch):
    # The status has to decide too: production's 402 arrived with the credits
    # message, and a code-only check would have let the whole batch fly.
    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=60.0)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")
    monkeypatch.setattr(mod, "detect_silences", lambda *_: _silences_every_ten())
    monkeypatch.setattr(mod, "extract_chunk", _write_chunk)

    sent = 0

    def handler(_request):
        nonlocal sent
        sent += 1
        return httpx.Response(402, json={"message": "Top up your balance."})

    with pytest.raises(mod.DuudlagaError):
        await _client(handler, concurrency=1).transcribe(audio)

    assert sent == 1


async def _silences_every_ten():
    return [(float(t), t + 0.5) for t in range(10, 60, 10)]


async def _write_chunk(ffmpeg, src, out, start, end):
    out.write_bytes(b"chunk")


def test_a_retryable_failure_is_not_treated_as_fatal():
    from app.stt.duudlaga_client import DuudlagaError

    assert not DuudlagaError("busy", status=429, code="rate_limit_exceeded").ends_the_run
    assert not DuudlagaError("boom", status=500, code="internal_error").ends_the_run
    assert DuudlagaError("broke", status=402, code="insufficient_credits").ends_the_run
    assert DuudlagaError("cap", status=403, code="daily_spend_cap_exceeded").ends_the_run


@pytest.mark.asyncio
async def test_the_abort_does_not_pile_up_one_exceptions_traceback(tmp_path, monkeypatch):
    """Re-raising ONE exception object from every waiting chunk appends to
    that object's traceback each time, and the log fills with

        raise fatal
          [Previous line repeated 55 more times]

    which is the noise the early abort was added to remove, arriving by a
    different route. Seen in production on the run after that change shipped.
    """
    import traceback as tb_module

    import app.stt.duudlaga_client as mod

    audio = tmp_path / "audio.wav"
    _wav(audio, seconds=600.0)
    monkeypatch.setattr(mod, "_require_ffmpeg", lambda: tmp_path / "ffmpeg")
    monkeypatch.setattr(mod, "detect_silences", lambda *_: _silences_every_ten_long())
    monkeypatch.setattr(mod, "extract_chunk", _write_chunk)

    def handler(_request):
        return _error(402, "insufficient_credits", "Not enough credits")

    with pytest.raises(mod.DuudlagaError) as raised:
        await _client(handler, concurrency=1).transcribe(audio)

    rendered = "".join(tb_module.format_exception(raised.value))
    assert "Previous line repeated" not in rendered, rendered[-1500:]


async def _silences_every_ten_long():
    return [(float(t), t + 0.5) for t in range(10, 600, 10)]
