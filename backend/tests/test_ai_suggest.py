"""End-to-end suggest() orchestration against a fake OpenAI transport — no
network, no real API key needed.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.ai.openai_client import OpenAIClient, OpenAIConfig
from app.ai.schema import SuggestionValidationError
from app.ai.suggest import generate_suggestions
from app.models import Segment, Transcript


def _transcript(n: int, text_len: int = 60) -> Transcript:
    segments = [
        Segment(id=str(i), start=float(i * 10), end=float(i * 10 + 8), text="w" * text_len)
        for i in range(n)
    ]
    return Transcript(language="mn", segments=segments, full_text=" ".join(s.text for s in segments))


def _schema_name(request: httpx.Request) -> str:
    body = json.loads(request.content)
    return body["response_format"]["json_schema"]["name"]


def _short_dict(start: float, end: float, title: str) -> dict:
    return {"title": title, "hook": "h", "description": "d", "start": start, "end": end}


def _client(handler) -> OpenAIClient:
    transport = httpx.MockTransport(handler)
    return OpenAIClient(
        OpenAIConfig(api_key="sk-test", model="gpt-test", base_url="https://fake.openai/v1"),
        http_client=httpx.AsyncClient(transport=transport),
    )


def _openai_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


@pytest.mark.asyncio
async def test_generate_suggestions_single_pass_short_transcript():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert _schema_name(request) == "suggestions"
        return _openai_response(
            {
                "shorts": [
                    _short_dict(0, 20, "A"),
                    _short_dict(30, 50, "B"),
                    _short_dict(60, 90, "C"),
                ],
                "youtube": None,
            }
        )

    transcript = _transcript(5)
    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=100.0)
    await client.aclose()

    assert calls["n"] == 1
    assert len(result.shorts) == 3
    assert result.youtube is None


@pytest.mark.asyncio
async def test_generate_suggestions_retries_once_on_wrong_short_count():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        two_shorts = {"shorts": [_short_dict(0, 20, "A"), _short_dict(30, 50, "B")], "youtube": None}
        if calls["n"] == 1:
            return _openai_response(two_shorts)
        return _openai_response(
            {
                "shorts": [_short_dict(0, 20, "A"), _short_dict(30, 50, "B"), _short_dict(60, 90, "C")],
                "youtube": None,
            }
        )

    transcript = _transcript(5)
    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=100.0)
    await client.aclose()

    assert calls["n"] == 2
    assert len(result.shorts) == 3


@pytest.mark.asyncio
async def test_generate_suggestions_fails_after_retry_still_wrong_count():
    two_shorts = {"shorts": [_short_dict(0, 20, "A"), _short_dict(30, 50, "B")], "youtube": None}

    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response(two_shorts)

    transcript = _transcript(5)
    client = _client(handler)
    with pytest.raises(SuggestionValidationError):
        await generate_suggestions(client, transcript, duration_sec=100.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_generate_suggestions_chunks_long_transcript_and_picks_best():
    # ~300 segments of ~70 chars each -> well over the 12k char single-request budget.
    transcript = _transcript(300, text_len=60)
    calls = {"candidates": 0, "suggestions": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        name = _schema_name(request)
        if name == "candidates":
            calls["candidates"] += 1
            return _openai_response(
                {"shorts": [_short_dict(0, 20, f"cand-{calls['candidates']}")], "youtube": None}
            )
        calls["suggestions"] += 1
        return _openai_response(
            {
                "shorts": [_short_dict(0, 20, "A"), _short_dict(30, 50, "B"), _short_dict(60, 90, "C")],
                "youtube": None,
            }
        )

    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=100.0)
    await client.aclose()

    assert calls["candidates"] > 1  # transcript was actually chunked, not sent whole
    assert calls["suggestions"] == 1  # exactly one final pick-best call
    assert len(result.shorts) == 3
