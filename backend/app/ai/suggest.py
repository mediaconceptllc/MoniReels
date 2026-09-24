"""Orchestrates transcript -> Suggestions: chunking for long transcripts, schema
validation with one repair-prompt retry, and post-validation/index-resolution.

Long transcripts are chunked into per-portion "candidates" calls, then a final
small "pick the best N (by index)" call selects among them - it does NOT
re-send the transcript, since a full transcript for a long video in a
token-dense language (e.g. Mongolian) can alone exceed a low-tier OpenAI
account's tokens-per-minute cap. See build_pick_indices_prompt for detail.

N is the producer's choice (app.ai.schema.count_limits bounds it). The answer
can hold FEWER than N — the model is told to stop short rather than pad, and a
short that still breaks a rule after the repair retry is dropped rather than
allowed to fail the job. Fewer is recorded on the result, never hidden.
"""
from __future__ import annotations

import math

from app.ai import boundaries, punctuate
from app.ai.llm_client import LLMClient
from app.ai.prompts import (
    SYSTEM_PROMPT,
    build_candidates_prompt,
    build_pick_indices_prompt,
    build_repair_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
    pick_schema,
    repair_short_dict,
    split_long_segments,
    suggestions_schema,
    validate_shorts,
)
from app.ai.schema import (
    DEFAULT_SHORT_COUNT,
    DEFAULT_YOUTUBE_COUNT,
    RawCut,
    RawShort,
    RawSuggestions,
    RawYoutubePlan,
    Segments,
    SuggestionValidationError,
    postprocess_suggestions,
    validate_llm_output,
)
from app.models import Suggestions, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)

YOUTUBE_MIN_VIDEO_DURATION_SEC = 1200.0

# The chunked pipeline asks every portion for at least this many candidates —
# what it asked for when the answer was always three, so a request for three
# behaves exactly as it did.
MIN_CANDIDATES_PER_CHUNK = 3
# Room the pool keeps above the number wanted: a candidate can still be
# dropped by validation, and the picker needs a choice, not a tally.
CANDIDATE_SLACK = 2


def candidates_per_chunk(wanted: int, chunks: int) -> int:
    """How many candidates each portion is asked for.

    A fixed three per portion made a request for eight on a two-portion video
    impossible — six candidates cannot yield eight picks — so the ask grows
    with the count. It is spread across the portions rather than asked of each
    in full, because a portion holds only some of the video's stories and
    every candidate is paid output.
    """
    return max(MIN_CANDIDATES_PER_CHUNK, math.ceil((wanted + CANDIDATE_SLACK) / max(1, chunks)))


async def _complete(client: LLMClient, system: str, user: str, schema: dict) -> dict:
    return await client.complete_json(system, user, schema["schema"], schema["name"])


def _repair_shorts_json(
    raw_json: dict,
    segments: Segments,
    sentence_ends: set[int] | None = None,
    pause_ends: set[int] | None = None,
) -> dict:
    """Applies repair_short_dict to every short in a raw response - a cut
    edge landing mid-sentence, a too-long duration, or a hook_quote that
    drifted from a true verbatim substring gets fixed outright, before
    validate_shorts makes the final call on what's left.
    """
    shorts = [
        repair_short_dict(s, segments, sentence_ends, pause_ends)
        for s in raw_json.get("shorts", [])
    ]
    return {**raw_json, "shorts": shorts}


def _count_problems(raw: RawSuggestions, shorts: int) -> list[str]:
    """FEWER than asked is not a problem — the model was told to stop short
    rather than pad. None at all is, and so is more: that is not following
    the request."""
    if not raw.shorts:
        return [f"Return at least 1 short (up to {shorts})."]
    if len(raw.shorts) > shorts:
        return [f"Return at most {shorts} shorts; got {len(raw.shorts)}."]
    return []


async def _request_validated(
    client: LLMClient,
    system: str,
    user: str,
    segments: Segments,
    *,
    shorts: int = DEFAULT_SHORT_COUNT,
    youtube: int = DEFAULT_YOUTUBE_COUNT,
    sentence_ends: set[int] | None = None,
    pause_ends: set[int] | None = None,
) -> RawSuggestions:
    """One call + validate; on a schema, rule or count failure, retry once
    with the problems appended as a repair prompt.

    What STILL breaks a rule after that retry is dropped, and the rest are
    kept. Every remaining problem used to fail the job, discarding the valid
    shorts with the invalid one — tolerable at three, not at eight, where the
    odds of every short surviving fall fast and the producer would be billed
    for nothing. Only an answer with no usable short at all is an error.
    """
    schema = suggestions_schema(shorts, youtube)
    raw_json = _repair_shorts_json(
        await _complete(client, system, user, schema), segments, sentence_ends, pause_ends
    )
    problems = validate_shorts(raw_json.get("shorts", []), segments)
    if not problems:
        try:
            raw = validate_llm_output(raw_json)
            problems = _count_problems(raw, shorts)
            if not problems:
                return raw
        except SuggestionValidationError as e:
            problems = [str(e)]

    logger.warning("Suggestion validation failed, retrying once: %s", problems)
    retry_user = f"{user}\n\n{build_repair_prompt(problems)}"
    raw_json = _repair_shorts_json(
        await _complete(client, system, retry_user, schema), segments, sentence_ends, pause_ends
    )
    returned = raw_json.get("shorts", [])
    usable = [s for s in returned if not validate_shorts([s], segments)]
    if not usable:
        remaining = validate_shorts(returned, segments)
        raise SuggestionValidationError("; ".join(remaining) or "No short came back at all")
    if len(usable) < len(returned):
        logger.warning(
            "%d of %d short(s) still broke a rule after the repair retry; kept the rest",
            len(returned) - len(usable), len(returned),
        )
    return validate_llm_output({**raw_json, "shorts": usable[:shorts]})


def _cut_summary(cuts: list[RawCut]) -> str:
    return "; ".join(f"{c.role}[{c.start_index}-{c.end_index}]" for c in cuts)


async def _fetch_candidates(
    client: LLMClient,
    chunk_lines: list[str],
    duration_sec: float,
    segments: Segments,
    *,
    candidates: int = MIN_CANDIDATES_PER_CHUNK,
    youtube_candidates: int = 0,
    sentence_ends: set[int] | None = None,
    pause_ends: set[int] | None = None,
) -> dict:
    """One candidates call for a chunk, with the same one-retry-via-repair-
    prompt shape as _request_validated. Without this, a candidate that's a
    near-miss on duration (observed: Claude models producing consistently
    60-100% too-long cuts here, even though the same model gets it right on
    the final pick-and-recut pass) was silently discarded instead of being
    handed the same "cut N to M seconds" repair guidance the final pass
    already gets - on a video where every chunk overshoots, that starves the
    picker down to 0-1 usable candidates instead of the normal 4-6.
    """
    user = build_candidates_prompt(
        chunk_lines, duration_sec, candidates=candidates, want_youtube=youtube_candidates > 0
    )
    schema = suggestions_schema(candidates, youtube_candidates, exact=True)
    schema = {**schema, "name": "candidates"}
    candidates_json = await _complete(client, SYSTEM_PROMPT, user, schema)
    candidates_json = _repair_shorts_json(candidates_json, segments, sentence_ends, pause_ends)
    problems = validate_shorts(candidates_json.get("shorts", []), segments)
    if not problems:
        return candidates_json

    logger.warning("Candidate shorts failed validation, retrying once: %s", problems)
    retry_user = f"{user}\n\n{build_repair_prompt(problems)}"
    retry_json = await _complete(client, SYSTEM_PROMPT, retry_user, schema)
    return _repair_shorts_json(retry_json, segments, sentence_ends, pause_ends)


def _index_problems(name: str, picked: list, wanted: int, pool: int, *, at_least: int) -> list[str]:
    distinct = len(set(picked)) == len(picked)
    in_range = all(isinstance(i, int) and 0 <= i < pool for i in picked)
    if at_least <= len(picked) <= wanted and distinct and in_range:
        return []
    low = f"{at_least}-{wanted}" if at_least < wanted else f"{wanted}"
    return [f"{name} must be {low} distinct integers in range 0-{pool - 1}."]


async def _pick_indices(
    client: LLMClient,
    short_summaries: list[str],
    youtube_summaries: list[str],
    duration_sec: float,
    *,
    shorts: int,
    youtube: int,
    pool_shorts: int,
    pool_youtube: int,
) -> tuple[list[int], list[int]]:
    """Selection-only call over already-built candidates (see
    build_pick_indices_prompt for why this doesn't re-send the transcript).
    Same one-retry-then-fail shape as _request_validated, but against the
    much smaller index-list schema instead of full Suggestions.

    `shorts`/`youtube` are how many to choose; `pool_*` how many there are to
    choose FROM. They were one pair of names used for both, which is how the
    number to pick stayed a literal 3.
    """
    user = build_pick_indices_prompt(
        short_summaries, youtube_summaries, duration_sec, shorts=shorts, youtube=youtube
    )
    schema = pick_schema(shorts, youtube)
    problems: list[str] = []
    for _attempt in range(2):
        data = await client.complete_json(SYSTEM_PROMPT, user, schema["schema"], schema["name"])
        short_idx = data.get("short_indices", [])
        yt_idx = data.get("youtube_indices", [])
        problems = _index_problems("short_indices", short_idx, shorts, pool_shorts, at_least=1)
        if youtube:
            problems += _index_problems("youtube_indices", yt_idx, youtube, pool_youtube, at_least=0)
        elif yt_idx:
            problems.append("youtube_indices must be an empty list for this video.")

        if not problems:
            return short_idx, yt_idx
        logger.warning("Pick-indices validation failed, retrying once: %s", problems)
        user = f"{user}\n\n{build_repair_prompt(problems)}"

    raise SuggestionValidationError("; ".join(problems))


async def restore_sentences(client: LLMClient, transcript: Transcript) -> tuple[Transcript, int]:
    """Gives the transcript sentences and speaker turns, if it hasn't got them.

    Runs before the cuts are chosen, because everything downstream is better
    for it: `split_long_segments` finds sentence boundaries instead of
    packing words, subtitles break where a thought breaks, and
    app.ai.boundaries can finally CHECK the prompt's oldest rule — "start on
    the first word of a real sentence".

    Skipped outright when the recogniser already did it. ElevenLabs Scribe
    returns punctuated text and measures who spoke each word, so re-deriving
    both from the words alone is a paid call that can only agree or be wrong —
    and it is the most expensive call in the job, because its answer is the
    whole transcript again.

    Never fatal, and never destructive. A transcript that could not be
    punctuated is exactly the transcript this pipeline has always worked with,
    so a failure here costs quality and nothing else — but it must not cost
    what was already known: a failed chunk keeps its lines as transcribed and
    a failed pass keeps the speaker count the recogniser measured.
    """
    measured = transcript.speakers
    if not transcript.segments:
        return transcript, measured

    if punctuate.is_punctuated(transcript.segments) and measured:
        logger.info(
            "Transcript arrived punctuated with %d speaker(s); skipping the punctuation pass",
            measured,
        )
        return transcript, measured

    chunks = punctuate.plan_chunks(transcript.segments)
    if len(chunks) > 1:
        logger.info(
            "Punctuating %d line(s) in %d chunks; one answer would not fit a single call",
            len(transcript.segments), len(chunks),
        )

    restored = transcript
    done = 0
    context: list[tuple[int, str]] = []
    for chunk in chunks:
        try:
            answer = await client.complete_json(
                punctuate.SYSTEM_PROMPT,
                punctuate.build_prompt(restored.segments, chunk, context),
                punctuate.SCHEMA["schema"],
                punctuate.SCHEMA["name"],
                max_tokens=punctuate.call_budget(restored.segments, chunk),
            )
        except Exception:  # noqa: BLE001 - quality step; the pipeline works without it
            logger.exception(
                "Could not punctuate lines %d-%d; keeping them as transcribed", chunk[0], chunk[-1]
            )
            continue

        restored, _, rejected = punctuate.apply(restored, answer, chunk)
        done += len(chunk) - len(rejected)
        # What the next chunk is shown, with the numbers this one gave out.
        context = [
            (i, f"{restored.segments[i].speaker or '?'}: {restored.segments[i].text}")
            for i in chunk[-punctuate.CONTEXT_LINES :]
        ]

    speakers = len({s.speaker for s in restored.segments if s.speaker}) or measured
    logger.info(
        "Punctuation restored on %d/%d line(s); %d speaker(s) heard",
        done, len(transcript.segments), speakers,
    )
    return restored.model_copy(update={"speakers": speakers}), speakers


async def generate_suggestions(
    client: LLMClient,
    transcript: Transcript,
    duration_sec: float,
    *,
    shorts: int = DEFAULT_SHORT_COUNT,
    youtube: int = DEFAULT_YOUTUBE_COUNT,
) -> Suggestions:
    # What is actually asked for. The duration gate is not the producer's to
    # lift, and a request for none is honoured on a long video too.
    youtube = youtube if duration_sec > YOUTUBE_MIN_VIDEO_DURATION_SEC else 0
    want_youtube = youtube > 0

    # Before anything is chosen: the cuts, the subtitles and the boundary
    # check all read this text, and until it has sentences none of them can
    # do their job properly.
    transcript, speakers = await restore_sentences(client, transcript)

    segments = split_long_segments(transcript)
    lines = build_segment_lines(transcript)
    chunks = chunk_segment_lines(lines)

    # Two independent signals about where a thought ends. Punctuation is the
    # strong one and exists only if the call above worked; pauses were
    # measured during transcription and cost nothing. See app.ai.boundaries.
    sentence_ends = boundaries.sentence_end_indices(segments)
    pause_ends = boundaries.pause_end_indices(segments, transcript.pauses)
    logger.info(
        "Cut boundaries available: %d sentence end(s), %d pause-backed, %d speaker(s)",
        len(sentence_ends), len(pause_ends), speakers,
    )

    # The cut unit's size is what decides whether the 35-60s rule is reachable
    # at all: a short needs at least 3 cuts, so 3x the shortest segment is the
    # floor no prompt or retry can get under. Coarse segments are invisible in
    # the failure - it surfaces as the model "ignoring" a duration rule - so
    # the number that explains it is logged before the first call is paid for.
    if segments:
        spans = sorted(e - s for s, e, _ in segments)
        logger.info(
            "%d segment(s) for cutting: shortest %.1fs, median %.1fs, longest %.1fs "
            "(a short needs 3+ cuts, so its floor here is ~%.0fs)",
            len(spans),
            spans[0],
            spans[len(spans) // 2],
            spans[-1],
            sum(spans[:3]),
        )

    if len(chunks) <= 1:
        user = build_suggestions_prompt(
            lines, duration_sec, shorts=shorts, youtube=youtube, speakers=speakers
        )
        raw = await _request_validated(
            client, SYSTEM_PROMPT, user, segments,
            shorts=shorts, youtube=youtube,
            sentence_ends=sentence_ends, pause_ends=pause_ends,
        )
        # Snapping fixes what it can reach; anything left is worth seeing.
        # It is NOT a validation failure: with punctuation restored on only
        # part of a transcript, hard-failing here would reject shorts that
        # are otherwise sound.
        for i, short in enumerate(raw.shorts, 1):
            for problem in boundaries.unfinished_cuts(
                short.model_dump(), segments, sentence_ends
            ):
                logger.info("Short %d: %s", i, problem)
    else:
        logger.info("Transcript exceeds single-request budget; chunking into %d parts", len(chunks))
        all_shorts: list[RawShort] = []
        short_summaries: list[str] = []
        all_youtube: list[RawYoutubePlan] = []
        youtube_summaries: list[str] = []

        per_chunk = candidates_per_chunk(shorts, len(chunks))
        per_chunk_youtube = candidates_per_chunk(youtube, len(chunks)) if want_youtube else 0
        for chunk_lines in chunks:
            candidates_json = await _fetch_candidates(
                client, chunk_lines, duration_sec, segments,
                candidates=per_chunk, youtube_candidates=per_chunk_youtube,
                sentence_ends=sentence_ends, pause_ends=pause_ends,
            )
            candidates = validate_llm_output(candidates_json)
            for i, short_dict in enumerate(candidates_json.get("shorts", [])):
                # Drop structurally-broken candidates here rather than let one
                # bad chunk kill the whole job - only sound candidates are
                # ever offered to the picker below.
                if validate_shorts([short_dict], segments):
                    continue
                s = candidates.shorts[i]
                summary = f"[{len(all_shorts)}] {s.title}: {_cut_summary(s.cuts)} — {s.why_it_works}"
                short_summaries.append(summary)
                all_shorts.append(s)
            for plan in candidates.youtube:
                ranges = ", ".join(f"[{r.start_index}-{r.end_index}]" for r in plan.keep_ranges)
                youtube_summaries.append(f"[{len(all_youtube)}] keep-ranges {ranges}: {plan.throughline}")
                all_youtube.append(plan)

        # Nothing usable at all is the one real failure. Fewer usable than
        # asked for is not: it used to fail the whole job, billing the
        # producer for every portion and returning nothing.
        if not all_shorts:
            raise SuggestionValidationError(
                "No usable candidate short was produced across any transcript chunk"
            )
        pick_shorts = min(shorts, len(all_shorts))
        pick_youtube = min(youtube, len(all_youtube))
        if pick_shorts < shorts or pick_youtube < youtube:
            logger.info(
                "Pool holds %d short(s) and %d plan(s) for %d and %d asked",
                len(all_shorts), len(all_youtube), shorts, youtube,
            )

        if len(all_shorts) <= pick_shorts and len(all_youtube) <= pick_youtube:
            # Nothing to choose between: every candidate is going in anyway,
            # and a paid call to rank them would change nothing but the bill.
            short_idx = list(range(len(all_shorts)))
            yt_idx = list(range(len(all_youtube)))
        else:
            short_idx, yt_idx = await _pick_indices(
                client,
                short_summaries,
                youtube_summaries,
                duration_sec,
                shorts=pick_shorts,
                youtube=pick_youtube,
                pool_shorts=len(all_shorts),
                pool_youtube=len(all_youtube),
            )
        raw = RawSuggestions(
            shorts=[all_shorts[i] for i in short_idx],
            youtube=[all_youtube[i] for i in yt_idx] if want_youtube else [],
        )

    return postprocess_suggestions(raw, segments, duration_sec, shorts=shorts, youtube=youtube)
