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


def build_voice_mix(
    audio_filter: str,
    original_volume: float,
    has_audio: bool,
    *,
    voice_input: int | None = 1,
    bed_input: int | None = None,
) -> str:
    """The clip's sound with a voice-over on top — input 0 the source, the
    voice track at `voice_input`, the result at `[vo_mix]`.

    The original stays under the voice at `original_volume` for the whole
    clip rather than ducking only while a line plays: between two Mongolian
    lines the original would come back up in the source language, which
    reads as a gap in the dubbing, not as ambience.

    With a `bed_input` — the original with its speech removed
    (app.audio.dub_bed) — the bed takes the original's place, at its own
    level: nothing in it speaks, so there is nothing to lower, and the music
    and effects play as loud as they did under the original speech. A clip
    nobody speaks in has a bed and no voice.

    Summed without normalising (amix would otherwise halve both) and then
    limited, so a loud line over a loud scene never clips. The limiter's own
    auto-level is off — it would lift a quiet mix to full scale — and its
    lookahead is compensated, so the voice stays where it was placed.
    """
    mix = (
        "[vo_bed][vo_line]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95:level=0:latency=1[vo_mix]"
    )
    if bed_input is not None:
        bed = f"[{bed_input}:a]{audio_filter}"
        if voice_input is None:
            return f"{bed}[vo_mix]"
        return f"{bed}[vo_bed];[{voice_input}:a]{audio_filter}[vo_line];{mix}"
    voice = f"[{voice_input}:a]{audio_filter}"
    if not has_audio:
        return f"{voice}[vo_mix]"
    return (
        f"[0:a]{audio_filter},volume={original_volume:.3f}[vo_bed];"
        f"{voice}[vo_line];"
        f"{mix}"
    )


def validate_portrait_fill(value: str) -> None:
    if value not in VALID_PORTRAIT_FILLS:
        raise ValueError(f"Invalid portrait_fill: {value!r}, must be one of {VALID_PORTRAIT_FILLS}")
