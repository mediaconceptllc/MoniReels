"""Post-validation is what the spec explicitly asks to unit-test: canned LLM
responses covering "only 2 shorts returned", cuts resolving to real
timestamps via a segment-index lookup, and out-of-bounds duration/role rules.
"""
from __future__ import annotations

import pytest

from app.ai.schema import (
    MAX_SHORT_COUNT,
    MAX_YOUTUBE_COUNT,
    MIN_SHORT_DURATION,
    RawCut,
    RawKeepRange,
    RawShort,
    RawSuggestions,
    RawYoutubePlan,
    SuggestionValidationError,
    _build_short,
    count_limits,
    default_counts,
    postprocess_suggestions,
    validate_llm_output,
)

# 1-second-per-index segments, so a cut spanning indices [a, b] resolves to
# exactly (b - a + 1) seconds - keeps every duration in these tests easy to
# reason about by hand.
_SEGMENTS = [(float(i), float(i + 1), f"word{i}") for i in range(300)]


def _raw_cut(start_index: int, end_index: int, role: str = "context", reason: str = "r") -> RawCut:
    return RawCut(start_index=start_index, end_index=end_index, role=role, reason=reason)


def _raw_short(title: str, cuts: list[RawCut]) -> RawShort:
    return RawShort(
        title=title, hook_text="hook text", hook_quote="quote", cuts=cuts,
        on_screen_texts=[], b_roll=[], caption="caption", hashtags=[], why_it_works="because",
    )


def _valid_cuts(offset: int = 0) -> list[RawCut]:
    """hook(10s) + context(15s) + payoff(15s) = 40s total - inside the 35-60s window."""
    return [
        _raw_cut(offset, offset + 9, role="hook"),
        _raw_cut(offset + 10, offset + 24, role="context"),
        _raw_cut(offset + 25, offset + 39, role="payoff"),
    ]


def _three_valid_shorts() -> list[RawShort]:
    return [_raw_short("A", _valid_cuts(0)), *_filler_shorts()]


def _filler_shorts() -> list[RawShort]:
    """Two more valid shorts, for a test that only cares about how the first
    one is processed.
    """
    return [_raw_short("B", _valid_cuts(50)), _raw_short("C", _valid_cuts(100))]


def _raw_keep_range(start_index: int, end_index: int) -> RawKeepRange:
    return RawKeepRange(start_index=start_index, end_index=end_index)


def _three_youtube_plans(*plans: RawYoutubePlan) -> list[RawYoutubePlan]:
    """Pad a single plan-under-test up to 3 with trivial filler plans, for a
    test that only cares about one plan's processing.
    """
    filler = RawYoutubePlan(title="filler", throughline="d", keep_ranges=[_raw_keep_range(0, 9)])
    result = list(plans)
    while len(result) < 3:
        result.append(filler)
    return result


# --------------------------------------------------------------------------
# Layer 1: schema validation.
# --------------------------------------------------------------------------


def test_validate_llm_output_valid():
    raw = validate_llm_output({"shorts": [_raw_short("A", _valid_cuts()).model_dump()], "youtube": []})
    assert len(raw.shorts) == 1


def test_validate_llm_output_missing_field_raises():
    with pytest.raises(SuggestionValidationError):
        validate_llm_output({"shorts": [{"title": "x"}], "youtube": []})


def test_validate_llm_output_wrong_type_raises():
    with pytest.raises(SuggestionValidationError):
        validate_llm_output({"shorts": "not a list", "youtube": []})


def test_validate_llm_output_youtube_null_raises():
    # v2 contract: youtube is always a list, never null - the model must emit [].
    with pytest.raises(SuggestionValidationError):
        validate_llm_output({"shorts": [_raw_short("A", _valid_cuts()).model_dump()], "youtube": None})


# --------------------------------------------------------------------------
# How many come back. The count is the producer's; the answer can hold fewer
# (never padded) and is recorded beside what was asked, so a shortfall is SAID.
# --------------------------------------------------------------------------


def test_fewer_shorts_than_asked_are_kept_and_the_ask_is_recorded():
    """Two good shorts used to fail the whole job ("exactly 3"), billing the
    producer for an answer that had two usable ideas in it."""
    raw = RawSuggestions(shorts=[_raw_short("A", _valid_cuts(0)), _raw_short("B", _valid_cuts(50))])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0, shorts=3, youtube=0)
    assert [s.title for s in result.shorts] == ["A", "B"]
    assert result.requested_shorts == 3


def test_more_shorts_than_asked_are_trimmed_to_the_ask():
    raw = RawSuggestions(shorts=[*_three_valid_shorts(), _raw_short("D", _valid_cuts(150))])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0, shorts=3, youtube=0)
    assert [s.title for s in result.shorts] == ["A", "B", "C"]


def test_a_short_that_breaks_a_rule_is_dropped_and_the_rest_are_kept():
    """One bad short used to fail the job and throw the good ones away with
    it. At eight, the odds of every short surviving validation fall fast; the
    producer would be billed for nothing instead of shown seven ideas."""
    bad = _raw_short("bad", [_raw_cut(0, 1, role="hook"), _raw_cut(2, 3), _raw_cut(4, 5, role="payoff")])
    raw = RawSuggestions(shorts=[bad, *_filler_shorts()])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0, shorts=3, youtube=0)
    assert [s.title for s in result.shorts] == ["B", "C"]
    assert result.requested_shorts == 3


def test_a_spare_stands_in_for_a_dropped_short():
    """When the model returned one more than asked, the extra is used before
    the answer is reported short."""
    bad = _raw_short("bad", [_raw_cut(0, 1, role="hook"), _raw_cut(2, 3), _raw_cut(4, 5, role="payoff")])
    raw = RawSuggestions(shorts=[bad, *_filler_shorts(), _raw_short("D", _valid_cuts(150))])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0, shorts=3, youtube=0)
    assert [s.title for s in result.shorts] == ["B", "C", "D"]


def test_an_answer_with_no_usable_short_is_still_a_failure():
    """The one case with nothing to show for the bill."""
    bad = _raw_short("bad", [_raw_cut(0, 1, role="hook"), _raw_cut(2, 3), _raw_cut(4, 5, role="payoff")])
    raw = RawSuggestions(shorts=[bad])
    with pytest.raises(SuggestionValidationError, match="usable"):
        postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0, shorts=3, youtube=0)


# --------------------------------------------------------------------------
# The range a video can be asked for.
# --------------------------------------------------------------------------


def test_the_shorts_limit_is_the_arithmetic_of_the_minimum_short():
    """Each short is at least MIN_SHORT_DURATION on a DIFFERENT story, so a
    video holds at most duration / MIN_SHORT_DURATION of them. Derived, not
    invented, so it moves if the rule does."""
    assert count_limits(MIN_SHORT_DURATION * 4)[0] == 4
    assert count_limits(MIN_SHORT_DURATION * 4 - 0.1)[0] == 3


def test_the_shorts_limit_has_a_ceiling_and_a_floor():
    assert count_limits(10_000.0)[0] == MAX_SHORT_COUNT
    # Even a clip too short for a real short can be asked for one: the model
    # is the one that says it cannot, and a limit of zero would hide the button.
    assert count_limits(20.0)[0] == 1


def test_youtube_plans_are_offered_only_above_twenty_minutes():
    assert count_limits(1200.0)[1] == 0
    assert count_limits(1200.1)[1] == MAX_YOUTUBE_COUNT


def test_an_omitted_choice_keeps_the_old_three_wherever_the_video_can_hold_them():
    """A client that predates choosing must behave exactly as before on the
    videos this product is for; below three minimum-length shorts the old
    three asked for stories the source does not have."""
    assert default_counts(MIN_SHORT_DURATION * 3) == (3, 0)
    assert default_counts(3600.0) == (3, 3)
    assert default_counts(MIN_SHORT_DURATION * 2) == (2, 0)


# --------------------------------------------------------------------------
# Index resolution -> real timestamps.
# --------------------------------------------------------------------------


def test_postprocess_resolves_cut_indices_to_exact_segment_timestamps():
    raw = RawSuggestions(shorts=_three_valid_shorts())
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0)
    first_cut = result.shorts[0].cuts[0]
    assert first_cut.start == 0.0  # clamped to 0 - CUT_PAD_SEC would go negative
    assert first_cut.end == 10.2  # segments[9].end + CUT_PAD_SEC
    assert first_cut.role == "hook"


def test_postprocess_clamps_out_of_range_end_index_instead_of_failing():
    """The model referencing an index past the last real segment is a
    near-miss, not grounds to fail the whole short - it clamps to the
    nearest valid segment instead of raising.
    """
    cuts = [
        _raw_cut(0, 9, role="hook"),
        _raw_cut(10, 34, role="context"),
        _raw_cut(295, 305, role="payoff"),  # 305 is past the last valid index (299)
    ]
    raw = RawSuggestions(shorts=[_raw_short("A", cuts), *_filler_shorts()])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=310.0)
    last_cut = result.shorts[0].cuts[-1]
    assert last_cut.end == 300.2  # clamped to segments[299].end, then + CUT_PAD_SEC


def test_postprocess_unknown_role_falls_back_to_context():
    cuts = [
        _raw_cut(0, 9, role="hook"),
        _raw_cut(10, 24, role="nonsense-role"),
        _raw_cut(25, 39, role="payoff"),
    ]
    raw = RawSuggestions(shorts=[_raw_short("A", cuts), *_filler_shorts()])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=300.0)
    assert result.shorts[0].cuts[1].role == "context"


# --------------------------------------------------------------------------
# Cut-count / duration / role-ordering rules - the model gets no free pass
# on these; a violation raises rather than getting mechanically "fixed"
# (unlike the old single-range shape, a multi-cut short can't be safely
# auto-resized without risking a cut mid-sentence).
# --------------------------------------------------------------------------


def test_a_short_is_refused_when_fewer_than_three_cuts():
    cuts = [_raw_cut(0, 9, role="hook"), _raw_cut(10, 39, role="payoff")]
    with pytest.raises(SuggestionValidationError, match="cuts"):
        _build_short(_raw_short("A", cuts), _SEGMENTS, 300.0)


def test_a_short_is_refused_when_more_than_five_cuts():
    cuts = [_raw_cut(i * 5, i * 5 + 4, role="context") for i in range(6)]
    cuts[0] = _raw_cut(0, 4, role="hook")
    cuts[-1] = _raw_cut(25, 29, role="payoff")
    with pytest.raises(SuggestionValidationError, match="cuts"):
        _build_short(_raw_short("A", cuts), _SEGMENTS, 300.0)


def test_a_short_is_refused_when_total_duration_too_short():
    # 6s total (2s hook + 2s context + 2s payoff)
    cuts = [_raw_cut(0, 1, role="hook"), _raw_cut(2, 3, role="context"), _raw_cut(4, 5, role="payoff")]
    with pytest.raises(SuggestionValidationError, match="duration"):
        _build_short(_raw_short("A", cuts), _SEGMENTS, 300.0)


def test_a_short_is_refused_when_total_duration_too_long():
    # 90s total (30s hook + 30s context + 30s payoff)
    cuts = [_raw_cut(0, 29, role="hook"), _raw_cut(30, 59, role="context"), _raw_cut(60, 89, role="payoff")]
    with pytest.raises(SuggestionValidationError, match="duration"):
        _build_short(_raw_short("A", cuts), _SEGMENTS, 300.0)


def test_a_short_is_refused_when_last_cut_is_not_payoff():
    cuts = [_raw_cut(0, 9, role="hook"), _raw_cut(10, 24, role="context"), _raw_cut(25, 39, role="proof")]
    with pytest.raises(SuggestionValidationError, match="payoff"):
        _build_short(_raw_short("A", cuts), _SEGMENTS, 300.0)


def test_a_short_is_refused_when_no_hook_cut():
    cuts = [_raw_cut(0, 9, role="context"), _raw_cut(10, 24, role="proof"), _raw_cut(25, 39, role="payoff")]
    with pytest.raises(SuggestionValidationError, match="hook"):
        _build_short(_raw_short("A", cuts), _SEGMENTS, 300.0)


# --------------------------------------------------------------------------
# YouTube plan gating - up to the number asked for when duration_sec is above
# the threshold, [] otherwise.
# --------------------------------------------------------------------------


def test_postprocess_forces_empty_youtube_list_for_short_video():
    yt = RawYoutubePlan(title="t", throughline="d", keep_ranges=[_raw_keep_range(0, 99)])
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=_three_youtube_plans(yt))
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=900.0)  # 15 min < 20 min threshold
    assert result.youtube == []


def test_fewer_plans_than_asked_never_cost_the_shorts():
    """Two plans for three asked used to fail the job — shorts and all."""
    yt = RawYoutubePlan(title="t", throughline="d", keep_ranges=[_raw_keep_range(0, 99)])
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=[yt, yt])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=2400.0, shorts=3, youtube=3)
    assert len(result.shorts) == 3
    assert len(result.youtube) == 2
    assert result.requested_youtube == 3


def test_a_long_video_asked_for_no_plans_keeps_none():
    yt = RawYoutubePlan(title="t", throughline="d", keep_ranges=[_raw_keep_range(0, 99)])
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=_three_youtube_plans(yt))
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=2400.0, shorts=3, youtube=0)
    assert result.youtube == []
    assert result.requested_youtube == 0


def test_a_short_video_records_that_no_plans_were_asked_for():
    """Whatever the request said, a video under the gate was not asked for
    plans — and the record must say what the MODEL was asked, or a missing
    plan would read as a shortfall."""
    raw = RawSuggestions(shorts=_three_valid_shorts())
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=900.0, shorts=3, youtube=3)
    assert result.requested_youtube == 0


def test_postprocess_keeps_three_independent_youtube_plans_for_long_video():
    yt1 = RawYoutubePlan(
        title="A", throughline="d", keep_ranges=[_raw_keep_range(0, 99), _raw_keep_range(150, 249)]
    )
    yt2 = RawYoutubePlan(title="B", throughline="d", keep_ranges=[_raw_keep_range(0, 50)])
    yt3 = RawYoutubePlan(title="C", throughline="d", keep_ranges=[_raw_keep_range(0, 50)])
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=[yt1, yt2, yt3])
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=2400.0)
    assert len(result.youtube) == 3
    assert [p.title for p in result.youtube] == ["A", "B", "C"]
    assert len(result.youtube[0].ranges) == 2


def test_postprocess_merges_ranges_with_small_gap():
    ranges = [_raw_keep_range(0, 99), _raw_keep_range(100, 199)]
    yt = RawYoutubePlan(title="t", throughline="d", keep_ranges=ranges)
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=_three_youtube_plans(yt))
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=2400.0)
    # segments are contiguous (index 99 ends at 100.0, index 100 starts at
    # 100.0) - zero gap, so the two ranges merge into one.
    assert len(result.youtube[0].ranges) == 1
    assert result.youtube[0].ranges[0].start == 0.0
    assert result.youtube[0].ranges[0].end == 200.0


def test_postprocess_sorts_ranges_by_start():
    yt = RawYoutubePlan(
        title="t", throughline="d",
        keep_ranges=[_raw_keep_range(200, 209), _raw_keep_range(0, 9)],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=_three_youtube_plans(yt))
    result = postprocess_suggestions(raw, _SEGMENTS, duration_sec=2400.0)
    assert [r.start for r in result.youtube[0].ranges] == [0.0, 200.0]


def test_postprocess_drops_zero_length_ranges_after_clamping():
    beyond_segments = [(0.0, 0.0, "")]  # a degenerate segment - end == start
    yt = RawYoutubePlan(
        title="t", throughline="d",
        keep_ranges=[_raw_keep_range(0, 0), _raw_keep_range(0, 99)],
    )
    raw = RawSuggestions(shorts=_three_valid_shorts(), youtube=_three_youtube_plans(yt))
    segments = beyond_segments + _SEGMENTS[1:]
    result = postprocess_suggestions(raw, segments, duration_sec=2400.0)
    # the [0,0] range resolves to a zero-length span against the degenerate
    # first segment and is dropped; the [0,99] range still resolves normally
    # since it spans past index 0 too.
    assert len(result.youtube[0].ranges) == 1
