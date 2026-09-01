from itertools import pairwise

from app.ai import schema
from app.ai.prompts import (
    MAX_SEGMENT_SEC,
    SYSTEM_PROMPT,
    build_candidates_prompt,
    build_pick_indices_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
    repair_short_dict,
    split_long_segments,
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


def test_narrows_a_cut_when_the_three_cut_floor_blocks_dropping():
    # hook(0)=10s, context(1-2)=20s, proof(3)=10s, payoff(4-6)=30s -> 70s raw.
    # Dropping reaches the 3-cut floor still over 60s, which is where
    # _shrink_short_to_fit stops by construction; narrowing takes it the rest
    # of the way instead of spending the retry on a prompt already failed once.
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 2, "context"), _cut(3, 3, "proof"), _cut(4, 6, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)

    assert not validate_shorts([fixed], _SEGMENTS)  # now passes real validation
    assert fixed["cuts"][0]["role"] == "hook"
    assert fixed["cuts"][-1]["role"] == "payoff"


def test_narrowing_takes_the_last_segment_of_an_ordinary_cut():
    # Three cuts, none droppable: hook(0)=10s, context(1-3)=30s, payoff(4)=10s
    # -> 50s raw + 3*0.4 = 51.2s. Push it over by widening context to 1-4.
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 5, "context"), _cut(6, 6, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)

    assert not validate_shorts([fixed], _SEGMENTS)
    # The opening the hook leads into survives; the tail is what goes.
    assert fixed["cuts"][1]["start_index"] == 1
    assert fixed["cuts"][1]["end_index"] < 5


def test_narrowing_keeps_the_landing_of_the_payoff_cut():
    # The payoff is the strongest moment and it lands at the END of its cut,
    # so that cut loses its first segment, not its last.
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 1, "context"), _cut(2, 6, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)

    assert not validate_shorts([fixed], _SEGMENTS)
    assert fixed["cuts"][-1]["end_index"] == 6  # the landing is still there
    assert fixed["cuts"][-1]["start_index"] > 2


def test_narrowing_never_trims_a_short_below_the_minimum():
    # hook(0)=10s, context(1)=10s, payoff(2-6)=50s -> 70s raw + 2.4 = 72.4s.
    # Every available trim drops a whole 10s segment; the first two land at
    # 62.4s and 52.4s, and stopping is correct once <=60 is reached rather
    # than continuing down past the 35s floor.
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 1, "context"), _cut(2, 6, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)

    total = sum(
        (_SEGMENTS[c["end_index"]][1] - _SEGMENTS[c["start_index"]][0]) + 0.4
        for c in fixed["cuts"]
    )
    assert 35 <= total <= 60
    assert not validate_shorts([fixed], _SEGMENTS)


def test_shrink_short_to_fit_never_touches_a_short_already_in_range():
    short = {
        "hook_quote": "Нэг",
        "cuts": [_cut(0, 0, "hook"), _cut(1, 1, "context"), _cut(2, 2, "payoff")],
    }
    fixed = repair_short_dict(short, _SEGMENTS)
    assert fixed["cuts"] == short["cuts"]


# --------------------------------------------------------------------------
# split_long_segments — the cut unit the model actually gets. duudlaga.dev
# returns Mongolian ASR text with no terminal punctuation, so the sentence
# split finds nothing and a whole 30s chunk used to arrive as ONE segment,
# putting the 35-60s short out of arithmetic reach (3 cuts minimum).
# --------------------------------------------------------------------------


def _one_segment(span: float, text: str) -> Transcript:
    seg = Segment(id="0", start=0.0, end=span, text=text)
    return Transcript(language="mn", segments=[seg], full_text=text)


def test_split_long_segments_splits_unpunctuated_text_by_words():
    # A 30s chunk of Mongolian ASR output: no '.', '!' or '?' anywhere.
    text = " ".join(["үг"] * 60)
    out = split_long_segments(_one_segment(30.0, text), max_sec=10.0)

    assert len(out) > 1, "an unpunctuated block must still be cuttable"
    assert all(e - s <= 10.0 + 1e-6 for s, e, _ in out)


def test_split_long_segments_keeps_the_span_contiguous_and_whole():
    text = " ".join(f"үг{i}" for i in range(40))
    out = split_long_segments(_one_segment(30.0, text), max_sec=7.0)

    assert out[0][0] == 0.0
    assert out[-1][1] == 30.0  # ends exactly on the original end, no drift
    for (_, prev_end, _), (next_start, _, _) in pairwise(out):
        assert prev_end == next_start  # no gaps, no overlaps
    assert " ".join(t for _, _, t in out) == text  # not one word lost


def test_split_long_segments_splits_a_sentence_that_is_itself_too_long():
    # Two sentences, but each one alone is over max_sec - the sentence path
    # used to emit those whole.
    half = " ".join(["үг"] * 30)
    text = f"{half}. {half}."
    out = split_long_segments(_one_segment(40.0, text), max_sec=10.0)

    assert all(e - s <= 10.0 + 1e-6 for s, e, _ in out)


def test_split_long_segments_leaves_short_segments_alone():
    transcript = _transcript(3)  # 1.5s each, well under the cap
    assert len(split_long_segments(transcript)) == 3


def test_split_long_segments_never_drops_a_single_unsplittable_word():
    out = split_long_segments(_one_segment(40.0, "урт-үг"), max_sec=10.0)
    assert out == [(0.0, 40.0, "урт-үг")]


def test_default_cap_keeps_every_cut_unit_under_the_prompt_limit():
    # The production case end to end: 30s unpunctuated chunks, default cap.
    text = " ".join(["үг"] * 60)
    out = split_long_segments(_one_segment(30.0, text))
    assert all(e - s <= MAX_SEGMENT_SEC + 1e-6 for s, e, _ in out)
    # Three cuts is the minimum for a short, so this is the floor the 35-60s
    # rule has to fit under.
    floor = sum(sorted(e - s for s, e, _ in out)[:3])
    assert floor <= 60.0


def test_split_long_segments_makes_even_pieces_not_a_runt_tail():
    # Filling each piece to the cap and letting the remainder fall out would
    # give 15.0s + 0.4s here; a one-word fragment is not a usable cut unit.
    out = split_long_segments(_one_segment(15.4, " ".join(["үг"] * 77)), max_sec=15.0)
    spans = [e - s for s, e, _ in out]
    assert len(spans) == 2
    assert max(spans) - min(spans) < 1.0


def test_split_long_segments_holds_the_cap_when_the_span_divides_exactly():
    # share == max_sec exactly, so there is no headroom for a word straddling
    # a boundary - the piece count has to grow instead of the cap giving way.
    out = split_long_segments(_one_segment(30.0, " ".join(f"үг{i}" for i in range(23))), max_sec=10.0)
    assert all(e - s <= 10.0 + 1e-6 for s, e, _ in out)


# --------------------------------------------------------------------------
# Speakers. How many people are talking changes what a good cut IS.
# --------------------------------------------------------------------------


def test_nothing_is_claimed_about_speakers_when_nobody_has_looked():
    prompt = build_suggestions_prompt(["[0] a"], 60.0, False, speakers=0)
    assert "speaker" not in prompt.lower()


def test_a_conversation_is_told_not_to_split_an_exchange():
    prompt = build_suggestions_prompt(["[0] a"], 60.0, False, speakers=2)
    assert "2 speakers" in prompt
    assert "question" in prompt


def test_a_monologue_is_not_warned_about_exchanges():
    prompt = build_suggestions_prompt(["[0] a"], 60.0, False, speakers=1)
    assert "One speaker" in prompt
    assert "question" not in prompt


# --- the duration the prompt asks for vs the one the validator accepts ------
#
# Production, both YouTube plans of one job: 718.8s and 492.7s against a
# 600s +/-10% target. The shorts landed in range and the one that did not was
# repaired; the YouTube plans were told "about 600 seconds" with no range and
# no arithmetic step, and nothing failed them.


def test_the_prompt_asks_for_the_window_the_validator_accepts():
    """schema.py used to carry the comment "Matches app.ai.prompts.
    SYSTEM_PROMPT's stated cutting rule" — an admission that two numbers were
    kept in step by hand. A target changed in one place and not the other asks
    for one length and rejects another."""
    low = schema.YOUTUBE_TARGET_DURATION_SEC * (1 - schema.YOUTUBE_TARGET_TOLERANCE)
    high = schema.YOUTUBE_TARGET_DURATION_SEC * (1 + schema.YOUTUBE_TARGET_TOLERANCE)
    # What the model reads, not how the source file happens to wrap.
    rendered = " ".join(SYSTEM_PROMPT.split())

    assert f"{low:.0f}-{high:.0f} seconds" in rendered
    assert f"{schema.MIN_SHORT_DURATION:.0f}-{schema.MAX_SHORT_DURATION:.0f} seconds" in rendered
    # The old hardcoded literal must not survive alongside the derived ones.
    assert "about 600 seconds" not in rendered


def test_the_youtube_rule_asks_for_the_arithmetic_the_shorts_rule_asks_for():
    """The shorts get "sum every cut and adjust until it lands in range" and
    land in range. The YouTube plans got a target to aim near, and missed."""
    youtube_rule = " ".join(SYSTEM_PROMPT.split("## YouTube plans")[1].split())

    assert "sum the mm:ss" in youtube_rule, "no arithmetic step"
    assert "RANGE to land inside" in youtube_rule, "still phrased as a number to approach"


def test_the_request_itself_carries_the_range_not_just_the_system_prompt():
    """A rule stated once in a long system prompt and never again is the rule
    most likely to be dropped on a long transcript."""
    asked = build_suggestions_prompt(["[0] 00:00-00:05 hi"], duration_sec=1700.0, want_youtube=True)
    low = schema.YOUTUBE_TARGET_DURATION_SEC * (1 - schema.YOUTUBE_TARGET_TOLERANCE)

    assert f"{low:.0f}" in asked
    assert "sum them and check" in asked


def test_a_short_video_is_told_to_skip_youtube_without_a_duration_rule():
    asked = build_suggestions_prompt(["[0] 00:00-00:05 hi"], duration_sec=300.0, want_youtube=False)
    assert "empty list" in asked
    assert "sum them and check" not in asked
