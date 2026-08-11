"""Export presets: target dimensions per orientation, and per-encoder CLI flags.

NVENC/QSV/AMF take different quality/preset flags than libx264 — "listed != working"
is handled at capability-probe time (app.video.capabilities); this module only
handles the flag-name differences once an encoder is known to work.
"""
from __future__ import annotations

LANDSCAPE_SIZE = (1920, 1080)
PORTRAIT_SIZE = (1080, 1920)

AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNEL_LAYOUT = "stereo"
AUDIO_BITRATE = "192k"

# libx264-style preset names -> nearest NVENC preset. NVENC's own preset names
# (p1..p7, or "slow"/"medium"/"fast" on newer builds) don't line up 1:1 with
# libx264, so this is a deliberate, documented approximation.
_NVENC_PRESET_MAP = {
    "ultrafast": "p1",
    "superfast": "p2",
    "veryfast": "p3",
    "faster": "p4",
    "fast": "p4",
    "medium": "p4",
    "slow": "p6",
    "slower": "p7",
    "veryslow": "p7",
}


def resolve_dimensions(orientation: str) -> tuple[int, int]:
    if orientation == "portrait":
        return PORTRAIT_SIZE
    if orientation == "landscape":
        return LANDSCAPE_SIZE
    raise ValueError(f"Unknown orientation: {orientation!r}")


def build_video_encoder_args(encoder: str, crf: int, preset: str) -> list[str]:
    """Returns the -c:v ... quality/preset flags for the given encoder.

    `encoder` must be either "libx264" or a working_hwaccel_encoder value
    already confirmed by app.video.capabilities.probe_hwaccel_encoder — this
    function does not itself validate that the encoder is available.
    """
    if encoder == "h264_nvenc":
        nvenc_preset = _NVENC_PRESET_MAP.get(preset, "p4")
        return ["-c:v", "h264_nvenc", "-cq", str(crf), "-preset", nvenc_preset]
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(crf), "-preset", preset]
    if encoder == "h264_amf":
        return ["-c:v", "h264_amf", "-qp_i", str(crf), "-qp_p", str(crf), "-quality", preset]
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"]


def build_audio_encoder_args() -> list[str]:
    return ["-c:a", "aac", "-b:a", AUDIO_BITRATE]


def choose_encoder(use_hwaccel: bool, working_hwaccel_encoder: str | None) -> str:
    if use_hwaccel and working_hwaccel_encoder:
        return working_hwaccel_encoder
    return "libx264"
