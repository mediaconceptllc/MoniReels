"""What of an outside service's error body reaches a user.

A failing job records its exception text in `jobs.error`, and `GET /jobs/{id}`
returns that to the project's owner. So whatever a provider chose to put in
its error body is what the person sees — and that body is the only text in
this system that nobody here wrote or reviewed.

MEASURED: an OpenRouter 400 arrived carrying an account identifier beside its
message. Not a secret, and the route is behind a login; the point is that the
useful half is always the same field, so there is no reason to pass on the
rest.
"""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.ai.openrouter_client import OpenRouterClient, OpenRouterConfig, OpenRouterError
from app.stt.duudlaga_client import _error_from_response
from app.stt.elevenlabs_client import ElevenLabsError, ElevenLabsSttClient, ElevenLabsSttConfig
from app.utils.provider_errors import MESSAGE_MAX, read_error

#: The body production actually received, kept verbatim.
OPENROUTER_400 = {
    "error": {"message": "gtp-5 is not a valid model ID", "code": 400},
    "user_id": "user_2oISXvisAcCOUNTiDeNTiFieR",
}
ACCOUNT_ID = "user_2oISXvisAcCOUNTiDeNTiFieR"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
}


def _response(status: int, **kw) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://fake/v1/x"), **kw)


# --------------------------------------------------------------------------
# The reader
# --------------------------------------------------------------------------


def test_the_message_is_taken_and_the_rest_is_left():
    read = read_error(_response(400, json=OPENROUTER_400))
    assert read.message == "gtp-5 is not a valid model ID"
    assert read.code == "400"
    assert ACCOUNT_ID not in (read.message or "")


def test_a_flat_body_is_read_too():
    """duudlaga documents `{code, message}` with no wrapper, and the code is
    what decides whether a failure is retried."""
    read = read_error(_response(429, json={"code": "rate_limited", "message": "Хэт олон хүсэлт"}))
    assert (read.code, read.message) == ("rate_limited", "Хэт олон хүсэлт")


def test_a_bare_string_error_is_read():
    assert read_error(_response(429, json={"error": "slow down"})).message == "slow down"


def test_html_from_a_proxy_yields_nothing_rather_than_html():
    """The fallback that must NOT exist. A body this reader cannot understand
    is a body it has no business relaying."""
    page = "<html><title>504 Gateway Time-out</title><body>nginx/1.18.0</body></html>"
    read = read_error(_response(504, text=page))
    assert read == read_error(_response(504, text=""))
    assert read.message is None and read.code is None


def test_a_bodiless_status_yields_nothing():
    """A CDN in front of a provider answers for a dead origin with an empty
    body — production hit exactly this as a 520."""
    read = read_error(_response(520, text=""))
    assert read.message is None


def test_a_long_message_is_cut():
    read = read_error(_response(400, json={"error": {"message": "ш" * 5000}}))
    assert len(read.message) == MESSAGE_MAX


def test_a_json_body_that_is_not_an_object_yields_nothing():
    assert read_error(_response(400, json=["nope"])).message is None


# --------------------------------------------------------------------------
# OpenRouter, end to end
# --------------------------------------------------------------------------


def _openrouter(handler) -> OpenRouterClient:
    return OpenRouterClient(
        OpenRouterConfig(api_key="sk-test", model="test/model", base_url="https://fake/api/v1"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def test_the_account_identifier_never_reaches_the_user(caplog):
    """The whole finding, at the point where it mattered."""

    def handler(request):
        return httpx.Response(400, json=OPENROUTER_400)

    client = _openrouter(handler)
    with caplog.at_level(logging.WARNING), pytest.raises(OpenRouterError) as excinfo:
        asyncio.run(client.complete_json("s", "u", SCHEMA, "items"))

    message = str(excinfo.value)
    assert "gtp-5 is not a valid model ID" in message, "the useful half must survive"
    assert ACCOUNT_ID not in message
    assert "user_id" not in message
    # And the operator is not left blind: the whole body is in the log.
    assert ACCOUNT_ID in caplog.text


def test_a_status_with_no_readable_body_still_names_the_status():
    """"Failed" with no number is unactionable; the status alone already
    separates a key problem from a rate limit from a dead provider."""

    def handler(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    client = _openrouter(handler)
    with pytest.raises(OpenRouterError) as excinfo:
        asyncio.run(client.complete_json("s", "u", SCHEMA, "items"))

    message = str(excinfo.value)
    assert "502" in message
    assert "html" not in message.lower()


# --------------------------------------------------------------------------
# ElevenLabs, end to end
# --------------------------------------------------------------------------


def test_elevenlabs_relays_its_message_and_not_its_body(tmp_path, caplog):
    import wave

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)

    body = {
        "detail": {"status": "invalid_api_key", "message": "Түлхүүр буруу"},
        "internal_trace_id": "trace_SHOULDNOTLEAK",
    }

    def handler(request):
        return httpx.Response(401, json=body)

    client = ElevenLabsSttClient(
        ElevenLabsSttConfig(api_key="k", base_url="https://fake"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(ElevenLabsError) as excinfo:
        asyncio.run(client.transcribe(wav))

    assert "401" in str(excinfo.value)
    assert "trace_SHOULDNOTLEAK" not in str(excinfo.value)
    assert "trace_SHOULDNOTLEAK" in caplog.text


# --------------------------------------------------------------------------
# duudlaga keeps classifying on the code it reads through the same reader.
# --------------------------------------------------------------------------


def test_duudlaga_still_reads_the_code_that_decides_a_retry():
    """The reader is shared; the classification it feeds must be unchanged.
    Getting this wrong turns a retryable 429 into a fatal one, or the other
    way round — a daily cap retried until it is spent."""
    error = _error_from_response(
        _response(429, json={"code": "rate_limited", "message": "дахин оролдоно уу"})
    )
    assert error.code == "rate_limited"
    assert "дахин оролдоно уу" in str(error)


def test_duudlaga_says_something_useful_for_a_bodiless_failure():
    error = _error_from_response(_response(520, text=""))
    assert "520" in str(error)
    assert error.code is None


def test_no_client_relays_a_body_it_could_not_parse():
    """One rule, three clients — stated as a rule so a fourth inherits it."""
    junk = "SECRET-LOOKING-STRING-nobody-here-wrote"
    read = read_error(_response(500, text=junk))
    assert junk not in json.dumps(read.__dict__)
