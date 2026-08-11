from app.ai.prompts import build_segment_lines, chunk_segment_lines
from app.models import Segment, Transcript


def _transcript(n: int, text: str = "hello world") -> Transcript:
    segments = [Segment(id=str(i), start=float(i * 2), end=float(i * 2 + 1.5), text=text) for i in range(n)]
    return Transcript(language="mn", segments=segments, full_text=" ".join(s.text for s in segments))


def test_build_segment_lines_format_and_order():
    transcript = _transcript(3)
    lines = build_segment_lines(transcript)
    assert len(lines) == 3
    assert lines[0].startswith("[0] 00:00-00:02")
    assert lines[1].startswith("[1] 00:02-00:04")
    assert "hello world" in lines[0]


def test_chunk_segment_lines_single_chunk_when_under_budget():
    lines = ["short line"] * 5
    chunks = chunk_segment_lines(lines, char_budget=10_000)
    assert len(chunks) == 1
    assert chunks[0] == lines


def test_chunk_segment_lines_splits_when_over_budget():
    lines = ["x" * 100] * 5
    chunks = chunk_segment_lines(lines, char_budget=250)
    assert len(chunks) > 1
    # every line must appear exactly once across all chunks — nothing dropped
    flat = [line for chunk in chunks for line in chunk]
    assert flat == lines


def test_chunk_segment_lines_never_drops_an_oversized_single_line():
    huge_line = "y" * 1000
    lines = ["short"] + [huge_line] + ["short2"]
    chunks = chunk_segment_lines(lines, char_budget=100)
    flat = [line for chunk in chunks for line in chunk]
    assert flat == lines
    assert any(huge_line in chunk for chunk in chunks)


def test_chunk_segment_lines_empty():
    assert chunk_segment_lines([], char_budget=100) == []
