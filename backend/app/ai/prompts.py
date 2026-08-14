"""Prompt construction. Transcript segments are sent as `[index] mm:ss-mm:ss text`
so the model can only ever reference segments that actually exist in the transcript.

The model returns SEGMENT INDICES, not raw seconds. Timestamps are resolved on our
side from those indices, which makes invented timestamps structurally impossible and
guarantees every cut lands on a real segment boundary.
"""
from __future__ import annotations

import re

from app.models import Transcript
from app.utils.timecode import seconds_to_mmss

# Raised from 12k. Current-generation models handle a full 60-90 min transcript in
# one request; chunking costs more than it saves because a story that straddles a
# chunk boundary can never be seen whole. Only very long videos chunk now.
CHAR_BUDGET = 45_000

# Chunks overlap by this many segments so a story spanning a boundary is fully
# visible in at least one chunk.
CHUNK_OVERLAP_SEGMENTS = 6

# STT segments longer than this are split before prompting. Without this the model
# cannot cut precisely — a 46-second segment is an all-or-nothing choice.
MAX_SEGMENT_SEC = 15.0


SYSTEM_PROMPT = """You are an expert short-form video editor for YouTube Shorts and
Meta Reels. You are given a timestamped transcript of a longer video. You build edits,
not summaries.

## Cutting rules
- Every short is assembled from 3-5 SEPARATE, non-contiguous cuts. Returning one
  continuous range is a failure — that is a trailer-less excerpt, not an edit.
- Each cut is identified by segment indices (`start_index`, `end_index`, inclusive).
  Never output raw seconds. Never reference an index outside the transcript given.
- Total duration across all cuts of one short: 35-60 seconds. Never exceed 60.
- Structure the cuts in this order, one `role` each:
    hook    - the conflict, the mystery, or the surprising claim. Never an intro.
    context - the minimum background needed to understand the payoff.
    proof   - concrete numbers, names, dates, evidence.
    payoff  - the emotional or revelatory landing. This must be the strongest
              moment in the whole short. Never end on a throwaway line.
  `context` may be omitted if the hook is self-explanatory. `proof` may repeat.
- Cuts must be ordered as they will appear in the final edit, which need NOT match
  chronological order in the source. Pulling the payoff from later in the video and a
  proof line from earlier is expected and good.
- Never include: greetings, sign-offs, sponsor or donation reads, bank account numbers,
  "next story" segues, host self-introduction, music-only or filler segments, or
  transcription noise. These kill retention instantly.
- Start on the first word of a real sentence and end on the last word of one.

## Content rules
- The shorts must be about MEANINGFULLY DIFFERENT topics from each other. Three angles
  on the same story is a failure.
- Rank candidates higher when they have: a concrete conflict or reversal, a number a
  viewer can picture, or direct local relevance to the audience described below.
- `hook_text` is on-screen text for the first 3 seconds. Under 12 words, in the
  transcript's language, phrased as a question or a jarring claim. Never start it with
  "Today" / "In this video" / their equivalents.
- `hook_quote` must be a verbatim substring copied from the transcript, taken from
  inside the `hook` cut. Do not paraphrase it.
- Write `title`, `hook_text`, `on_screen_texts` and `caption` in the SAME LANGUAGE as
  the transcript. `role` and `why_it_works` stay in English.

## Method (do this internally before answering)
1. List every distinct story in the video with its segment range.
2. Draft 5 candidate shorts across those stories.
3. Score each 1-10 on: hook strength, ease of sourcing b-roll, audience relevance.
4. Return only the 3 highest-scoring. Put the three scores in `why_it_works`.

## YouTube plans
When requested, produce exactly 3 independent long-form highlight plans. Each selects
multiple non-overlapping keep-ranges (by segment index) that together form a coherent
condensed version, combined duration about 600 seconds. The 3 plans must take
meaningfully different throughlines — not near-duplicates.

Output valid JSON matching the schema exactly. No commentary, no markdown fences."""


# ---------------------------------------------------------------------------
# Strict JSON schema — pass as response_format={"type": "json_schema", ...}
# ---------------------------------------------------------------------------

CUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["start_index", "end_index", "role", "reason"],
    "properties": {
        "start_index": {"type": "integer"},
        "end_index": {"type": "integer"},
        "role": {"type": "string", "enum": ["hook", "context", "proof", "payoff"]},
        "reason": {"type": "string"},
    },
}

SHORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "hook_text", "hook_quote", "cuts",
        "on_screen_texts", "b_roll", "caption", "hashtags", "why_it_works",
    ],
    "properties": {
        "title": {"type": "string"},
        "hook_text": {"type": "string"},
        "hook_quote": {"type": "string"},
        "cuts": {"type": "array", "minItems": 3, "maxItems": 5, "items": CUT_SCHEMA},
        "on_screen_texts": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "b_roll": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "maxItems": 8, "items": {"type": "string"}},
        "why_it_works": {"type": "string"},
    },
}

YOUTUBE_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "throughline", "keep_ranges"],
    "properties": {
        "title": {"type": "string"},
        "throughline": {"type": "string"},
        "keep_ranges": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start_index", "end_index"],
                "properties": {
                    "start_index": {"type": "integer"},
                    "end_index": {"type": "integer"},
                },
            },
        },
    },
}

SUGGESTIONS_SCHEMA: dict = {
    "name": "suggestions",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["shorts", "youtube"],
        "properties": {
            "shorts": {"type": "array", "minItems": 3, "maxItems": 3, "items": SHORT_SCHEMA},
            "youtube": {"type": "array", "maxItems": 3, "items": YOUTUBE_PLAN_SCHEMA},
        },
    },
}


# ---------------------------------------------------------------------------
# Segment preparation
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def split_long_segments(transcript: Transcript, max_sec: float = MAX_SEGMENT_SEC):
    """Splits segments longer than max_sec at sentence boundaries, distributing the
    original time span proportionally to sentence length.

    STT often returns 40-60s blocks when word timings are absent. Those are unusable
    as cut units: the model has to take the whole block or nothing. Splitting first is
    what makes precise, multi-cut edits possible at all.

    Returns a list of (start, end, text) tuples. Timing inside a split segment is
    approximate — good enough to choose cuts, and the editor still lets the user nudge
    the handles afterwards.
    """
    out: list[tuple[float, float, str]] = []
    for seg in transcript.segments:
        span = seg.end - seg.start
        text = (seg.text or "").strip()
        if span <= max_sec or not text:
            out.append((seg.start, seg.end, text))
            continue

        sentences = [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]
        if len(sentences) < 2:
            out.append((seg.start, seg.end, text))
            continue

        total_chars = sum(len(s) for s in sentences)
        cursor = seg.start
        for i, sentence in enumerate(sentences):
            share = span * (len(sentence) / total_chars)
            end = seg.end if i == len(sentences) - 1 else cursor + share
            out.append((cursor, end, sentence))
            cursor = end
    return out


def build_segment_lines(transcript: Transcript) -> list[str]:
    return [
        f"[{i}] {seconds_to_mmss(start)}-{seconds_to_mmss(end)} {text}"
        for i, (start, end, text) in enumerate(split_long_segments(transcript))
    ]


def chunk_segment_lines(
    lines: list[str],
    char_budget: int = CHAR_BUDGET,
    overlap: int = CHUNK_OVERLAP_SEGMENTS,
) -> list[list[str]]:
    """Groups lines into overlapping chunks under char_budget. Never drops a line — if a
    single line alone exceeds the budget it still becomes its own chunk, so nothing is
    silently truncated. Consecutive chunks share `overlap` trailing lines so a story
    spanning a boundary is visible whole in at least one chunk.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > char_budget:
            chunks.append(current)
            current = current[-overlap:] if overlap else []
            current_len = sum(len(x) + 1 for x in current)
        current.append(line)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Prompt bodies
# ---------------------------------------------------------------------------

def _audience_block(audience: str | None) -> str:
    if not audience:
        return ""
    return f"Target audience: {audience}\n"


def build_suggestions_prompt(
    lines: list[str],
    duration_sec: float,
    want_youtube: bool,
    audience: str | None = None,
) -> str:
    youtube_instruction = (
        "Also produce exactly 3 independent YouTube long-form highlight plans (`youtube`), "
        "since this video is longer than 20 minutes."
        if want_youtube
        else "Set `youtube` to an empty list — this video is under 20 minutes long."
    )
    transcript_block = "\n".join(lines)
    return (
        f"Video duration: {duration_sec:.1f} seconds.\n"
        f"{_audience_block(audience)}"
        f"{youtube_instruction}\n\n"
        f"Transcript segments:\n{transcript_block}"
    )


def build_candidates_prompt(
    lines: list[str],
    duration_sec: float,
    want_youtube: bool,
    audience: str | None = None,
) -> str:
    transcript_block = "\n".join(lines)
    youtube_instruction = (
        "Also suggest candidate keep-ranges for YouTube highlight reels from this portion "
        "of the video (these will be combined with candidates from other portions later, "
        "so a `youtube` list here is just candidates, not final)."
        if want_youtube
        else "Set `youtube` to an empty list."
    )
    return (
        f"This is one portion of a longer video (total duration {duration_sec:.1f}s). "
        f"Identify the distinct stories in THIS PORTION and suggest up to 3 candidate "
        f"shorts from them, following the cutting rules.\n"
        f"{_audience_block(audience)}"
        f"{youtube_instruction}\n\n"
        f"Transcript segments (this portion):\n{transcript_block}"
    )


def build_pick_best_prompt(
    candidate_summaries: list[str],
    lines: list[str],
    duration_sec: float,
    want_youtube: bool,
    audience: str | None = None,
) -> str:
    """Unlike the previous version this re-sends the full segment lines alongside the
    candidate summaries, so the final pass can re-cut a candidate rather than only
    rubber-stamping ranges it can no longer see the text of.
    """
    youtube_instruction = (
        "Build exactly 3 final, independent YouTube long-form highlight plans from the "
        "candidate keep-ranges below, each targeting a combined duration of about 600 "
        "seconds and each meaningfully different from the others."
        if want_youtube
        else "Set `youtube` to an empty list."
    )
    candidates_block = "\n".join(candidate_summaries)
    transcript_block = "\n".join(lines)
    return (
        f"Video duration: {duration_sec:.1f} seconds. Below are candidate moments gathered "
        f"from different portions of the video, followed by the full transcript. Select the "
        f"best 3 candidates, and RE-CUT each one against the full transcript — you may pull "
        f"a stronger payoff or proof cut from any part of the video, not only the portion the "
        f"candidate came from.\n"
        f"{_audience_block(audience)}"
        f"{youtube_instruction}\n\n"
        f"Candidates:\n{candidates_block}\n\n"
        f"Transcript segments:\n{transcript_block}"
    )


def build_repair_prompt(problems: list[str]) -> str:
    """Sent as a follow-up user turn when validation fails, instead of re-running the
    whole request from scratch. One retry catches nearly everything.
    """
    issues = "\n".join(f"- {p}" for p in problems)
    return (
        "Your previous output violated these rules:\n"
        f"{issues}\n\n"
        "Return corrected JSON matching the same schema. Fix only the listed problems."
    )


# ---------------------------------------------------------------------------
# Validation — run before showing anything to the user
# ---------------------------------------------------------------------------

def validate_shorts(shorts: list[dict], segments: list[tuple[float, float, str]]) -> list[str]:
    """Returns a list of human-readable problems. Empty list means the output is usable.
    Feed a non-empty result into build_repair_prompt and retry once.
    """
    problems: list[str] = []
    last = len(segments) - 1
    full_text = " ".join(text for _, _, text in segments)

    for n, short in enumerate(shorts, 1):
        cuts = short.get("cuts", [])
        if len(cuts) < 3:
            problems.append(f"Short {n}: only {len(cuts)} cuts, minimum is 3 separate cuts.")

        total = 0.0
        for cut in cuts:
            s, e = cut.get("start_index"), cut.get("end_index")
            if not isinstance(s, int) or not isinstance(e, int) or not (0 <= s <= e <= last):
                problems.append(f"Short {n}: cut [{s}, {e}] is out of range 0-{last}.")
                continue
            total += segments[e][1] - segments[s][0]

        if not 30 <= total <= 65:
            problems.append(f"Short {n}: total duration {total:.0f}s, must be 35-60s.")

        roles = [c.get("role") for c in cuts]
        if roles and roles[-1] != "payoff":
            problems.append(f"Short {n}: last cut has role '{roles[-1]}', must be 'payoff'.")
        if "hook" not in roles:
            problems.append(f"Short {n}: no cut with role 'hook'.")

        quote = (short.get("hook_quote") or "").strip()
        if quote and quote not in full_text:
            problems.append(f"Short {n}: hook_quote is not a verbatim transcript substring.")

    return problems