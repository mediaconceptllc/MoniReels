"""Encoder preset tests.

The hardware-encoder branches (NVENC / QSV / AMF) and `choose_encoder` are
gone: there is no GPU on the target platform, so those paths could never be
taken and the "choice" they exposed had no effect. What remains is the one
encoder that runs, plus the pixel-format guard that outlives the branches.
"""
import pytest

from app.export.presets import (
    VIDEO_ENCODER,
    build_audio_encoder_args,
    build_video_encoder_args,
    resolve_dimensions,
)


def test_resolve_dimensions_landscape():
    assert resolve_dimensions("landscape") == (1920, 1080)


def test_resolve_dimensions_portrait():
    assert resolve_dimensions("portrait") == (1080, 1920)


def test_resolve_dimensions_invalid_raises():
    with pytest.raises(ValueError):
        resolve_dimensions("square")


def test_build_video_encoder_args_uses_crf():
    assert build_video_encoder_args(crf=20, preset="medium") == [
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
    ]


def test_encoder_is_software_only():
    """Guards the decision, not just the string: if a hardware encoder is
    ever reintroduced it must come with a capability probe, because "listed"
    and "working" are different things and a listed-but-broken encoder fails
    every render."""
    assert VIDEO_ENCODER == "libx264"


def test_pixel_format_is_forced():
    """Each clip is already yuv420p going in, but the xfade and subtitle-burn
    passes re-encode and can upconvert chroma to yuv444p. That yields a
    High 4:4:4 Predictive stream only permissive players decode — to a user,
    indistinguishable from a broken export."""
    args = build_video_encoder_args(crf=23, preset="fast")
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"


def test_build_audio_encoder_args():
    assert build_audio_encoder_args() == ["-c:a", "aac", "-b:a", "192k"]
