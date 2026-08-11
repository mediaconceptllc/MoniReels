from app.models import Segment
from app.subtitle.srt import segments_to_srt


def _segment(start: float, end: float, text: str) -> Segment:
    return Segment(id="s", start=start, end=end, text=text)


def test_segments_to_srt_basic_format():
    segments = [_segment(0.0, 2.5, "Hello world")]
    srt = segments_to_srt(segments)
    assert srt == "1\n00:00:00,000 --> 00:00:02,500\nHello world\n"


def test_segments_to_srt_multiple_blocks_numbered_sequentially():
    segments = [_segment(0.0, 1.0, "one"), _segment(2.0, 3.0, "two")]
    srt = segments_to_srt(segments)
    blocks = srt.strip("\n").split("\n\n")
    assert blocks[0].startswith("1\n")
    assert blocks[1].startswith("2\n")


def test_segments_to_srt_sorts_by_start_time():
    segments = [_segment(5.0, 6.0, "second"), _segment(0.0, 1.0, "first")]
    srt = segments_to_srt(segments)
    assert srt.index("first") < srt.index("second")


def test_segments_to_srt_empty():
    assert segments_to_srt([]) == ""
