"""What the container's FFmpeg can do.

Reduced to the one probe that still means something on a server: which xfade
transition names this build supports, since the frontend offers them and a
name the binary does not know fails the render.

Two probes were dropped outright:

* Hardware-encoder test encodes — there is no GPU, so the answer is always
  "none" and the probe just spent 15 seconds per candidate at startup.
* The `C:/Windows/Fonts` scan — fonts now come from the image, and the ones
  that matter are the ones with Cyrillic coverage (see the Dockerfile).
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.logging import get_logger
from app.video.ffmpeg import get_ffmpeg_version

logger = get_logger(__name__)

_XFADE_LINE_RE = re.compile(r"^ {5}([a-z][a-z0-9_]*)\s+-?\d+\s")


@dataclass
class Capabilities:
    ffmpeg_available: bool = False
    ffmpeg_version: str = ""
    xfade_transitions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ffmpeg_available": self.ffmpeg_available,
            "ffmpeg_version": self.ffmpeg_version,
            "xfade_transitions": self.xfade_transitions,
        }


async def _run(ffmpeg_path: Path, args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        str(ffmpeg_path), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace")


async def probe_xfade_transitions(ffmpeg_path: Path) -> list[str]:
    text = await _run(ffmpeg_path, ["-hide_banner", "-h", "filter=xfade"])
    names: list[str] = []
    in_transition_block = False
    for line in text.splitlines():
        if "set cross fade transition" in line:
            in_transition_block = True
            continue
        if in_transition_block:
            m = _XFADE_LINE_RE.match(line)
            if m:
                if m.group(1) != "custom":
                    names.append(m.group(1))
            elif line.strip() == "" or not line.startswith("     "):
                in_transition_block = False
    if not names:
        logger.warning("Could not parse the xfade transition list from ffmpeg")
    return names


async def build_capabilities(ffmpeg_path: Path | None) -> Capabilities:
    if ffmpeg_path is None:
        return Capabilities(ffmpeg_available=False)
    return Capabilities(
        ffmpeg_available=True,
        ffmpeg_version=await get_ffmpeg_version(ffmpeg_path),
        xfade_transitions=await probe_xfade_transitions(ffmpeg_path),
    )
