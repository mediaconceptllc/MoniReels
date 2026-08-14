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


def _transcript(n: int, text_len: int = 5) -> Transcript:
    """1-second-per-index segments (short text, so split_long_segments never
    further subdivides them) - keeps cut/keep-range indices in the fake
    model responses below easy to reason about: index i is always second i.
    """
    segments = [Segment(id=str(i), start=float(i), end=float(i + 1), text="w" * text_len) for i in range(n)]
    return Transcript(language="mn", segments=segments, full_text=" ".join(s.text for s in segments))


def _schema_name(request: httpx.Request) -> str:
    body = json.loads(request.content)
    return body["response_format"]["json_schema"]["name"]


def _cut_dict(start_index: int, end_index: int, role: str = "context", reason: str = "r") -> dict:
    return {"start_index": start_index, "end_index": end_index, "role": role, "reason": reason}


def _valid_cut_dicts(offset: int = 0) -> list[dict]:
    """hook(10s) + context(15s) + payoff(15s) = 40s - inside the 35-60s window."""
    return [
        _cut_dict(offset, offset + 9, role="hook"),
        _cut_dict(offset + 10, offset + 24, role="context"),
        _cut_dict(offset + 25, offset + 39, role="payoff"),
    ]


def _short_dict(title: str, offset: int = 0) -> dict:
    return {
        # "w" (not e.g. "q") since validate_shorts checks hook_quote is a
        # verbatim transcript substring, and _transcript()'s segment text is
        # always "w" * text_len.
        "title": title, "hook_text": "h", "hook_quote": "w", "cuts": _valid_cut_dicts(offset),
        "on_screen_texts": [], "b_roll": [], "caption": "d", "hashtags": [], "why_it_works": "w",
    }


def _youtube_dict(title: str, keep_ranges: list[tuple[int, int]]) -> dict:
    return {
        "title": title, "throughline": "d",
        "keep_ranges": [{"start_index": s, "end_index": e} for s, e in keep_ranges],
    }


def _three_shorts_dicts() -> list[dict]:
    return [_short_dict("A", 0), _short_dict("B", 50), _short_dict("C", 100)]


def _three_youtube_dicts() -> list[dict]:
    return [
        _youtube_dict("Y1", [(0, 99)]),
        _youtube_dict("Y2", [(150, 249)]),
        _youtube_dict("Y3", [(300, 399)]),
    ]


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
        return _openai_response({"shorts": _three_shorts_dicts(), "youtube": []})

    transcript = _transcript(150)
    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=150.0)
    await client.aclose()

    assert calls["n"] == 1
    assert len(result.shorts) == 3
    assert result.youtube == []


@pytest.mark.asyncio
async def test_generate_suggestions_retries_once_on_wrong_short_count():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        two_shorts = {"shorts": [_short_dict("A", 0), _short_dict("B", 50)], "youtube": []}
        if calls["n"] == 1:
            return _openai_response(two_shorts)
        return _openai_response({"shorts": _three_shorts_dicts(), "youtube": []})

    transcript = _transcript(150)
    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=150.0)
    await client.aclose()

    assert calls["n"] == 2
    assert len(result.shorts) == 3


@pytest.mark.asyncio
async def test_generate_suggestions_fails_after_retry_still_wrong_count():
    two_shorts = {"shorts": [_short_dict("A", 0), _short_dict("B", 50)], "youtube": []}

    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response(two_shorts)

    transcript = _transcript(150)
    client = _client(handler)
    with pytest.raises(SuggestionValidationError):
        await generate_suggestions(client, transcript, duration_sec=150.0)
    await client.aclose()


@pytest.mark.asyncio
async def test_generate_suggestions_retries_once_on_invalid_cut_structure():
    """Business-rule failure (not a shape failure): last cut isn't 'payoff'.
    Caught by validate_shorts before Pydantic even runs, same repair-retry
    path as a wrong short count.
    """
    calls = {"n": 0}
    bad_cuts = [
        _cut_dict(0, 9, role="hook"), _cut_dict(10, 24, role="context"), _cut_dict(25, 39, role="proof")
    ]
    bad_short = {**_short_dict("A", 0), "cuts": bad_cuts}
    bad_response = {"shorts": [bad_short, _short_dict("B", 50), _short_dict("C", 100)], "youtube": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _openai_response(bad_response)
        return _openai_response({"shorts": _three_shorts_dicts(), "youtube": []})

    transcript = _transcript(150)
    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=150.0)
    await client.aclose()

    assert calls["n"] == 2
    assert len(result.shorts) == 3


@pytest.mark.asyncio
async def test_generate_suggestions_chunks_long_transcript_and_picks_best():
    # ~600 one-second segments -> well over the 45k char single-request budget.
    transcript = _transcript(600, text_len=80)
    calls = {"candidates": 0, "suggestions": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        name = _schema_name(request)
        if name == "candidates":
            calls["candidates"] += 1
            cand = _short_dict(f"cand-{calls['candidates']}", 0)
            return _openai_response({"shorts": [cand], "youtube": []})
        calls["suggestions"] += 1
        return _openai_response({"shorts": _three_shorts_dicts(), "youtube": []})

    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=600.0)
    await client.aclose()

    assert calls["candidates"] > 1  # transcript was actually chunked, not sent whole
    assert calls["suggestions"] == 1  # exactly one final pick-best call
    assert len(result.shorts) == 3


@pytest.mark.asyncio
async def test_generate_suggestions_single_pass_long_video_returns_three_youtube_plans():
    def handler(request: httpx.Request) -> httpx.Response:
        return _openai_response({"shorts": _three_shorts_dicts(), "youtube": _three_youtube_dicts()})

    transcript = _transcript(500)
    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=1500.0)  # 25 min >= 20 min gate
    await client.aclose()

    assert len(result.youtube) == 3
    assert [p.title for p in result.youtube] == ["Y1", "Y2", "Y3"]


@pytest.mark.asyncio
async def test_generate_suggestions_chunks_flatten_multiple_youtube_plans_from_candidates():
    """Map-reduce branch: each chunk can return multiple candidate youtube
    plans (not just one) - the flattened `for plan in candidates.youtube:
    for r in plan.keep_ranges` loop must collect ranges from all of them,
    not just the first, into the final pick-best prompt.
    """
    transcript = _transcript(600, text_len=80)  # forces chunking
    pick_best_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = _schema_name(request)
        if name == "candidates":
            return _openai_response(
                {
                    "shorts": [_short_dict("cand", 0)],
                    "youtube": [_youtube_dict("A", [(0, 49)]), _youtube_dict("B", [(60, 99)])],
                }
            )
        pick_best_bodies.append(json.loads(request.content)["messages"][-1]["content"])
        return _openai_response({"shorts": _three_shorts_dicts(), "youtube": _three_youtube_dicts()})

    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=1500.0)
    await client.aclose()

    assert len(result.youtube) == 3
    assert len(pick_best_bodies) == 1
    # Ranges from BOTH candidate plans (A's 0-49 and B's 60-99), from every
    # chunk - proves the nested loop didn't stop at the first plan per chunk.
    assert "[0-49]" in pick_best_bodies[0]
    assert "[60-99]" in pick_best_bodies[0]
