"""duudlaga.dev STT client.

The wire format was written against the OpenAI-compatible convention because
the real spec was unreachable (see the module docstring). These tests pin
what IS known to be required regardless of the exact field names — retry
policy, error mapping, and the response parsing being tolerant enough that a
small difference surfaces as a wrong field, not a crash.
"""
from __future__ import annotations

import wave

import httpx
import pytest

from app.stt.duudlaga_client import DuudlagaClient, DuudlagaConfig, DuudlagaError


def _client(handler, **cfg) -> DuudlagaClient:
    base = {"base_url": "https://fake.duudlaga/v1", "api_key": "key", "max_audio_sec": 60.0}
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


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    import asyncio

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


@pytest.mark.asyncio
async def test_sends_bearer_token_and_multipart(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["ctype"] = request.headers.get("content-type", "")
        seen["path"] = request.url.path
        return httpx.Response(200, json={"text": "сайн байна уу"})

    chunk = tmp_path / "chunk.wav"
    _wav(chunk)
    text = await _client(handler).transcribe_chunk_text(chunk)

    assert text == "сайн байна уу"
    assert seen["auth"] == "Bearer key"
    assert seen["ctype"].startswith("multipart/form-data")
    assert seen["path"].endswith("/audio/transcriptions")


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"text": "нэг"}, "нэг"),
        ({"transcription": "хоёр"}, "хоёр"),
        ({"result": "гурав"}, "гурав"),
        ({"segments": [{"text": "дөрөв"}, {"text": "тав"}]}, "дөрөв тав"),
        ("шууд мөр", "шууд мөр"),
    ],
)
def test_parse_response_tolerates_the_common_shapes(payload, expected):
    """Tolerant on purpose: a small difference from the assumed spec should
    surface as slightly-off parsing, not a hard failure on the first real
    call against a paid API."""
    assert DuudlagaClient._parse_response(payload) == expected


def test_parse_response_rejects_an_unknown_shape():
    """Returning "" for an unrecognised body would be indistinguishable from
    "this audio contained no speech" — the worst possible failure mode for a
    transcription service."""
    with pytest.raises(DuudlagaError, match="Unrecognised"):
        DuudlagaClient._parse_response({"unexpected": {"nested": 1}})


@pytest.mark.asyncio
async def test_retries_a_503_then_succeeds(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"text": "болсон"})

    chunk = tmp_path / "c.wav"
    _wav(chunk)
    assert await _client(handler).transcribe_chunk_text(chunk) == "болсон"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_does_not_retry_a_400(tmp_path):
    """A rejected request is rejected the same way every time; retrying only
    spends the rate limit."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad audio")

    chunk = tmp_path / "c.wav"
    _wav(chunk)
    with pytest.raises(DuudlagaError):
        await _client(handler).transcribe_chunk_text(chunk)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_401_names_the_key_and_404_names_the_path(tmp_path):
    """The path is the single most likely thing to be wrong, since it was
    assumed rather than read from a spec — the error has to say so."""
    chunk = tmp_path / "c.wav"
    _wav(chunk)

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


@pytest.mark.asyncio
async def test_transcribe_splits_at_pauses_and_offsets_each_chunk(tmp_path, monkeypatch):
    """The provider returns text with no timing. Timing comes from OUR cut
    boundaries, so each chunk's text must land at that chunk's own start —
    forgetting the offset is the classic chunked-STT bug."""
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

    texts = iter(["эхлэл.", "дунд.", "төгсгөл."])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": next(texts)})

    result = await _client(handler).transcribe(audio)

    assert len(result.segments) == 3
    # Cuts fall at pause midpoints: 10.25 and 20.25.
    assert [round(s.start, 2) for s in result.segments] == [0.0, 10.25, 20.25]
    assert result.full_text == "эхлэл. дунд. төгсгөл."


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
