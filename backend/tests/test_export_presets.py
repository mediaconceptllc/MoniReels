"""Encoder preset tests.

The hardware-encoder branches (NVENC / QSV / AMF) and `choose_encoder` are
gone: there is no GPU on the target platform, so those paths could never be
taken and the "choice" they exposed had no effect. What remains is the one
encoder that runs, plus the pixel-format guard that outlives the branches.
"""
import pytest

from app.export import presets
from app.export.presets import (
    VIDEO_ENCODER,
    build_audio_encoder_args,
    build_video_encoder_args,
    decoder_thread_args,
    filter_thread_args,
    resolve_dimensions,
)


@pytest.fixture
def cores(monkeypatch):
    """What config.heavy_threads reports: the container's quota, less the
    core left to the web process."""
    def set_to(n: int) -> None:
        monkeypatch.setattr(presets, "heavy_threads", lambda: n)
    return set_to


def test_resolve_dimensions_landscape():
    assert resolve_dimensions("landscape") == (1920, 1080)


def test_resolve_dimensions_portrait():
    assert resolve_dimensions("portrait") == (1080, 1920)


def test_resolve_dimensions_invalid_raises():
    with pytest.raises(ValueError):
        resolve_dimensions("square")


def test_build_video_encoder_args_uses_crf(cores):
    cores(23)
    assert build_video_encoder_args(crf=20, preset="medium") == [
        "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p", "-threads", "8",
    ]


@pytest.mark.parametrize(
    ("quota", "encoder", "one_input", "many_inputs"),
    [
        (63, "8", "4", "2"),  # a large host: the caps hold, whatever ffmpeg can see
        (23, "8", "4", "2"),  # the production worker's 24-vCPU quota
        (3, "3", "3", "2"),  # a small container gets its own quota and no more
        (1, "1", "1", "1"),
    ],
)
def test_every_thread_count_is_set_and_bounded_by_the_quota(cores, quota, encoder, one_input, many_inputs):
    """Left unset, a decoder and x264 each start threads per core they can
    SEE — the host's. MEASURED at the audit: one join of 28 clips, 9.07 GB,
    the worker's production peak. The count has to reach the command line:
    a cap held anywhere else limits nothing."""
    cores(quota)
    args = build_video_encoder_args(crf=20, preset="medium")
    assert args[args.index("-threads") + 1] == encoder
    assert decoder_thread_args() == ["-threads", one_input]
    assert decoder_thread_args(inputs=6) == ["-threads", many_inputs]
    assert filter_thread_args() == ["-filter_threads", encoder, "-filter_complex_threads", encoder]


def test_encoder_is_software_only():
    """Guards the decision, not just the string: if a hardware encoder is
    ever reintroduced it must come with a capability probe, because "listed"
    and "working" are different things and a listed-but-broken encoder fails
    every render."""
    assert VIDEO_ENCODER == "libx264"


def test_pixel_format_is_forced(cores):
    """Each clip is already yuv420p going in, but the xfade and subtitle-burn
    passes re-encode and can upconvert chroma to yuv444p. That yields a
    High 4:4:4 Predictive stream only permissive players decode — to a user,
    indistinguishable from a broken export."""
    cores(4)
    args = build_video_encoder_args(crf=23, preset="fast")
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"


def test_build_audio_encoder_args():
    assert build_audio_encoder_args() == ["-c:a", "aac", "-b:a", "192k"]
