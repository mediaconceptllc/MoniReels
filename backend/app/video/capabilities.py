"""Probes what the installed FFmpeg/host machine can actually do.

Everything here is probed once at startup and cached — probing (especially the
hwaccel test-encodes) is too slow to redo per request.
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

# Preference order for hardware encoders; libx264 is the always-available fallback.
_HWACCEL_CANDIDATES = ["h264_nvenc", "h264_qsv", "h264_amf"]


@dataclass
class Capabilities:
    ffmpeg_available: bool = False
    ffmpeg_version: str = ""
    xfade_transitions: list[str] = field(default_factory=list)
    encoders_listed: list[str] = field(default_factory=list)
    working_hwaccel_encoder: str | None = None  # None => use libx264
    fonts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ffmpeg_available": self.ffmpeg_available,
            "ffmpeg_version": self.ffmpeg_version,
            "xfade_transitions": self.xfade_transitions,
            "encoders_listed": self.encoders_listed,
            "working_hwaccel_encoder": self.working_hwaccel_encoder,
            "fonts": self.fonts,
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
        logger.warning("Could not parse xfade transition list from ffmpeg -h filter=xfade")
    return names


async def probe_listed_encoders(ffmpeg_path: Path) -> list[str]:
    text = await _run(ffmpeg_path, ["-hide_banner", "-encoders"])
    listed = []
    for line in text.splitlines():
        m = re.match(r"^\s*[VAS.][F.][S.][X.][B.][D.]\s+(\S+)\s", line)
        if m:
            listed.append(m.group(1))
    return listed


async def probe_hwaccel_encoder(ffmpeg_path: Path, listed_encoders: list[str]) -> str | None:
    """Listed != working: actually try a 1s testsrc encode for each hw candidate, in order."""
    for encoder in _HWACCEL_CANDIDATES:
        if encoder not in listed_encoders:
            continue
        args = [
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
            "-c:v", encoder,
        ]
        if encoder == "h264_nvenc":
            args += ["-cq", "23"]
        else:
            args += ["-b:v", "500k"]
        args += ["-f", "null", "-"]
        try:
            proc = await asyncio.create_subprocess_exec(
                str(ffmpeg_path), *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode == 0:
                logger.info("Hardware encoder probe succeeded: %s", encoder)
                return encoder
            tail = stderr.decode(errors="replace")[-300:]
            logger.info("Hardware encoder probe failed for %s: %s", encoder, tail)
        except (TimeoutError, OSError) as e:
            logger.info("Hardware encoder probe error for %s: %s", encoder, e)
    return None


def list_windows_fonts() -> list[str]:
    fonts_dir = Path("C:/Windows/Fonts")
    if not fonts_dir.is_dir():
        return []
    names = set()
    for f in fonts_dir.glob("*.*"):
        if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
            names.add(f.stem)
    return sorted(names)


async def build_capabilities(ffmpeg_path: Path | None) -> Capabilities:
    if ffmpeg_path is None:
        return Capabilities(ffmpeg_available=False, fonts=list_windows_fonts())

    version = await get_ffmpeg_version(ffmpeg_path)
    xfade = await probe_xfade_transitions(ffmpeg_path)
    encoders = await probe_listed_encoders(ffmpeg_path)
    hwaccel = await probe_hwaccel_encoder(ffmpeg_path, encoders)
    fonts = list_windows_fonts()

    return Capabilities(
        ffmpeg_available=True,
        ffmpeg_version=version,
        xfade_transitions=xfade,
        encoders_listed=encoders,
        working_hwaccel_encoder=hwaccel,
        fonts=fonts,
    )
