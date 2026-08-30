"""End-to-end suggest() orchestration against a fake OpenRouter transport —
network, no real API key needed.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.ai.openrouter_client import OpenRouterClient, OpenRouterConfig
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


def _client(handler) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    return OpenRouterClient(
        OpenRouterConfig(api_key="sk-test", model="gpt-test", base_url="https://fake.openrouter/api/v1"),
        http_client=httpx.AsyncClient(transport=transport),
    )


def _llm_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


@pytest.mark.asyncio
async def test_generate_suggestions_single_pass_short_transcript():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert _schema_name(request) == "suggestions"
        return _llm_response({"shorts": _three_shorts_dicts(), "youtube": []})

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
            return _llm_response(two_shorts)
        return _llm_response({"shorts": _three_shorts_dicts(), "youtube": []})

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
        return _llm_response(two_shorts)

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
            return _llm_response(bad_response)
        return _llm_response({"shorts": _three_shorts_dicts(), "youtube": []})

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
    calls = {"candidates": 0, "pick_best": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        name = _schema_name(request)
        if name == "candidates":
            calls["candidates"] += 1
            cands = [_short_dict(f"cand-{calls['candidates']}-{j}", j * 50) for j in range(2)]
            return _llm_response({"shorts": cands, "youtube": []})
        calls["pick_best"] += 1
        assert name == "pick_best"
        return _llm_response({"short_indices": [0, 1, 2], "youtube_indices": []})

    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=600.0)
    await client.aclose()

    assert calls["candidates"] > 1  # transcript was actually chunked, not sent whole
    assert calls["pick_best"] == 1  # exactly one final pick call
    assert len(result.shorts) == 3


@pytest.mark.asyncio
async def test_generate_suggestions_candidates_retry_rescues_over_duration_short():
    """A chunk's candidates response can come back with valid shape but an
    over-duration cut (observed with Claude models: consistently 60-100%
    over the 35-60s target) - that's exactly what build_repair_prompt's
    specific "cut N to M seconds" guidance is for, so the candidates stage
    must get the same one-retry chance the final pick call already has,
    instead of silently discarding the near-miss.
    """
    transcript = _transcript(600, text_len=80)  # forces chunking
    candidate_attempts = {"n": 0}
    too_long_cuts = [
        _cut_dict(0, 9, role="hook"),
        _cut_dict(10, 39, role="context"),
        _cut_dict(40, 79, role="payoff"),
    ]  # 80s total - over the 60s max

    def handler(request: httpx.Request) -> httpx.Response:
        name = _schema_name(request)
        if name == "candidates":
            candidate_attempts["n"] += 1
            if candidate_attempts["n"] == 1:
                bad_short = {**_short_dict("too-long", 0), "cuts": too_long_cuts}
                return _llm_response({"shorts": [bad_short], "youtube": []})
            cands = [_short_dict(f"fixed-{j}", j * 50) for j in range(3)]
            return _llm_response({"shorts": cands, "youtube": []})
        return _llm_response({"short_indices": [0, 1, 2], "youtube_indices": []})

    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=600.0)
    await client.aclose()

    assert candidate_attempts["n"] >= 2  # the too-long candidate triggered a repair retry
    assert len(result.shorts) == 3


@pytest.mark.asyncio
async def test_generate_suggestions_single_pass_long_video_returns_three_youtube_plans():
    def handler(request: httpx.Request) -> httpx.Response:
        return _llm_response({"shorts": _three_shorts_dicts(), "youtube": _three_youtube_dicts()})

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
    not just the first, into the final pick prompt.
    """
    transcript = _transcript(600, text_len=80)  # forces chunking
    pick_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = _schema_name(request)
        if name == "candidates":
            cands = [_short_dict(f"cand-{len(pick_bodies)}-{j}", j * 50) for j in range(2)]
            return _llm_response(
                {
                    "shorts": cands,
                    "youtube": [_youtube_dict("A", [(0, 49)]), _youtube_dict("B", [(60, 99)])],
                }
            )
        pick_bodies.append(json.loads(request.content)["messages"][-1]["content"])
        return _llm_response({"short_indices": [0, 1, 2], "youtube_indices": [0, 1, 2]})

    client = _client(handler)
    result = await generate_suggestions(client, transcript, duration_sec=1500.0)
    await client.aclose()

    assert len(result.youtube) == 3
    assert len(pick_bodies) == 1
    # Ranges from BOTH candidate plans (A's 0-49 and B's 60-99), from every
    # chunk - proves the nested loop didn't stop at the first plan per chunk.
    assert "[0-49]" in pick_bodies[0]
    assert "[60-99]" in pick_bodies[0]
    # And the full transcript must NOT be resent in this final call - that's
    # the whole point of the pick-indices redesign (see build_pick_indices_prompt).
    assert "wwwwwwww" not in pick_bodies[0]
