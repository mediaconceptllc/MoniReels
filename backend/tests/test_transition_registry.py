from app.transition.registry import REGISTRY, available_transitions, resolve


def test_resolve_uses_primary_when_supported():
    all_names = [t.xfade_name for t in REGISTRY]
    assert resolve("Fade", all_names) == "fadeblack"


def test_resolve_falls_back_when_primary_unsupported():
    # fadeblack missing -> falls back to "fade"
    supported = [t.xfade_name for t in REGISTRY if t.xfade_name != "fadeblack"]
    assert resolve("Fade", supported) == "fade"


def test_resolve_falls_back_to_fade_when_nothing_supported():
    assert resolve("Zoom", ["fade"]) == "fade"
    assert resolve("Wipe Left", ["fade"]) == "fade"


def test_resolve_unknown_transition_raises():
    try:
        resolve("Not A Real Transition", ["fade"])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_available_transitions_flags_unsupported():
    supported = ["fade"]  # only cross-fade available
    result = available_transitions(supported)
    by_name = {r["ui_name"]: r for r in result}
    assert by_name["Cross Fade"]["supported"] is True
    assert by_name["Fade"]["supported"] is True  # falls back to "fade"
    assert by_name["Zoom"]["supported"] is True  # falls back to "fade"
    assert len(result) == len(REGISTRY)


def test_available_transitions_none_supported():
    result = available_transitions([])
    assert all(r["supported"] is False for r in result)
