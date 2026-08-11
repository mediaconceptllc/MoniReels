import pytest

from app.export.normalize import build_audio_filter, build_video_filter, validate_portrait_fill


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
