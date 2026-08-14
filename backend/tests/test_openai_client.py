"""OpenAIClient — no network, no real API key needed.

The retry-without-temperature test covers a real production incident:
gpt-5 rejected our default temperature=0.4 with a 400 ("Unsupported value:
'temperature' does not support 0.4 with this model. Only the default (1)
value is supported."), and the client had no fallback for it.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.ai.openai_client import OpenAIClient, OpenAIConfig, OpenAIError

TEMPERATURE_ERROR_BODY = {
    "error": {
        "message": "Unsupported value: 'temperature' does not support 0.4 with this model. "
        "Only the default (1) value is supported.",
        "type": "invalid_request_error",
        "param": "temperature",
        "code": "unsupported_value",
    }
}


def _client(handler) -> OpenAIClient:
    transport = httpx.MockTransport(handler)
    return OpenAIClient(
        OpenAIConfig(api_key="sk-test", model="gpt-test", base_url="https://fake.openai/v1"),
        http_client=httpx.AsyncClient(transport=transport),
    )


def _ok_response(text: str = "{}") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


@pytest.mark.asyncio
async def test_complete_json_sends_temperature_by_default():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response('{"ok": true}')

    client = _client(handler)
    result = await client.complete_json("sys", "user", {"type": "object"}, "name", temperature=0.4)
    await client.aclose()

    assert seen["body"]["temperature"] == 0.4
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_retries_without_temperature_on_unsupported_value():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "temperature" in body:
            return httpx.Response(400, json=TEMPERATURE_ERROR_BODY)
        return _ok_response('{"ok": true}')

    client = _client(handler)
    result = await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()

    assert len(bodies) == 2
    assert "temperature" in bodies[0]
    assert "temperature" not in bodies[1]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_remembers_temperature_unsupported_across_calls():
    """Once discovered, the client should stop sending temperature at all -
    no repeated failed round trip on every subsequent call.
    """
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        body = json.loads(request.content)
        if "temperature" in body:
            return httpx.Response(400, json=TEMPERATURE_ERROR_BODY)
        return _ok_response('{"ok": true}')

    client = _client(handler)
    await client.complete_json("sys", "user", {"type": "object"}, "name")  # 2 attempts (fail, then succeed)
    await client.complete_json("sys", "user", {"type": "object"}, "name")  # should be 1 attempt now
    await client.aclose()

    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_complete_json_does_not_retry_on_unrelated_400():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(
            400,
            json={"error": {"message": "Invalid API key provided", "type": "invalid_request_error"}},
        )

    client = _client(handler)
    with pytest.raises(OpenAIError, match="Invalid API key"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_complete_json_missing_config_raises_without_request():
    client = OpenAIClient(OpenAIConfig(api_key="", model="", base_url="https://fake.openai/v1"))
    with pytest.raises(OpenAIError):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_json_raises_on_malformed_response_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = _client(handler)
    with pytest.raises(OpenAIError, match="Unexpected OpenAI response shape"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_json_raises_on_non_json_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response("not valid json")

    client = _client(handler)
    with pytest.raises(OpenAIError, match="not valid JSON"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()
