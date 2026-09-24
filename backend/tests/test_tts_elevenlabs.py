"""ElevenLabs text to speech.

What these hold: the request is the one that was chosen (v3, the voice in the
path — the default, or a speaker's own — the format in the query); a busy
answer is retried because it was never billed, and nothing else is; the
failures no retry can fix end the run and say so; and the voice and model
lists are read defensively, because the contract could not be checked
against the live documentation from the network this was written on.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.tts.elevenlabs import (
    BUSY_RETRIES,
    OUTPUT_FORMAT,
    ElevenLabsTts,
    TtsError,
    VoiceConfig,
    is_mongolian,
)


def _client(handler, **config) -> tuple[ElevenLabsTts, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    cfg = {"api_key": "k", "voice_id": "Voice123", "base_url": "https://fake/v1", **config}
    client = ElevenLabsTts(
        VoiceConfig(**cfg),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )
    return client, slept


@pytest.mark.asyncio
async def test_a_line_is_sent_to_v3_in_the_chosen_voice():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"ID3mp3")

    client, _ = _client(handler)
    assert await client.synthesize("  Сайн байна уу.  ") == b"ID3mp3"

    (request,) = seen
    assert request.url.path == "/v1/text-to-speech/Voice123"
    assert request.url.params["output_format"] == OUTPUT_FORMAT
    assert request.headers["xi-api-key"] == "k"
    body = json.loads(request.content)
    assert body["model_id"] == "eleven_v3"
    assert body["text"] == "Сайн байна уу."


@pytest.mark.asyncio
async def test_a_busy_answer_is_asked_again_because_it_was_never_billed():
    answers = iter([httpx.Response(429), httpx.Response(429, headers={"retry-after": "1"}),
                    httpx.Response(200, content=b"mp3")])
    client, slept = _client(lambda request: next(answers))
    assert await client.synthesize("Нэг.") == b"mp3"
    assert slept == [2.0, 1.0]


@pytest.mark.asyncio
async def test_busy_forever_gives_up_with_the_reason():
    calls = []

    def handler(request):
        calls.append(1)
        body = {"detail": {"status": "too_many_concurrent_requests", "message": "slow down"}}
        return httpx.Response(429, json=body)

    client, _ = _client(handler)
    with pytest.raises(TtsError) as excinfo:
        await client.synthesize("Нэг.")
    assert len(calls) == BUSY_RETRIES + 1
    assert "Зэрэг хүсэлт хэт олон" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_long_retry_after_is_a_quota_window_not_a_busy_moment():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "3600"})

    client, slept = _client(handler)
    with pytest.raises(TtsError):
        await client.synthesize("Нэг.")
    assert calls == [1] and slept == []


@pytest.mark.asyncio
async def test_a_server_error_is_not_retried_because_it_may_have_been_billed():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    client, _ = _client(handler)
    with pytest.raises(TtsError):
        await client.synthesize("Нэг.")
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_spent_quota_ends_the_run_and_says_why():
    body = {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota"}}
    client, _ = _client(lambda request: httpx.Response(401, json=body))
    with pytest.raises(TtsError) as excinfo:
        await client.synthesize("Нэг.")
    assert excinfo.value.ends_the_run
    assert "Тэмдэгтийн үлдэгдэл дууссан" in str(excinfo.value)
    assert "{" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_voice_gone_from_the_account_ends_the_run_and_says_which():
    """A speaker's voice deleted after it was given: every line of theirs
    would be refused the same way, so no retry is spent on it."""
    body = {"detail": {"status": "voice_not_found", "message": "A voice with that ID does not exist"}}
    client, _ = _client(lambda request: httpx.Response(400, json=body))
    with pytest.raises(TtsError) as excinfo:
        await client.synthesize("Нэг.", "Gone")
    assert excinfo.value.ends_the_run
    assert "хоолой олдсонгүй" in str(excinfo.value)


@pytest.mark.parametrize("code", ["invalid_uid", "model_not_found"])
def test_a_wrong_voice_or_model_ends_the_run(code):
    assert TtsError("x", status=400, code=code).ends_the_run
    assert not TtsError("x", status=400, code="max_character_limit_exceeded").ends_the_run


@pytest.mark.asyncio
async def test_a_line_is_sent_in_the_voice_it_was_given():
    seen: list[httpx.Request] = []
    client, _ = _client(lambda request: seen.append(request) or httpx.Response(200, content=b"x"))
    await client.synthesize("Нэг.", "Speaker2Voice")
    await client.synthesize("Хоёр.")
    assert [r.url.path for r in seen] == ["/v1/text-to-speech/Speaker2Voice",
                                         "/v1/text-to-speech/Voice123"]


@pytest.mark.parametrize("voice_id", ["", "../user", "a/b", "a b", "x" * 65])
@pytest.mark.asyncio
async def test_anything_but_an_id_never_reaches_the_url(voice_id):
    """The voice id is a path segment: `../user` would address another
    endpoint with the account's key."""
    calls = []
    client, _ = _client(lambda request: calls.append(1) or httpx.Response(200, content=b"x"),
                        voice_id=voice_id)
    with pytest.raises(TtsError):
        await client.synthesize("Нэг.")
    assert calls == []


@pytest.mark.parametrize("voice_id", ["../user", "a/b", "a%2Fb", "x" * 65])
@pytest.mark.asyncio
async def test_a_speakers_voice_is_held_to_an_id_too(voice_id):
    """The schema refuses these; this is the second wall, at the URL."""
    calls = []
    client, _ = _client(lambda request: calls.append(1) or httpx.Response(200, content=b"x"))
    with pytest.raises(TtsError) as excinfo:
        await client.synthesize("Нэг.", voice_id)
    assert calls == [] and excinfo.value.ends_the_run


@pytest.mark.asyncio
async def test_a_voice_cleared_after_queueing_ends_the_run():
    """The route refuses a voice-over with no voice; one cleared while the
    export waited in the queue must not be retried three times for it."""
    client, _ = _client(lambda request: httpx.Response(200, content=b"x"), voice_id="")
    with pytest.raises(TtsError) as excinfo:
        await client.synthesize("Нэг.")
    assert excinfo.value.ends_the_run
    client, _ = _client(lambda request: httpx.Response(200, content=b"x"), api_key="")
    with pytest.raises(TtsError) as excinfo:
        await client.synthesize("Нэг.")
    assert excinfo.value.ends_the_run


@pytest.mark.asyncio
async def test_nothing_is_sent_without_a_key_or_without_words():
    calls = []
    client, _ = _client(lambda request: calls.append(1) or httpx.Response(200, content=b"x"),
                        api_key="")
    with pytest.raises(TtsError):
        await client.synthesize("Нэг.")
    client, _ = _client(lambda request: calls.append(1) or httpx.Response(200, content=b"x"))
    with pytest.raises(TtsError):
        await client.synthesize("   ")
    assert calls == []


@pytest.mark.asyncio
async def test_an_empty_answer_is_a_failure_not_a_silent_line():
    client, _ = _client(lambda request: httpx.Response(200, content=b""))
    with pytest.raises(TtsError):
        await client.synthesize("Нэг.")


def test_the_fingerprint_changes_with_the_voice_and_the_model_but_not_the_key():
    base = VoiceConfig(api_key="a", voice_id="V1")
    assert base.fingerprint() == VoiceConfig(api_key="b", voice_id="V1").fingerprint()
    assert base.fingerprint() != VoiceConfig(api_key="a", voice_id="V2").fingerprint()
    other_model = VoiceConfig(api_key="a", voice_id="V1", model="eleven_multilingual_v2")
    assert base.fingerprint() != other_model.fingerprint()


def test_a_speakers_voice_is_fingerprinted_as_that_voice():
    """A speaker given V2 sounds exactly like a default of V2 — the same
    clips — and a speaker given nothing sounds like the default."""
    base = VoiceConfig(api_key="a", voice_id="V1")
    assert base.fingerprint("V2") == VoiceConfig(api_key="a", voice_id="V2").fingerprint()
    assert base.fingerprint(None) == base.fingerprint() != base.fingerprint("V2")


# --------------------------------------------------------------------------
# The lists the admin picks from
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_voices_are_listed_with_what_a_producer_chooses_by():
    body = {"voices": [
        {"voice_id": "b2", "name": "Zed", "category": "premade",
         "labels": {"gender": "male", "accent": "american"}, "preview_url": "https://cdn/zed.mp3"},
        {"voice_id": "a1", "name": "Ana"},
        {"name": "no id — skipped"},
        "not even an object",
    ]}
    client, _ = _client(lambda request: httpx.Response(200, json=body))
    voices = await client.voices()
    assert [v["id"] for v in voices] == ["a1", "b2"]
    assert voices[1] == {"id": "b2", "name": "Zed", "category": "premade", "gender": "male",
                         "accent": "american", "preview_url": "https://cdn/zed.mp3"}
    assert voices[0]["preview_url"] is None


@pytest.mark.asyncio
async def test_the_models_languages_are_read_for_the_chosen_model_only():
    body = [
        {"model_id": "eleven_flash_v2_5", "languages": [{"language_id": "en"}]},
        {"model_id": "eleven_v3", "languages": [{"language_id": "ru"}, {"language_id": "mn"}]},
    ]
    client, _ = _client(lambda request: httpx.Response(200, json=body))
    assert await client.model_languages("eleven_v3") == ["mn", "ru"]
    assert await client.model_languages("nonexistent") is None


def test_mongolian_is_recognised_in_either_code_and_unknown_is_not_no():
    assert is_mongolian(["en", "mn"]) is True
    assert is_mongolian(["MON"]) is True
    assert is_mongolian(["en", "ru"]) is False
    assert is_mongolian(None) is None
