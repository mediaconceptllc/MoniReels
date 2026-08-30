"""Prompt construction. Transcript segments are sent as `[index] mm:ss-mm:ss text`
so the model can only ever reference segments that actually exist in the transcript.

The model returns SEGMENT INDICES, not raw seconds. Timestamps are resolved on our
side from those indices, which makes invented timestamps structurally impossible and
guarantees every cut lands on a real segment boundary.
"""
from __future__ import annotations

import re

from app.ai.schema import CUT_PAD_SEC, MAX_SHORT_DURATION, MIN_SHORT_DURATION
from app.models import Transcript
from app.utils.text import split_span
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
- Total duration across all cuts of one short: 35-60 seconds. Never exceed 60. Before
  finalizing a short, actually add up each cut's own span (its end mm:ss minus its
  start mm:ss) and sum those spans across all its cuts — do not estimate by eye. If
  the sum lands outside 35-60s, narrow/widen a cut's index range or swap one for a
  shorter/longer cut, then re-add the sum. Overshooting by even 5-10 seconds is a
  failure just like overshooting by 60.
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
2. Draft 5 candidate shorts across those stories. For each, sum the mm:ss span of
   every cut and adjust cuts until that sum is 35-60s — a candidate whose cuts don't
   actually add up in-range is not a valid candidate yet.
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

PICK_SCHEMA: dict = {
    "name": "pick_best",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["short_indices", "youtube_indices"],
        "properties": {
            "short_indices": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "integer"}},
            "youtube_indices": {"type": "array", "maxItems": 3, "items": {"type": "integer"}},
        },
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


def _split_on_words(
    start: float, end: float, text: str, max_sec: float
) -> list[tuple[float, float, str]]:
    """The case the sentence split cannot serve at all: duudlaga.dev returns
    Mongolian ASR text with no terminal punctuation, so `_SENTENCE_END` finds
    nothing to cut on and the chunk reached the model whole. Measured on the
    production transcript that failed - 68 chunks over 17:44 - that left 32 of
    68 segments above this cap and a median segment of 14.3s: half the video
    offered only take-it-or-leave-it blocks.

    A 35-60s short needs 3+ cuts, so building one out of blocks that size
    overshoots as soon as the model picks anything but the shortest scraps -
    observed as shorts of 80s, 93s, 108s, 109s and 120s. The repair prompt
    then asked it to "narrow a cut's index range" when every cut was already
    a single indivisible segment, so the retry could not converge either and
    the job died having paid for two calls. Not strictly unreachable (the
    shortest three segments summed to 14.5s), but unreachable for any sensible
    choice of content, which amounts to the same failure.

    Only a duration limit here. Characters are a subtitle's problem, not a
    cut's: the model reads a segment's text, it does not have to fit it on a
    frame.
    """
    return split_span(start, end, text, max_sec=max_sec)


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
            out.extend(_split_on_words(seg.start, seg.end, text, max_sec))
            continue

        total_chars = sum(len(s) for s in sentences)
        cursor = seg.start
        for i, sentence in enumerate(sentences):
            share = span * (len(sentence) / total_chars)
            end = seg.end if i == len(sentences) - 1 else cursor + share
            # One sentence can be longer than max_sec on its own, so the
            # sentence path needs the same word-level fallback rather than
            # emitting a block the model still cannot cut inside.
            out.extend(_split_on_words(cursor, end, sentence, max_sec))
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


def build_pick_indices_prompt(
    short_summaries: list[str],
    youtube_summaries: list[str],
    duration_sec: float,
    want_youtube: bool,
    audience: str | None = None,
) -> str:
    """Selection only, from already-built candidates - deliberately does NOT
    re-send the transcript. An earlier version re-sent the full transcript so
    this pass could re-cut a candidate rather than only rubber-stamp it, but
    on a long transcript in a token-dense language (e.g. Mongolian, ~2.4
    chars/token vs English's ~4) that alone can exceed a low-tier OpenAI
    account's tokens-per-minute cap before a single output token is
    generated - and padding the transcript down to just the regions near
    candidates doesn't help, since good candidates end up spread across
    nearly the whole video anyway. Picking among candidates that were
    already cut against the real transcript keeps requests small regardless
    of video length, at the cost of no further re-cutting in this pass.
    """
    youtube_instruction = (
        "Also choose exactly 3 of the candidate YouTube plans below (by index) - the 3 "
        "that together take the most meaningfully different throughlines. List their "
        "indices, in your preferred order, in `youtube_indices`."
        if want_youtube
        else "Set `youtube_indices` to an empty list."
    )
    shorts_block = "\n".join(short_summaries) or "(none)"
    youtube_block = "\n".join(youtube_summaries) or "(none)"
    return (
        f"Video duration: {duration_sec:.1f} seconds. Below are candidate shorts gathered "
        f"from different portions of the video - each already cut and ready to use. Choose "
        f"exactly 3 (by index) that are the strongest and about MEANINGFULLY DIFFERENT "
        f"topics from each other. List their indices, in your preferred order, in "
        f"`short_indices`.\n"
        f"{_audience_block(audience)}"
        f"{youtube_instruction}\n\n"
        f"Candidate shorts:\n{shorts_block}\n\n"
        f"Candidate YouTube plans:\n{youtube_block}"
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
            # + 2*CUT_PAD_SEC per cut to match schema.py's _resolve_cut, which
            # pads every cut's start/end outward by CUT_PAD_SEC. Without this,
            # a candidate measuring e.g. 59.6s raw here (passes the 35-60
            # gate below) resolves to ~61s after padding is applied for real
            # in postprocess_suggestions - which has no tolerance and no
            # retry left, failing the whole job on a short this function had
            # already called "usable".
            total += (segments[e][1] - segments[s][0]) + 2 * CUT_PAD_SEC

        if not MIN_SHORT_DURATION <= total <= MAX_SHORT_DURATION:
            # The bounds come from schema.py rather than being restated here:
            # this function decides whether a candidate is "usable" for the
            # chunked pipeline (app.ai.suggest), so any gap between this gate
            # and postprocess_suggestions' real enforcement lets a duration
            # (e.g. 32s, 62s) sail through here as fine, get treated as a
            # keeper, and then die with no retry left when postprocess
            # finally checks it for real - after the API calls that picked it
            # are already spent. A restated constraint ("must be 35-60s")
            # without a concrete target gave the model nothing to act on
            # beyond retrying with a similar guess - observed producing the
            # identical ~19s total on the repair retry as the original
            # attempt. Naming the exact gap and a mechanical fix (widen/add
            # vs narrow/drop a cut, using the real per-segment timestamps
            # already in the prompt) is something a retry can actually
            # follow.
            lo, hi = MIN_SHORT_DURATION, MAX_SHORT_DURATION
            if total < lo:
                problems.append(
                    f"Short {n}: total duration {total:.0f}s is too short, must be {lo:.0f}-{hi:.0f}s. "
                    f"Add {lo - total:.0f} to {hi - total:.0f} more seconds by widening an existing "
                    f"cut's start_index/end_index further apart, or adding one more cut (up to 5 "
                    f"total) - use the real segment timestamps shown to pick a range that size."
                )
            else:
                problems.append(
                    f"Short {n}: total duration {total:.0f}s is too long, must be {lo:.0f}-{hi:.0f}s. "
                    f"Cut {total - hi:.0f} to {total - lo:.0f} seconds by narrowing a cut's index "
                    f"range, or dropping the weakest cut (down to 3 minimum)."
                )

        roles = [c.get("role") for c in cuts]
        if roles and roles[-1] != "payoff":
            problems.append(f"Short {n}: last cut has role '{roles[-1]}', must be 'payoff'.")
        if "hook" not in roles:
            problems.append(f"Short {n}: no cut with role 'hook'.")

        quote = (short.get("hook_quote") or "").strip()
        if quote and quote not in full_text:
            problems.append(f"Short {n}: hook_quote is not a verbatim transcript substring.")

    return problems


# ---------------------------------------------------------------------------
# Deterministic repair — code-side fixes for the failure modes observed most
# often (Claude models in particular): a too-long total duration, fixed by
# dropping a cut or narrowing one, and a hook_quote that drifted from a true
# verbatim substring. Applied before validate_shorts gets the final say, so a
# near-miss the model won't reliably self-correct even with a specific repair
# prompt gets fixed outright instead of spending (and possibly exhausting) a
# retry, or being discarded.
#
# Duration is arithmetic on timestamps we already hold, so asking a model to
# redo it is the wrong tool twice over: it costs a billed call and it is the
# part the model is worst at. The repair prompt states the exact remedy in
# seconds and production still came back over the limit on the retry.
# ---------------------------------------------------------------------------


def _fix_hook_quote_if_invalid(short: dict, segments: list[tuple[float, float, str]]) -> dict:
    """Replaces hook_quote with the literal text of the hook cut's own
    segments when it isn't a verbatim transcript substring - guaranteed
    verbatim by construction, rather than relying on the model recalling a
    quote exactly (a common copying-fidelity slip, especially in non-Latin
    scripts). Returns `short` unchanged if the quote is already fine or
    there's no hook cut to source a replacement from.
    """
    quote = (short.get("hook_quote") or "").strip()
    full_text = " ".join(text for _, _, text in segments)
    if not quote or quote in full_text:
        return short

    cuts = short.get("cuts", [])
    hook_cut = next((c for c in cuts if c.get("role") == "hook"), cuts[0] if cuts else None)
    if not hook_cut:
        return short
    s, e = hook_cut.get("start_index"), hook_cut.get("end_index")
    if not isinstance(s, int) or not isinstance(e, int) or not (0 <= s <= e < len(segments)):
        return short

    replacement = " ".join(segments[i][2] for i in range(s, e + 1)).strip()
    if not replacement:
        return short
    return {**short, "hook_quote": replacement}


def _shrink_short_to_fit(short: dict, segments: list[tuple[float, float, str]]) -> dict:
    """If the short's total duration (including CUT_PAD_SEC, same as
    validate_shorts) exceeds 60s, repeatedly drops the shortest droppable
    interior cut - never the first cut, the last cut, or any cut whose role
    is "hook" - until it fits or hits the 3-cut floor. Returns `short`
    unchanged if it's not over 60s, or if dropping cuts can't bring it into
    range. Never touches an under-length short: dropping cuts can't
    manufacture more content, only a genuinely different cut selection can.
    """
    cuts = list(short.get("cuts", []))

    def cut_span(c: dict) -> float:
        s, e = c.get("start_index"), c.get("end_index")
        if isinstance(s, int) and isinstance(e, int) and 0 <= s <= e < len(segments):
            return segments[e][1] - segments[s][0]
        return 0.0

    def total_duration(cs: list[dict]) -> float:
        return sum(cut_span(c) + 2 * CUT_PAD_SEC for c in cs)

    if len(cuts) <= 3 or total_duration(cuts) <= MAX_SHORT_DURATION:
        return short

    while len(cuts) > 3 and total_duration(cuts) > MAX_SHORT_DURATION:
        interior = [
            (i, c) for i, c in enumerate(cuts) if i != 0 and i != len(cuts) - 1 and c.get("role") != "hook"
        ]
        if not interior:
            break
        drop_i, _ = min(interior, key=lambda ic: cut_span(ic[1]))
        cuts.pop(drop_i)

    if total_duration(cuts) <= MAX_SHORT_DURATION:
        return {**short, "cuts": cuts}
    return short


def _narrow_cuts_to_fit(short: dict, segments: list[tuple[float, float, str]]) -> dict:
    """If the short is still over MAX_SHORT_DURATION after dropping cuts, trims
    one segment at a time off the widest cut until it fits - the other half of
    the remedy `validate_shorts` names ("narrowing a cut's index range") and
    the only one left once a short is at the 3-cut floor, where
    `_shrink_short_to_fit` returns immediately by construction. A short with
    exactly 3 wide cuts therefore got no code-side repair at all and spent the
    single retry on a prompt the model had already failed once.

    Which end is trimmed follows the roles the system prompt assigns: a payoff
    cut (and the last cut, whatever its role, since that is the one played
    last) loses its FIRST segment so the landing survives; every other cut
    loses its LAST, keeping the opening the hook depends on.

    Never trims below MIN_SHORT_DURATION: one segment can be worth 30s, so a
    single careless trim can take a 62s short to 32s, trading one validation
    failure for the opposite one. A trim that would undershoot is skipped in
    favour of the next-widest cut, and when no cut can be trimmed safely the
    short is returned unchanged for `validate_shorts` to reject - same
    best-effort contract as `_shrink_short_to_fit`.
    """
    cuts = [dict(c) for c in short.get("cuts", [])]
    if not cuts:
        return short

    def cut_span(c: dict) -> float:
        s, e = c.get("start_index"), c.get("end_index")
        if isinstance(s, int) and isinstance(e, int) and 0 <= s <= e < len(segments):
            return segments[e][1] - segments[s][0]
        return 0.0

    def total_duration(cs: list[dict]) -> float:
        return sum(cut_span(c) + 2 * CUT_PAD_SEC for c in cs)

    def trim(c: dict, is_last: bool) -> dict | None:
        s, e = c.get("start_index"), c.get("end_index")
        if not isinstance(s, int) or not isinstance(e, int) or e <= s:
            return None  # single-segment cut: nothing left to narrow
        if is_last or c.get("role") == "payoff":
            return {**c, "start_index": s + 1}
        return {**c, "end_index": e - 1}

    while total_duration(cuts) > MAX_SHORT_DURATION:
        # Widest first: the largest single reduction, and it evens the cuts out
        # rather than whittling one down to nothing.
        for i in sorted(range(len(cuts)), key=lambda j: cut_span(cuts[j]), reverse=True):
            narrowed = trim(cuts[i], i == len(cuts) - 1)
            if narrowed is None:
                continue
            candidate = cuts[:i] + [narrowed] + cuts[i + 1 :]
            if total_duration(candidate) >= MIN_SHORT_DURATION:
                cuts = candidate
                break
        else:
            return short

    return {**short, "cuts": cuts}


def repair_short_dict(short: dict, segments: list[tuple[float, float, str]]) -> dict:
    """Best-effort code-side fixes for a candidate short, applied before
    validate_shorts makes the final call. Doesn't guarantee validity (an
    under-length short, or one broken in some other way, passes through
    unchanged) - just improves the odds without spending another API call.
    """
    # Cuts settle before the quote does: narrowing can move the hook cut's
    # boundaries, and a quote sourced from the cut it no longer covers reads
    # as a quote from somewhere else in the video.
    short = _shrink_short_to_fit(short, segments)
    short = _narrow_cuts_to_fit(short, segments)
    short = _fix_hook_quote_if_invalid(short, segments)
    return short