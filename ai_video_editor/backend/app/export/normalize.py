"""Pure filtergraph string builders for per-clip normalization (scale/pad/crop/blur,
format, audio format). Kept side-effect-free so the filter strings are unit-testable
without running ffmpeg.
"""
from __future__ import annotations

VALID_PORTRAIT_FILLS = ("blur", "crop", "pad")


def build_video_filter(width: int, height: int, fps: float, orientation: str, portrait_fill: str) -> str:
    """Builds the -vf chain that normalizes one clip to the target canvas.

    Landscape (and portrait "pad") letterbox/pillarbox onto a black canvas.
    Portrait "crop" fills the canvas by cropping the overflow.
    Portrait "blur" fills the canvas with a blurred, cropped copy of the frame
    as background, with the original frame letterboxed on top.
    """
    tail = f"setsar=1,fps={fps},format=yuv420p"

    if orientation == "portrait" and portrait_fill == "crop":
        return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},{tail}"

    if orientation == "portrait" and portrait_fill == "blur":
        return (
            f"split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2[bgblur];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgfit];"
            f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2,{tail}"
        )

    # landscape, or portrait "pad": letterbox/pillarbox onto a black canvas
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,{tail}"
    )


def build_audio_filter(sample_rate: int, channel_layout: str) -> str:
    return f"aformat=sample_rates={sample_rate}:channel_layouts={channel_layout}"


def validate_portrait_fill(value: str) -> None:
    if value not in VALID_PORTRAIT_FILLS:
        raise ValueError(f"Invalid portrait_fill: {value!r}, must be one of {VALID_PORTRAIT_FILLS}")
