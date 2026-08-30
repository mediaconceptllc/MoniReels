"""Export presets: target dimensions per orientation and encoder flags.

The desktop build chose between libx264 and whichever of NVENC / QSV / AMF
tested working on the user's machine. On Railway there is no GPU, so all
three hardware branches were dead code that only ever offered the operator a
choice with no effect. libx264 is the encoder.

This is a real, measurable cost, not a tidy-up: software H.264 is several
times slower than a hardware encoder, so export wall-clock time and the CPU
bill both rise compared with a desktop run. `crf` and `preset` are therefore
the two levers that matter, and both stay operator-controlled per project.
"""
from __future__ import annotations

LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNEL_LAYOUT = "stereo"
AUDIO_BITRATE = "192k"

VIDEO_ENCODER = "libx264"


def resolve_dimensions(orientation: str) -> tuple[int, int]:
    if orientation == "portrait":
        return PORTRAIT_SIZE
    if orientation == "landscape":
        return LANDSCAPE_SIZE
    raise ValueError(f"Unknown orientation: {orientation!r}")


def build_video_encoder_args(crf: int, preset: str) -> list[str]:
    """Video encoder flags.

    yuv420p is forced even though each clip is already normalized to it going
    in: the xfade and subtitle-burn passes re-encode and can silently upconvert
    chroma (yuv444p has been observed). That produces a High 4:4:4 Predictive
    stream only permissive players (VLC, anything ffmpeg-based) can decode —
    every phone and browser fails on it outright, which to a user is
    indistinguishable from a broken export.
    """
    return ["-c:v", VIDEO_ENCODER, "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"]


def build_audio_encoder_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
