"""AnthropicClient - no network, no real API key needed.

Mirrors test_openai_client.py's shape (same MockTransport pattern) since
AnthropicClient implements the same LLMClient interface as OpenAIClient.
"""
from __future__ import annotations

import json

import anthropic
import httpx
import pytest

from app.ai.anthropic_client import (
    AnthropicClient,
    AnthropicConfig,
    AnthropicError,
    _strip_array_size_constraints,
)


def _client(handler) -> AnthropicClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk_client = anthropic.AsyncAnthropic(api_key="sk-test", http_client=http_client)
    return AnthropicClient(AnthropicConfig(api_key="sk-test", model="claude-sonnet-5"), client=sdk_client)


def _ok_response(
    text: str = "{}", stop_reason: str = "end_turn", stop_details: dict | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


@pytest.mark.asyncio
async def test_complete_json_disables_thinking_by_default():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response('{"ok": true}')

    client = _client(handler)
    result = await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()

    assert seen["body"]["thinking"] == {"type": "disabled"}
    assert seen["body"]["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_omits_thinking_for_haiku():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response('{"ok": true}')

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk_client = anthropic.AsyncAnthropic(api_key="sk-test", http_client=http_client)
    client = AnthropicClient(AnthropicConfig(api_key="sk-test", model="claude-haiku-4-5"), client=sdk_client)
    result = await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()

    assert "thinking" not in seen["body"]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_retries_without_thinking_disable_when_unsupported():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "thinking" in body:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking disabled is not supported for this model",
                    },
                },
            )
        return _ok_response('{"ok": true}')

    client = _client(handler)
    result = await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()

    assert len(bodies) == 2
    assert "thinking" in bodies[0]
    assert "thinking" not in bodies[1]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_complete_json_remembers_thinking_unsupported_across_calls():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        body = json.loads(request.content)
        if "thinking" in body:
            error = {"type": "invalid_request_error", "message": "thinking not supported"}
            return httpx.Response(400, json={"type": "error", "error": error})
        return _ok_response('{"ok": true}')

    client = _client(handler)
    await client.complete_json("sys", "user", {"type": "object"}, "name")  # 2 attempts (fail, then succeed)
    await client.complete_json("sys", "user", {"type": "object"}, "name")  # should be 1 attempt now
    await client.aclose()

    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_complete_json_raises_on_refusal():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(
            text="",
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        )

    client = _client(handler)
    with pytest.raises(AnthropicError, match="cyber"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_json_missing_config_raises_without_request():
    client = AnthropicClient(AnthropicConfig(api_key="", model=""))
    with pytest.raises(AnthropicError):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_json_raises_on_non_json_content():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response("not valid json")

    client = _client(handler)
    with pytest.raises(AnthropicError, match="not valid JSON"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_json_hints_truncation_on_max_tokens_json_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response('{"shorts": [', stop_reason="max_tokens")

    client = _client(handler)
    with pytest.raises(AnthropicError, match="truncated"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


@pytest.mark.asyncio
async def test_complete_json_raises_on_unrelated_400():
    def handler(request: httpx.Request) -> httpx.Response:
        error = {"type": "invalid_request_error", "message": "Invalid API key provided"}
        return httpx.Response(400, json={"type": "error", "error": error})

    client = _client(handler)
    with pytest.raises(AnthropicError, match="Invalid API key"):
        await client.complete_json("sys", "user", {"type": "object"}, "name")
    await client.aclose()


def test_strip_array_size_constraints_removes_min_and_max_items():
    """Regression test: Anthropic's structured-output schema support rejects
    minItems/maxItems values other than 0 or 1 (400 invalid_request_error:
    "For 'array' type, 'minItems' values other than 0 or 1 are not
    supported") - this app's real schemas (app.ai.prompts) use 2+ everywhere.
    """
    schema = {
        "type": "object",
        "properties": {
            "shorts": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "cuts": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"type": "object"}},
                    },
                },
            },
        },
    }
    cleaned = _strip_array_size_constraints(schema)

    assert "minItems" not in cleaned["properties"]["shorts"]
    assert "maxItems" not in cleaned["properties"]["shorts"]
    assert "minItems" not in cleaned["properties"]["shorts"]["items"]["properties"]["cuts"]
    assert "maxItems" not in cleaned["properties"]["shorts"]["items"]["properties"]["cuts"]
    # the shape (types, required-ness, nesting) is otherwise untouched
    assert cleaned["properties"]["shorts"]["type"] == "array"
    # original dict passed in is not mutated - OpenAI's client uses the same
    # shared app.ai.prompts schema objects and needs minItems/maxItems intact
    assert schema["properties"]["shorts"]["minItems"] == 3


@pytest.mark.asyncio
async def test_complete_json_strips_array_size_constraints_from_outgoing_schema():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok_response('{"ok": true}')

    shorts_array = {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "object"}}
    schema_with_constraints = {"type": "object", "properties": {"shorts": shorts_array}}
    client = _client(handler)
    await client.complete_json("sys", "user", schema_with_constraints, "suggestions")
    await client.aclose()

    sent_schema = seen["body"]["output_config"]["format"]["schema"]
    assert "minItems" not in sent_schema["properties"]["shorts"]
    assert "maxItems" not in sent_schema["properties"]["shorts"]
