"""The final render pass: brand logo, burned subtitles, or both.

Pure filtergraph building, so these run without ffmpeg, a font, or a video —
the same shape as the transition filtergraph tests.
"""
from __future__ import annotations

import pytest

from app.export.overlay import (
    DEFAULT_POSITION,
    POSITIONS,
    LogoOverlay,
    OverlayError,
    build_burn_args,
    build_overlay_filter,
)

_HD = {"frame_width": 1080, "frame_height": 1920}


def test_nothing_to_draw_means_no_filter_at_all():
    # An empty graph is what tells the caller to skip the re-encode entirely.
    assert build_overlay_filter(None, subtitles=False, **_HD) == ""


def test_subtitles_alone_do_not_take_a_second_input():
    graph = build_overlay_filter(None, subtitles=True, **_HD)
    assert graph == "[0:v]subtitles=subs.ass[v]"
    assert "[1:v]" not in graph


def test_logo_and_subtitles_ride_one_pass():
    graph = build_overlay_filter(LogoOverlay(), subtitles=True, **_HD)
    assert "overlay=" in graph
    assert "subtitles=subs.ass" in graph
    assert graph.endswith("[v]")


def test_the_subtitle_burn_comes_after_the_logo():
    # Whichever corner the mark sits in, dialogue is what must stay readable.
    graph = build_overlay_filter(LogoOverlay(), subtitles=True, **_HD)
    assert graph.index("overlay=") < graph.index("subtitles=")


def test_sizes_are_resolved_against_the_real_frame():
    # 12% of 1080 is 130px; the same project in landscape must not get the
    # same pixel count.
    portrait = build_overlay_filter(LogoOverlay(width_pct=12.0), subtitles=False, **_HD)
    landscape = build_overlay_filter(
        LogoOverlay(width_pct=12.0), subtitles=False, frame_width=1920, frame_height=1080
    )
    assert "scale=130:-1" in portrait
    assert "scale=230:-1" in landscape


def test_the_logo_keeps_its_own_aspect_ratio():
    assert ":-1," in build_overlay_filter(LogoOverlay(), subtitles=False, **_HD)


@pytest.mark.parametrize(
    ("position", "x_is_left", "y_is_top"),
    [
        ("top-left", True, True),
        ("top-right", False, True),
        ("bottom-left", True, False),
        ("bottom-right", False, False),
    ],
)
def test_every_corner_lands_in_its_corner(position, x_is_left, y_is_top):
    graph = build_overlay_filter(LogoOverlay(position=position), subtitles=False, **_HD)
    x = graph.split("overlay=x=")[1].split(":")[0]
    y = graph.split(":y=")[1].split(":")[0]

    assert (x == "43") is x_is_left, f"{position} put x at {x}"
    assert (y == "43") is y_is_top, f"{position} put y at {y}"
    if not x_is_left:
        assert x.startswith("W-w-")  # measured by ffmpeg, not guessed here
    if not y_is_top:
        assert y.startswith("H-h-")


def test_the_default_corner_is_clear_of_the_subtitles():
    # Subtitles default to the bottom; a bottom logo would sit on them.
    assert DEFAULT_POSITION.startswith("top-")
    assert LogoOverlay().position == DEFAULT_POSITION


def test_opacity_reaches_the_filter():
    assert "aa=0.5000" in build_overlay_filter(
        LogoOverlay(opacity=0.5), subtitles=False, **_HD
    )


def test_an_alpha_less_logo_still_honours_opacity():
    # format=rgba has to come before the alpha mix, or a JPEG logo ignores it.
    graph = build_overlay_filter(LogoOverlay(opacity=0.5), subtitles=False, **_HD)
    assert graph.index("format=rgba") < graph.index("colorchannelmixer")


@pytest.mark.parametrize(
    "logo",
    [
        LogoOverlay(position="middle"),
        LogoOverlay(width_pct=0),
        LogoOverlay(width_pct=140),
        LogoOverlay(opacity=1.5),
        LogoOverlay(margin_pct=60),
    ],
)
def test_a_nonsensical_logo_is_refused_not_rendered(logo):
    with pytest.raises(OverlayError):
        build_overlay_filter(logo, subtitles=False, **_HD)


def test_a_frame_with_no_size_is_refused():
    with pytest.raises(OverlayError):
        build_overlay_filter(LogoOverlay(), subtitles=False, frame_width=0, frame_height=0)


def test_the_position_list_and_the_model_agree():
    from app.models import LogoSettings

    assert LogoSettings().position in POSITIONS


# --------------------------------------------------------------------------
# The argument list. A filter_complex replaces ffmpeg's default stream
# selection, which is how a logo turns an export silent without anything
# failing.
# --------------------------------------------------------------------------


def _args(logo_path="logo.png", subtitles=True):
    graph = build_overlay_filter(
        LogoOverlay() if logo_path else None, subtitles=subtitles, **_HD
    )
    return build_burn_args(
        "joined.mp4",
        logo_path,
        graph,
        encoder_args=["-c:v", "libx264"],
        audio_args=["-c:a", "aac"],
        out_path="out.mp4",
    )


def test_the_audio_is_mapped_or_the_short_comes_out_silent():
    args = _args()
    assert "-map" in args
    assert "0:a?" in args, "no audio map: every export with a logo would be silent"


def test_the_audio_map_tolerates_a_clip_with_no_sound():
    # `0:a` and not `0:a?` fails the whole export on a silent source.
    assert "0:a" not in _args() or "0:a?" in _args()


def test_the_logo_is_the_second_input_the_graph_refers_to():
    args = _args()
    assert args[:4] == ["-i", "joined.mp4", "-i", "logo.png"]
    assert "[1:v]" in args[args.index("-filter_complex") + 1]


def test_without_a_logo_there_is_no_second_input():
    args = _args(logo_path=None)
    assert args.count("-i") == 1
    assert "[1:v]" not in args[args.index("-filter_complex") + 1]


def test_faststart_survives_the_overlay_pass():
    # Without it the moov atom lands at the end and the short will not begin
    # playing until the whole file has downloaded.
    args = _args()
    assert args[-3:] == ["-movflags", "+faststart", "out.mp4"]
