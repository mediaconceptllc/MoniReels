from app.ai.prompts import (
    build_candidates_prompt,
    build_pick_indices_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
    repair_short_dict,
    validate_shorts,
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


def test_build_pick_indices_prompt_wants_three_of_each_and_no_transcript():
    prompt = build_pick_indices_prompt(["[0] cand"], ["[0] yt cand"], duration_sec=1500.0, want_youtube=True)
    assert "3" in prompt
    assert "[0] cand" in prompt and "[0] yt cand" in prompt
    prompt_off = build_pick_indices_prompt(["[0] cand"], [], duration_sec=100.0, want_youtube=False)
    assert "empty list" in prompt_off.lower()


# --------------------------------------------------------------------------
# repair_short_dict — deterministic code-side fixes for the two near-miss
# failures observed most often from Claude models: a too-long total duration
# and a hook_quote that drifted from a true verbatim substring. Segments
# below are 10s each with distinct words, so cut spans are easy to reason
# about: cut [i, j] spans segments i..j inclusive, (j - i + 1) * 10 seconds.
# --------------------------------------------------------------------------

_WORDS = ["Нэг", "Хоёр", "Гурав", "Дөрөв", "Тав", "Зургаа", "Долоо"]
_SEGMENTS = [(float(i * 10), float(i * 10 + 10), word) for i, word in enumerate(_WORDS)]


def _cut(start: int, end: int, role: str) -> dict:
    return {"start_index": start, "end_index": end, "role": role, "reason": "r"}


def test_fix_hook_quote_replaces_invalid_quote_with_real_hook_text():
    short = {
        "hook_quote": "энэ бол буруу ишлэл",  # not present in the transcript at all
        "cuts": [_cut(0, 0, "hook"), _cut(1, 1, "context"), _cut(2, 2, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)
    assert fixed["hook_quote"] == "Нэг"  # the hook cut's (index 0) actual text


def test_fix_hook_quote_leaves_valid_quote_untouched():
    short = {
        "hook_quote": "Хоёр",  # already a real verbatim substring
        "cuts": [_cut(1, 1, "hook"), _cut(2, 2, "context"), _cut(3, 3, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)
    assert fixed["hook_quote"] == "Хоёр"


def test_shrink_short_to_fit_drops_shortest_interior_cut_to_land_in_range():
    # hook(0)=10s, context(1)=10s, proof(2)=10s, payoff(3-5)=30s -> 60s raw,
    # +4*2*CUT_PAD_SEC padding = 61.6s, over the 60s max.
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 1, "context"), _cut(2, 2, "proof"), _cut(3, 5, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)

    assert len(fixed["cuts"]) == 3  # one interior cut (context or proof, both 10s) was dropped
    assert fixed["cuts"][0]["role"] == "hook"  # never drops the hook
    assert fixed["cuts"][-1]["role"] == "payoff"  # never drops the last (payoff) cut
    assert not validate_shorts([fixed], _SEGMENTS)  # now passes real validation


def test_shrink_short_to_fit_gives_up_at_three_cut_floor_if_still_over():
    # hook(0)=10s, context(1-2)=20s, proof(3)=10s, payoff(4-6)=30s -> 70s raw;
    # even dropping one interior cut down to the 3-cut floor can't reach <=60.
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 2, "context"), _cut(3, 3, "proof"), _cut(4, 6, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)
    # Can't be brought into range by dropping cuts - returned unchanged so the
    # caller's own validate_shorts() correctly still rejects it.
    assert fixed["cuts"] == short["cuts"]
    assert validate_shorts([fixed], _SEGMENTS)  # still flagged as a problem


def test_shrink_short_to_fit_never_touches_a_short_already_in_range():
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 1, "context"), _cut(2, 2, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)
    assert fixed["cuts"] == short["cuts"]
