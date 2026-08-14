from app.ai.prompts import (
    build_candidates_prompt,
    build_pick_best_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
)
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
    chunks = chunk_segment_lines(lines, char_budget=250, overlap=0)
    assert len(chunks) > 1
    # every line must appear exactly once across all chunks — nothing dropped
    flat = [line for chunk in chunks for line in chunk]
    assert flat == lines


def test_chunk_segment_lines_never_drops_an_oversized_single_line():
    huge_line = "y" * 1000
    lines = ["short"] + [huge_line] + ["short2"]
    chunks = chunk_segment_lines(lines, char_budget=100, overlap=0)
    flat = [line for chunk in chunks for line in chunk]
    assert flat == lines
    assert any(huge_line in chunk for chunk in chunks)


def test_chunk_segment_lines_empty():
    assert chunk_segment_lines([], char_budget=100) == []


def test_chunk_segment_lines_overlaps_trailing_lines_across_boundary():
    """A story that straddles a chunk boundary must be visible whole in at
    least one chunk - consecutive chunks share `overlap` trailing lines
    instead of cutting cleanly, so a line can legitimately appear in two
    chunks (never zero).
    """
    lines = [f"line-{i:02d}" for i in range(20)]  # 7 chars + 1 = 8 each
    chunks = chunk_segment_lines(lines, char_budget=40, overlap=2)
    assert len(chunks) > 1
    seen = {line for chunk in chunks for line in chunk}
    assert seen == set(lines)  # nothing dropped
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev[-2:] == nxt[:2]  # the shared overlap actually matches


# --------------------------------------------------------------------------
# youtube_instruction wording: v2 contract is a list (0 or exactly 3 plans),
# never a single nullable object - prompts must say so, not "null"/"a plan".
# --------------------------------------------------------------------------


def test_build_suggestions_prompt_wants_three_youtube_plans():
    prompt = build_suggestions_prompt(["line"], duration_sec=1500.0, want_youtube=True)
    assert "3" in prompt and "YouTube" in prompt
    assert "null" not in prompt.lower()


def test_build_suggestions_prompt_sets_empty_list_when_not_wanted():
    prompt = build_suggestions_prompt(["line"], duration_sec=100.0, want_youtube=False)
    assert "empty list" in prompt.lower()
    assert "null" not in prompt.lower()


def test_build_candidates_prompt_youtube_wording():
    assert "empty list" in build_candidates_prompt(["line"], 100.0, want_youtube=False).lower()
    assert "youtube" in build_candidates_prompt(["line"], 1500.0, want_youtube=True).lower()


def test_build_pick_best_prompt_wants_three_independent_plans():
    prompt = build_pick_best_prompt(["- candidate"], ["line"], duration_sec=1500.0, want_youtube=True)
    assert "3" in prompt
    assert "independent" in prompt.lower()
    prompt_off = build_pick_best_prompt(["- candidate"], ["line"], duration_sec=100.0, want_youtube=False)
    assert "empty list" in prompt_off.lower()
