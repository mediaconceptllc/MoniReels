"""Filtergraph for the final pass: brand logo, burned subtitles, or both.

One pass, not two. Both draw on pixels, so both force a re-encode; doing them
separately pays that cost twice and stacks two generations of compression loss
on the part of the frame the logo covers. The overlay itself adds no
measurable time next to the encode.

Pure string building, so the graph is testable without a video, a font, or
ffmpeg on PATH — the same reason app.transition.filtergraph is shaped this
way. Nothing here runs a process.
"""
from __future__ import annotations

from dataclasses import dataclass

# Corners only. "Somewhere in the middle" is not a brand mark, it is a
# watermark across the face the short is about.
POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")

# A TOP corner by default: subtitles sit at the bottom (SubtitleStyle.position
# defaults to "bottom"), and a bottom logo lands on the dialogue.
DEFAULT_POSITION = "top-right"


class OverlayError(ValueError):
    pass


@dataclass(frozen=True)
class LogoOverlay:
    """A logo about to be drawn.

    Sizes are percentages of the FRAME, never pixels: the same project renders
    portrait and landscape, and a logo fixed at 160px is a twelfth of the width
    on one and a quarter of it on the other.
    """

    position: str = DEFAULT_POSITION
    width_pct: float = 12.0
    opacity: float = 0.85
    margin_pct: float = 4.0


def _validate(logo: LogoOverlay) -> None:
    if logo.position not in POSITIONS:
        raise OverlayError(f"Unknown logo position {logo.position!r}; expected one of {POSITIONS}")
    if not 0 < logo.width_pct <= 100:
        raise OverlayError(f"Logo width must be a percentage above 0, got {logo.width_pct}")
    if not 0 <= logo.opacity <= 1:
        raise OverlayError(f"Logo opacity must be between 0 and 1, got {logo.opacity}")
    if not 0 <= logo.margin_pct < 50:
        raise OverlayError(f"Logo margin must be a percentage under 50, got {logo.margin_pct}")


def build_overlay_filter(
    logo: LogoOverlay | None, *, subtitles: bool, frame_width: int, frame_height: int
) -> str:
    """The `-filter_complex` value, or "" when there is nothing to draw.

    Pixels are resolved HERE rather than by ffmpeg expressions: the output
    dimensions are already fixed by the time this runs, so `scale2ref` and its
    aspect-ratio pitfalls (`mdar` is the MAIN video's ratio, not the logo's)
    buy nothing over arithmetic we can see in a test.

    The subtitle burn goes LAST so the logo cannot cover a line of dialogue.
    Whichever corner the mark sits in, the text is what a viewer must be able
    to read.
    """
    if logo is None and not subtitles:
        return ""
    if logo is None:
        return "[0:v]subtitles=subs.ass[v]"

    _validate(logo)
    if frame_width <= 0 or frame_height <= 0:
        raise OverlayError(f"Frame must have a positive size, got {frame_width}x{frame_height}")

    logo_w = max(1, round(frame_width * logo.width_pct / 100))
    margin = max(0, round(frame_width * logo.margin_pct / 100))
    x = str(margin) if logo.position.endswith("-left") else f"W-w-{margin}"
    y = str(margin) if logo.position.startswith("top-") else f"H-h-{margin}"

    # format=rgba before the alpha mix so a logo saved without a channel still
    # honours the opacity, and -1 keeps its own aspect ratio. The overlay
    # input is never encoded on its own, so an odd height here costs nothing.
    chain = (
        f"[1:v]format=rgba,scale={logo_w}:-1,"
        f"colorchannelmixer=aa={logo.opacity:.4f}[lg];"
        f"[0:v][lg]overlay=x={x}:y={y}:format=auto"
    )
    if subtitles:
        return chain + "[ov];[ov]subtitles=subs.ass[v]"
    return chain + "[v]"


def build_burn_args(
    input_path: str,
    logo_path: str | None,
    graph: str,
    *,
    encoder_args: list[str],
    audio_args: list[str],
    out_path: str,
) -> list[str]:
    """The full ffmpeg argument list for the overlay pass.

    Separate from running it so the one thing that is easy to get silently
    wrong can be asserted: a `-filter_complex` REPLACES ffmpeg's default
    stream selection, so without an explicit audio map every export carrying
    a logo comes out silent — a failure nobody sees until someone plays the
    finished short. `0:a?` and not `0:a`, because a clip with no audio track
    must not fail the export outright.
    """
    args = ["-i", input_path]
    if logo_path is not None:
        args += ["-i", logo_path]
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?"]
    args += encoder_args
    args += audio_args
    args += ["-movflags", "+faststart", out_path]
    return args
