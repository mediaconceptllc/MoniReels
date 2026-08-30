"""ASS (SubStation Alpha) style builder for subtitle burn-in.

Burn-in always goes through a generated .ass file (never `force_style` on an
.srt) so font/size/outline/shadow/position are fully controlled. Colors are
`&HAABBGGRR` — BGR order, alpha first — which is the classic place to get
this backwards; see test_subtitle_ass.py for the regression coverage.
"""
from __future__ import annotations

import re

from app.models import Segment, SubtitleStyle
from app.subtitle.cues import to_cues
from app.utils.timecode import seconds_to_ass

_HEX_COLOR_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")

_ALIGNMENT_BY_POSITION = {"bottom": 2, "top": 8, "center": 5}

ASS_PLAY_RES = (1920, 1080)  # style/margin values below are authored against this res


def hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """"#RRGGBB" -> "&HAABBGGRR" (BGR order, alpha first)."""
    m = _HEX_COLOR_RE.match(hex_color)
    if not m:
        raise ValueError(f"Expected a #RRGGBB color, got {hex_color!r}")
    r, g, b = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
    return f"&H{alpha:02X}{b.upper()}{g.upper()}{r.upper()}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("\n", r"\N").replace("{", r"\{").replace("}", r"\}")


def build_ass_document(segments: list[Segment], style: SubtitleStyle) -> str:
    """Transcript segments in, burned-in subtitle script out.

    Splits to cue-sized blocks here for the same reason segments_to_srt does:
    these two are every subtitle this system emits, and burned text is the
    half that cannot be turned off — an untouched 30s segment covers a third
    of a vertical frame for half a minute of the finished video.
    """
    width, height = ASS_PLAY_RES
    alignment = _ALIGNMENT_BY_POSITION.get(style.position, 2)
    primary = hex_to_ass_color(style.primary_color)
    outline = hex_to_ass_color(style.outline_color)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{style.font_family},{style.font_size},{primary},&H000000FF,{outline},&H00000000,"
        f"0,0,0,0,100,100,0,0,1,{style.outline_width},{style.shadow},{alignment},20,20,{style.margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = [
        f"Dialogue: 0,{seconds_to_ass(seg.start)},{seconds_to_ass(seg.end)},Default,,0,0,0,,"
        f"{_escape_ass_text(seg.text)}"
        for seg in sorted(to_cues(segments), key=lambda s: s.start)
    ]
    return header + "\n".join(lines) + ("\n" if lines else "")
