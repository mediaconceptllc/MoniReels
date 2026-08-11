"""Float-seconds <-> timecode string conversions.

Hard rule: every timestamp inside the system is a float number of seconds.
Only convert to a string at a UI/SRT/ASS boundary; never do math on strings.
"""
from __future__ import annotations

import re

_SRT_RE = re.compile(r"^(\d+):(\d{2}):(\d{2}),(\d{3})$")
_ASS_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")
_CLOCK_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?$")


def seconds_to_srt(seconds: float) -> str:
    """HH:MM:SS,mmm — SRT uses a comma millisecond separator."""
    seconds = max(0.0, seconds)
    total_ms = round(seconds * 1000)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def srt_to_seconds(value: str) -> float:
    m = _SRT_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid SRT timecode: {value!r}")
    h, mi, s, ms = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def seconds_to_ass(seconds: float) -> str:
    """H:MM:SS.cc — ASS uses centiseconds, no leading zero on hours."""
    seconds = max(0.0, seconds)
    total_cs = round(seconds * 100)
    hours, rem = divmod(total_cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_to_seconds(value: str) -> float:
    m = _ASS_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid ASS timecode: {value!r}")
    h, mi, s, cs = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + cs / 100


def seconds_to_clock(seconds: float) -> str:
    """HH:MM:SS.mmm — generic UI-facing display format."""
    seconds = max(0.0, seconds)
    total_ms = round(seconds * 1000)
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def clock_to_seconds(value: str) -> float:
    m = _CLOCK_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid clock timecode: {value!r}")
    h, mi, s, frac = m.groups()
    total = float(int(h) * 3600 + int(mi) * 60 + int(s))
    if frac:
        total += int(frac.ljust(3, "0")[:3]) / 1000
    return total


def seconds_to_mmss(seconds: float) -> str:
    """mm:ss — compact form used when sending transcript segments to the LLM."""
    seconds = max(0.0, seconds)
    total = round(seconds)
    minutes, secs = divmod(total, 60)
    return f"{minutes:02d}:{secs:02d}"


def ffmpeg_out_time_to_seconds(out_time: str) -> float:
    """Parse the `out_time=` value FFmpeg emits on -progress (HH:MM:SS.microseconds)."""
    out_time = out_time.strip()
    if not out_time or out_time == "N/A":
        return 0.0
    h, mi, s = out_time.split(":")
    return int(h) * 3600 + int(mi) * 60 + float(s)
