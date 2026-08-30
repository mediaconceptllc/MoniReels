"""OpenRouter client.

Covers the three capability fallbacks that exist because real providers
rejected these requests, and the cost meter. Each fallback must be probed
ONCE and then remembered — a per-call probe doubles every request.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.ai import usage as usage_meter
from app.ai.openrouter_client import MAX_TOKENS, OpenRouterClient, OpenRouterConfig, OpenRouterError

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"type": "string"}}
    },
}


def _client(handler, **cfg) -> OpenRouterClient:
    return OpenRouterClient(
        OpenRouterConfig(api_key="sk-test", model="test/model", base_url="https://fake/api/v1", **cfg),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _ok(content: dict, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test/model",
            "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.0012},
        },
    )


@pytest.mark.asyncio
async def test_sends_json_schema_and_returns_parsed_object():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return _ok({"items": ["a", "b", "c"]})

    result = await _client(handler).complete_json("sys", "user", SCHEMA, "things")

    assert result == {"items": ["a", "b", "c"]}
    assert seen["url"] == "https://fake/api/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["response_format"]["json_schema"]["name"] == "things"
    assert seen["body"]["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_max_tokens_is_always_capped():
    """Uncapped, a provider reserves the model's ENTIRE output ceiling against
    the account's per-minute limit — which is what actually produces 429s on
    a small account, not the size of the transcript."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _ok({"items": []})

    await _client(handler).complete_json("s", "u", SCHEMA, "n")
    assert seen["body"]["max_tokens"] == MAX_TOKENS


@pytest.mark.asyncio
async def test_retries_once_without_temperature_then_remembers():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append("temperature" in body)
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "temperature is not supported"}})
        return _ok({"items": []})

    client = _client(handler)
    await client.complete_json("s", "u", SCHEMA, "n")
    await client.complete_json("s", "u", SCHEMA, "n")

    # First call tries with temperature and retries without; the second call
    # must not re-probe.
    assert calls == [True, False, False]


@pytest.mark.asyncio
async def test_strips_array_bounds_when_the_provider_rejects_them():
    """Anthropic-backed models reject minItems/maxItems above 1. Dropping them
    is safe because app.ai.schema enforces the real counts in Python."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        has_bounds = "minItems" in schema["properties"]["items"]
        seen.append(has_bounds)
        if has_bounds:
            return httpx.Response(
                400, json={"error": {"message": "'minItems' values other than 0 or 1 are not supported"}}
            )
        return _ok({"items": []})

    client = _client(handler)
    await client.complete_json("s", "u", SCHEMA, "n")
    assert seen == [True, False]

    await client.complete_json("s", "u", SCHEMA, "n")
    assert seen == [True, False, False]


@pytest.mark.asyncio
async def test_upstream_error_in_a_200_body_is_still_an_error():
    """OpenRouter reports a provider failure as HTTP 200 with an `error`
    object. Treating that as success yields a KeyError on `choices` far from
    the cause."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "upstream is down", "code": 502}})

    with pytest.raises(OpenRouterError, match="upstream is down"):
        await _client(handler).complete_json("s", "u", SCHEMA, "n")


@pytest.mark.asyncio
async def test_truncated_output_names_the_token_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "test/model",
                "choices": [{"message": {"content": '{"items": ["a"'}, "finish_reason": "length"}],
            },
        )

    with pytest.raises(OpenRouterError, match="token limit"):
        await _client(handler).complete_json("s", "u", SCHEMA, "n")


@pytest.mark.asyncio
async def test_cost_is_recorded_per_job_not_globally():
    """Jobs run concurrently in one worker; their costs must not blend."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _ok({"items": []}, usage={"prompt_tokens": 900, "completion_tokens": 100, "cost": 0.004})

    usage = usage_meter.start()
    client = _client(handler)
    await client.complete_json("s", "u", SCHEMA, "n")
    await client.complete_json("s", "u", SCHEMA, "n")

    assert usage.calls == 2
    assert usage.prompt_tokens == 1800
    assert usage.cost_usd == pytest.approx(0.008)
    assert usage.to_dict()["models"] == ["test/model"]


@pytest.mark.asyncio
async def test_missing_key_fails_before_any_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the network without a key")

    client = OpenRouterClient(
        OpenRouterConfig(api_key="", model="m"),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(OpenRouterError, match="OPENROUTER_API_KEY"):
        await client.complete_json("s", "u", SCHEMA, "n")


@pytest.mark.asyncio
async def test_attribution_headers_are_optional():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["referer"] = request.headers.get("http-referer")
        seen["title"] = request.headers.get("x-title")
        return _ok({"items": []})

    await _client(handler, app_url="", app_title="").complete_json("s", "u", SCHEMA, "n")
    assert seen["referer"] is None
    assert seen["title"] is None
