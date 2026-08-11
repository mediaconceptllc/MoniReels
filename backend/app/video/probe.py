"""ffprobe metadata extraction + thumbnail generation."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.utils.logging import get_logger

logger = get_logger(__name__)


class ProbeError(Exception):
    pass


def _parse_fps(rate: str) -> float:
    """ffprobe reports frame rates as a fraction string like '30000/1001'."""
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        den_f = float(den)
        return float(num) / den_f if den_f else 0.0
    return float(rate)


async def probe_video(ffprobe_path: Path, video_path: Path) -> dict:
    """Returns raw fields needed to build a VideoMeta (thumbnail_path filled in by caller)."""
    args = [
        str(ffprobe_path),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed on {video_path}: {stderr.decode(errors='replace')[-500:]}")

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise ProbeError(f"ffprobe returned invalid JSON for {video_path}: {e}") from e

    streams = data.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format", {})

    if video_stream is None:
        raise ProbeError(f"No video stream found in {video_path}")

    duration_raw = fmt.get("duration") or video_stream.get("duration")
    if duration_raw is None:
        raise ProbeError(f"Could not determine duration for {video_path}")

    return {
        "duration_sec": float(duration_raw),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "fps": _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate", "0/0")),
        "has_audio": audio_stream is not None,
        "codec": video_stream.get("codec_name", "unknown"),
    }


async def generate_thumbnail(
    ffmpeg_path: Path, video_path: Path, output_path: Path, at_sec: float = 1.0
) -> None:
    """Extracts a single JPEG frame. Clamps the seek point for very short clips."""
    args = [
        str(ffmpeg_path),
        "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, at_sec):.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not output_path.exists():
        tail = stderr.decode(errors="replace")[-500:]
        raise ProbeError(f"Thumbnail generation failed for {video_path}: {tail}")
