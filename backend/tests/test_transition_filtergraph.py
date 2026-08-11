import pytest

from app.transition.filtergraph import build_xfade_filtergraph, plan_transition


def test_plan_transition_offset_math_two_clips():
    # d = [10, 8], T = 1 -> offset[0] = 10 - 1*1 = 9; final = 18 - 1 = 17
    plan = plan_transition([10.0, 8.0], requested_duration=1.0)
    assert plan.offsets == [9.0]
    assert plan.final_duration == 17.0
    assert plan.reduced is False


def test_plan_transition_offset_math_three_clips():
    # d = [10, 8, 6], T = 1
    # offset[0] = 10 - 1 = 9
    # offset[1] = (10+8) - 2 = 16
    # final = 24 - 2*1 = 22
    plan = plan_transition([10.0, 8.0, 6.0], requested_duration=1.0)
    assert plan.offsets == [9.0, 16.0]
    assert plan.final_duration == 22.0


def test_plan_transition_reduces_duration_when_too_long():
    # shortest clip is 2s -> max allowed T is 1.0 (exclusive), request 1.5 should reduce
    plan = plan_transition([10.0, 2.0, 10.0], requested_duration=1.5)
    assert plan.reduced is True
    assert plan.duration < 1.0


def test_plan_transition_requires_at_least_two_clips():
    with pytest.raises(ValueError):
        plan_transition([10.0], requested_duration=1.0)


def test_build_xfade_filtergraph_two_clips_contains_expected_labels():
    plan = plan_transition([10.0, 8.0], requested_duration=1.0)
    filter_complex, v_out, a_out = build_xfade_filtergraph(2, "fade", plan)
    assert v_out == "vout"
    assert a_out == "aout"
    assert "[0:v][1:v]xfade=transition=fade:duration=1.000:offset=9.000[vout]" in filter_complex
    assert "[0:a][1:a]acrossfade=d=1.000:c1=tri:c2=tri[aout]" in filter_complex


def test_build_xfade_filtergraph_three_clips_chains_correctly():
    plan = plan_transition([10.0, 8.0, 6.0], requested_duration=1.0)
    filter_complex, v_out, a_out = build_xfade_filtergraph(3, "dissolve", plan)
    parts = filter_complex.split(";")
    assert len(parts) == 4  # 2 video xfades + 2 audio acrossfades
    assert "[0:v][1:v]xfade=transition=dissolve:duration=1.000:offset=9.000[v1]" in parts
    assert "[v1][2:v]xfade=transition=dissolve:duration=1.000:offset=16.000[vout]" in parts
    assert "[0:a][1:a]acrossfade=d=1.000:c1=tri:c2=tri[a1]" in parts
    assert "[a1][2:a]acrossfade=d=1.000:c1=tri:c2=tri[aout]" in parts


def test_build_xfade_filtergraph_requires_at_least_two_clips():
    with pytest.raises(ValueError):
        build_xfade_filtergraph(1, "fade", plan_transition([10.0, 8.0], 1.0))


@pytest.mark.parametrize(
    "durations,transition_sec",
    [
        ([10.0, 10.0], 0.5),
        ([10.0, 10.0, 10.0], 1.0),
        ([5.0, 5.0, 5.0, 5.0], 0.25),
    ],
)
def test_plan_transition_final_duration_matches_formula(durations, transition_sec):
    plan = plan_transition(durations, transition_sec)
    expected = sum(durations) - (len(durations) - 1) * plan.duration
    assert plan.final_duration == pytest.approx(expected)
