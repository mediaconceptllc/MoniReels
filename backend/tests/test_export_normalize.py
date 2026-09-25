import pytest

from app.export.normalize import (
    build_audio_filter,
    build_video_filter,
    build_voice_mix,
    validate_portrait_fill,
)


def test_build_video_filter_landscape_pads():
    vf = build_video_filter(1920, 1080, 30.0, "landscape", "pad")
    assert "scale=1920:1080:force_original_aspect_ratio=decrease" in vf
    assert "pad=1920:1080" in vf
    assert "fps=30.0" in vf
    assert "format=yuv420p" in vf


def test_build_video_filter_portrait_pad():
    vf = build_video_filter(1080, 1920, 30.0, "portrait", "pad")
    assert "pad=1080:1920" in vf


def test_build_video_filter_portrait_crop():
    vf = build_video_filter(1080, 1920, 30.0, "portrait", "crop")
    assert "force_original_aspect_ratio=increase" in vf
    assert "crop=1080:1920" in vf
    assert "pad" not in vf


def test_build_video_filter_portrait_blur_has_split_and_overlay():
    vf = build_video_filter(1080, 1920, 30.0, "portrait", "blur")
    assert "split=2" in vf
    assert "boxblur" in vf
    assert "overlay" in vf


def test_build_audio_filter():
    af = build_audio_filter(48000, "stereo")
    assert af == "aformat=sample_rates=48000:channel_layouts=stereo"


def test_validate_portrait_fill_accepts_known_values():
    for v in ("blur", "crop", "pad"):
        validate_portrait_fill(v)  # should not raise


def test_validate_portrait_fill_rejects_unknown():
    with pytest.raises(ValueError):
        validate_portrait_fill("stretch")


def test_the_voice_is_summed_over_the_original_kept_at_its_level():
    graph = build_voice_mix("aformat=sample_rates=48000", 0.2, has_audio=True)
    assert "[0:a]aformat=sample_rates=48000,volume=0.200[vo_bed]" in graph
    assert "[1:a]aformat=sample_rates=48000[vo_line]" in graph
    # amix would otherwise halve both: the voice at half level over a
    # fifth of the scene is not what "keep the original at 20%" means.
    assert "normalize=0" in graph and "duration=first" in graph
    assert graph.endswith("[vo_mix]")


def test_the_limiter_neither_lifts_the_mix_nor_moves_the_voice():
    graph = build_voice_mix("anull", 0.2, has_audio=True)
    # Auto-level would raise a quiet mix to full scale; an uncompensated
    # lookahead would shift the voice off where it was placed.
    assert "level=0" in graph and "latency=1" in graph


def test_a_silent_clip_carries_the_voice_alone():
    assert build_voice_mix("anull", 0.2, has_audio=False) == "[1:a]anull[vo_mix]"


def test_under_a_dub_the_source_is_never_in_the_mix():
    """The bed replaces the original: the original still speaks, so no
    level of it belongs under the dub — and the bed plays at its own."""
    graph = build_voice_mix("anull", 0.2, has_audio=True, voice_input=1, bed_input=2)
    assert "[0:a]" not in graph
    assert "[2:a]anull[vo_bed]" in graph and "[1:a]anull[vo_line]" in graph
    assert "volume=" not in graph
    assert "normalize=0" in graph and "level=0" in graph and graph.endswith("[vo_mix]")


def test_a_dubbed_clip_nobody_speaks_in_is_its_bed():
    graph = build_voice_mix("anull", 0.2, has_audio=True, voice_input=None, bed_input=1)
    assert graph == "[1:a]anull[vo_mix]"
