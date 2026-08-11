"""Post-validation is what the spec explicitly asks to unit-test: canned LLM
responses covering "only 2 shorts returned" and "timestamps out of range".
"""
from __future__ import annotations

import pytest

from app.ai.schema import (
    RawKeepRange,
    RawShort,
    RawSuggestions,
    RawYoutubePlan,
    SuggestionValidationError,
    postprocess_suggestions,
    validate_llm_output,
)


def _short(start: float, end: float, title: str = "T") -> RawShort:
    return RawShort(title=title, hook="hook", description="desc", start=start, end=end)


def _three_valid_shorts() -> list[RawShort]:
    return [_short(0, 30, "A"), _short(60, 100, "B"), _short(150, 200, "C")]


# --------------------------------------------------------------------------
# Layer 1: schema validation.
# --------------------------------------------------------------------------


def test_validate_llm_output_valid():
    raw = validate_llm_output({"shorts": [_short(0, 30).model_dump()], "youtube": None})
    assert len(raw.shorts) == 1


def test_validate_llm_output_missing_field_raises():
    with pytest.raises(SuggestionValidationError):
        validate_llm_output({"shorts": [{"title": "x"}], "youtube": None})


def test_validate_llm_output_wrong_type_raises():
    with pytest.raises(SuggestionValidationError):
        validate_llm_output({"shorts": "not a list", "youtube": None})


# --------------------------------------------------------------------------
# "only 2 shorts returned"
# --------------------------------------------------------------------------


def test_postprocess_fails_when_only_two_shorts():
    raw = RawSuggestions(shorts=[_short(0, 30), _short(60, 100)], youtube=None)
    with pytest.raises(SuggestionValidationError, match="exactly 3"):
        postprocess_suggestions(raw, duration_sec=300.0)


def test_postprocess_fails_when_four_shorts():
    raw = RawSuggestions(shorts=[*_three_valid_shorts(), _short(210, 240)], youtube=None)
    with pytest.raises(SuggestionValidationError, match="exactly 3"):
        postprocess_suggestions(raw, duration_sec=300.0)


# --------------------------------------------------------------------------
# "timestamps out of range"
# --------------------------------------------------------------------------


def test_postprocess_clamps_negative_start_and_over_duration_end():
    raw = RawSuggestions(
        shorts=[_short(-10, 40), _short(60, 100), _short(150, 200)], youtube=None
    )
    result = postprocess_suggestions(raw, duration_sec=180.0)
    first = result.shorts[0]
    assert first.start == 0.0  # clamped
    third = result.shorts[2]
    assert third.end == 180.0  # clamped to video duration


def test_postprocess_shrinks_overlong_short_to_max_duration():
    raw = RawSuggestions(
        shorts=[_short(0, 200), _short(210, 240), _short(250, 280)], youtube=None
    )
    result = postprocess_suggestions(raw, duration_sec=300.0)
    first = result.shorts[0]
    assert first.end - first.start == 90.0


def test_postprocess_extends_short_short_to_min_duration():
    raw = RawSuggestions(
        shorts=[_short(0, 5), _short(60, 100), _short(150, 200)], youtube=None
    )
    result = postprocess_suggestions(raw, duration_sec=300.0)
    first = result.shorts[0]
    assert first.end - first.start == 15.0


def test_postprocess_raises_when_cannot_reach_min_duration():
    # video itself is only 10s long, short can't reach the 15s minimum
    raw = RawSuggestions(shorts=[_short(0, 5), _short(2, 8), _short(1, 9)], youtube=None)
    with pytest.raises(SuggestionValidationError):
        postprocess_suggestions(raw, duration_sec=10.0)


def test_postprocess_raises_on_degenerate_zero_length_range():
    raw = RawSuggestions(shorts=[_short(50, 50), _short(60, 100), _short(150, 200)], youtube=None)
    with pytest.raises(SuggestionValidationError):
        postprocess_suggestions(raw, duration_sec=300.0)


# --------------------------------------------------------------------------
# YouTube plan gating.
# --------------------------------------------------------------------------


def test_postprocess_drops_youtube_plan_for_short_video():
    yt = RawYoutubePlan(title="t", description="d", ranges=[RawKeepRange(start=0, end=100, reason="r")])
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=900.0)  # 15 min < 20 min threshold
    assert result.youtube is None


def test_postprocess_keeps_youtube_plan_for_long_video():
    yt = RawYoutubePlan(
        title="t", description="d",
        ranges=[RawKeepRange(start=0, end=300, reason="r1"), RawKeepRange(start=400, end=700, reason="r2")],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=2400.0)  # 40 min
    assert result.youtube is not None
    assert len(result.youtube.ranges) == 2


def test_postprocess_merges_ranges_with_small_gap():
    yt = RawYoutubePlan(
        title="t", description="d",
        ranges=[RawKeepRange(start=0, end=100, reason="r1"), RawKeepRange(start=100.3, end=200, reason="r2")],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=2400.0)
    assert len(result.youtube.ranges) == 1
    assert result.youtube.ranges[0].start == 0.0
    assert result.youtube.ranges[0].end == 200.0


def test_postprocess_does_not_merge_ranges_with_large_gap():
    yt = RawYoutubePlan(
        title="t", description="d",
        ranges=[RawKeepRange(start=0, end=100, reason="r1"), RawKeepRange(start=105, end=200, reason="r2")],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=2400.0)
    assert len(result.youtube.ranges) == 2


def test_postprocess_drops_zero_length_ranges_after_clamping():
    yt = RawYoutubePlan(
        title="t", description="d",
        ranges=[
            RawKeepRange(start=2400, end=2500, reason="beyond video"),  # fully clamped away
            RawKeepRange(start=0, end=300, reason="valid"),
        ],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=2400.0)
    assert len(result.youtube.ranges) == 1


def test_postprocess_sorts_ranges_by_start():
    yt = RawYoutubePlan(
        title="t", description="d",
        ranges=[
            RawKeepRange(start=500, end=600, reason="second"),
            RawKeepRange(start=0, end=100, reason="first"),
        ],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=2400.0)
    assert [r.start for r in result.youtube.ranges] == [0.0, 500.0]


def test_postprocess_youtube_none_when_ranges_empty_after_clamping():
    yt = RawYoutubePlan(title="t", description="d", ranges=[RawKeepRange(start=3000, end=3100, reason="oob")])
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=yt)
    result = postprocess_suggestions(raw, duration_sec=2400.0)
    assert result.youtube is None
