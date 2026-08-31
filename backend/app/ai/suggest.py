"""Orchestrates transcript -> Suggestions: chunking for long transcripts, schema
validation with one repair-prompt retry, and post-validation/index-resolution.

Long transcripts are chunked into per-portion "candidates" calls, then a final
small "pick the best 3 (by index)" call selects among them - it does NOT
re-send the transcript, since a full transcript for a long video in a
token-dense language (e.g. Mongolian) can alone exceed a low-tier OpenAI
account's tokens-per-minute cap. See build_pick_indices_prompt for detail.
"""
from __future__ import annotations

from app.ai import boundaries, punctuate
from app.ai.llm_client import LLMClient
from app.ai.prompts import (
    PICK_SCHEMA,
    SUGGESTIONS_SCHEMA,
    SYSTEM_PROMPT,
    build_candidates_prompt,
    build_pick_indices_prompt,
    build_repair_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
    repair_short_dict,
    split_long_segments,
    validate_shorts,
)
from app.ai.schema import (
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


async def _complete(client: LLMClient, system: str, user: str, schema_name: str) -> dict:
    return await client.complete_json(system, user, SUGGESTIONS_SCHEMA["schema"], schema_name)


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


async def _request_validated(
    client: LLMClient,
    system: str,
    user: str,
    segments: Segments,
    schema_name: str = "suggestions",
    sentence_ends: set[int] | None = None,
    pause_ends: set[int] | None = None,
) -> RawSuggestions:
    """One call + validate; on schema OR business-rule failure (validate_shorts,
    "exactly 3 shorts"), retry once with the problems appended as a repair
    prompt, then let the error propagate (job fails).
    """
    raw_json = _repair_shorts_json(
        await _complete(client, system, user, schema_name), segments, sentence_ends, pause_ends
    )
    problems = validate_shorts(raw_json.get("shorts", []), segments)
    if not problems:
        try:
            raw = validate_llm_output(raw_json)
            if len(raw.shorts) == 3:
                return raw
            problems = [f"Expected exactly 3 shorts, got {len(raw.shorts)}"]
        except SuggestionValidationError as e:
            problems = [str(e)]

    logger.warning("Suggestion validation failed, retrying once: %s", problems)
    retry_user = f"{user}\n\n{build_repair_prompt(problems)}"
    raw_json = _repair_shorts_json(
        await _complete(client, system, retry_user, schema_name), segments, sentence_ends, pause_ends
    )
    raw = validate_llm_output(raw_json)
    if len(raw.shorts) != 3:
        raise SuggestionValidationError(f"Expected exactly 3 shorts, got {len(raw.shorts)}")
    remaining = validate_shorts(raw_json.get("shorts", []), segments)
    if remaining:
        raise SuggestionValidationError("; ".join(remaining))
    return raw


def _cut_summary(cuts: list[RawCut]) -> str:
    return "; ".join(f"{c.role}[{c.start_index}-{c.end_index}]" for c in cuts)


async def _fetch_candidates(
    client: LLMClient,
    chunk_lines: list[str],
    duration_sec: float,
    want_youtube: bool,
    segments: Segments,
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
    user = build_candidates_prompt(chunk_lines, duration_sec, want_youtube)
    candidates_json = await _complete(client, SYSTEM_PROMPT, user, "candidates")
    candidates_json = _repair_shorts_json(candidates_json, segments, sentence_ends, pause_ends)
    problems = validate_shorts(candidates_json.get("shorts", []), segments)
    if not problems:
        return candidates_json

    logger.warning("Candidate shorts failed validation, retrying once: %s", problems)
    retry_user = f"{user}\n\n{build_repair_prompt(problems)}"
    retry_json = await _complete(client, SYSTEM_PROMPT, retry_user, "candidates")
    return _repair_shorts_json(retry_json, segments, sentence_ends, pause_ends)


async def _pick_indices(
    client: LLMClient,
    short_summaries: list[str],
    youtube_summaries: list[str],
    duration_sec: float,
    want_youtube: bool,
    n_shorts: int,
    n_youtube: int,
) -> tuple[list[int], list[int]]:
    """Selection-only call over already-built candidates (see
    build_pick_indices_prompt for why this doesn't re-send the transcript).
    Same one-retry-then-fail shape as _request_validated, but against the
    much smaller index-list schema instead of full Suggestions.
    """
    user = build_pick_indices_prompt(short_summaries, youtube_summaries, duration_sec, want_youtube)
    problems: list[str] = []
    for _attempt in range(2):
        data = await client.complete_json(SYSTEM_PROMPT, user, PICK_SCHEMA["schema"], "pick_best")
        short_idx = data.get("short_indices", [])
        yt_idx = data.get("youtube_indices", [])
        problems = []
        if len(short_idx) != 3 or len(set(short_idx)) != 3 or any(not (0 <= i < n_shorts) for i in short_idx):
            problems.append(f"short_indices must be exactly 3 distinct integers in range 0-{n_shorts - 1}.")
        if want_youtube:
            yt_valid = len(yt_idx) == 3 and len(set(yt_idx)) == 3 and all(0 <= i < n_youtube for i in yt_idx)
            if not yt_valid:
                problems.append(
                    f"youtube_indices must be exactly 3 distinct integers in range 0-{n_youtube - 1}."
                )
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
    client: LLMClient, transcript: Transcript, duration_sec: float
) -> Suggestions:
    want_youtube = duration_sec > YOUTUBE_MIN_VIDEO_DURATION_SEC

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
        user = build_suggestions_prompt(lines, duration_sec, want_youtube, speakers=speakers)
        raw = await _request_validated(
            client, SYSTEM_PROMPT, user, segments,
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

        for chunk_lines in chunks:
            candidates_json = await _fetch_candidates(
                client, chunk_lines, duration_sec, want_youtube, segments,
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

        if len(all_shorts) < 3:
            raise SuggestionValidationError(
                f"Only {len(all_shorts)} usable candidate shorts were produced across all "
                "transcript chunks; need at least 3"
            )
        if want_youtube and len(all_youtube) < 3:
            raise SuggestionValidationError(
                f"Only {len(all_youtube)} usable candidate YouTube plans were produced across "
                "all transcript chunks; need at least 3"
            )

        short_idx, yt_idx = await _pick_indices(
            client,
            short_summaries,
            youtube_summaries,
            duration_sec,
            want_youtube,
            n_shorts=len(all_shorts),
            n_youtube=len(all_youtube),
        )
        raw = RawSuggestions(
            shorts=[all_shorts[i] for i in short_idx],
            youtube=[all_youtube[i] for i in yt_idx] if want_youtube else [],
        )

    return postprocess_suggestions(raw, segments, duration_sec)
