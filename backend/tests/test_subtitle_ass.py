"""The BGR-order color conversion is the classic bug in ASS style builders —
these tests exist specifically to catch #RRGGBB being written through in the
wrong byte order.
"""
import pytest

from app.models import Segment, SubtitleStyle
from app.subtitle.ass import build_ass_document, hex_to_ass_color


def test_hex_to_ass_color_white():
    assert hex_to_ass_color("#FFFFFF") == "&H00FFFFFF"


def test_hex_to_ass_color_black():
    assert hex_to_ass_color("#000000") == "&H00000000"


def test_hex_to_ass_color_pure_red_is_bgr_reordered():
    # #FF0000 = R:FF G:00 B:00 -> ASS wants BGR -> 00 00 FF
    assert hex_to_ass_color("#FF0000") == "&H000000FF"


def test_hex_to_ass_color_pure_green_stays_in_middle():
    # #00FF00 = R:00 G:FF B:00 -> BGR -> 00 FF 00
    assert hex_to_ass_color("#00FF00") == "&H0000FF00"


def test_hex_to_ass_color_pure_blue_moves_to_front():
    # #0000FF = R:00 G:00 B:FF -> BGR -> FF 00 00
    assert hex_to_ass_color("#0000FF") == "&H00FF0000"


def test_hex_to_ass_color_with_alpha():
    assert hex_to_ass_color("#FFFFFF", alpha=0x80) == "&H80FFFFFF"


def test_hex_to_ass_color_without_hash_prefix():
    assert hex_to_ass_color("ABCDEF") == "&H00EFCDAB"


def test_hex_to_ass_color_rejects_invalid_input():
    with pytest.raises(ValueError):
        hex_to_ass_color("not-a-color")
    with pytest.raises(ValueError):
        hex_to_ass_color("#FFF")


def test_build_ass_document_contains_style_and_dialogue():
    segments = [Segment(id="1", start=0.0, end=2.5, text="Sain baina uu")]
    style = SubtitleStyle(primary_color="#FF0000", outline_color="#00FF00")
    doc = build_ass_document(segments, style)
    assert "[Script Info]" in doc
    assert "[V4+ Styles]" in doc
    assert "[Events]" in doc
    assert "&H000000FF" in doc  # primary red, BGR order
    assert "&H0000FF00" in doc  # outline green, BGR order
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,Default,,0,0,0,,Sain baina uu" in doc


def test_build_ass_document_alignment_by_position():
    segments = [Segment(id="1", start=0.0, end=1.0, text="x")]
    bottom = build_ass_document(segments, SubtitleStyle(position="bottom"))
    top = build_ass_document(segments, SubtitleStyle(position="top"))
    center = build_ass_document(segments, SubtitleStyle(position="center"))
    assert ",2,20,20," in bottom.split("[Events]")[0]
    assert ",8,20,20," in top.split("[Events]")[0]
    assert ",5,20,20," in center.split("[Events]")[0]


def test_build_ass_document_escapes_braces_and_newlines():
    segments = [Segment(id="1", start=0.0, end=1.0, text="a {b} c\nd")]
    doc = build_ass_document(segments, SubtitleStyle())
    assert r"\{b\}" in doc
    assert r"\Nd" in doc


def test_build_ass_document_sorts_segments_by_start():
    segments = [
        Segment(id="2", start=5.0, end=6.0, text="second"),
        Segment(id="1", start=0.0, end=1.0, text="first"),
    ]
    doc = build_ass_document(segments, SubtitleStyle())
    assert doc.index("first") < doc.index("second")


def test_build_ass_document_empty_segments():
    doc = build_ass_document([], SubtitleStyle())
    assert "[Events]" in doc
