"""Orchestrates transcript -> Suggestions: chunking for long transcripts, schema
validation with one repair-prompt retry, and post-validation/index-resolution.

Long transcripts are chunked into per-portion "candidates" calls, then a final
small "pick the best 3 (by index)" call selects among them - it does NOT
re-send the transcript, since a full transcript for a long video in a
token-dense language (e.g. Mongolian) can alone exceed a low-tier OpenAI
account's tokens-per-minute cap. See build_pick_indices_prompt for detail.
"""
from __future__ import annotations

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


def _repair_shorts_json(raw_json: dict, segments: Segments) -> dict:
    """Applies repair_short_dict to every short in a raw response - a
    too-long duration or a hook_quote that drifted from a true verbatim
    substring gets fixed outright, before validate_shorts makes the final
    call on what's left.
    """
    shorts = [repair_short_dict(s, segments) for s in raw_json.get("shorts", [])]
    return {**raw_json, "shorts": shorts}


async def _request_validated(
    client: LLMClient, system: str, user: str, segments: Segments, schema_name: str = "suggestions"
) -> RawSuggestions:
    """One call + validate; on schema OR business-rule failure (validate_shorts,
    "exactly 3 shorts"), retry once with the problems appended as a repair
    prompt, then let the error propagate (job fails).
    """
    raw_json = _repair_shorts_json(await _complete(client, system, user, schema_name), segments)
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
    raw_json = _repair_shorts_json(await _complete(client, system, retry_user, schema_name), segments)
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
    client: LLMClient, chunk_lines: list[str], duration_sec: float, want_youtube: bool, segments: Segments
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
    candidates_json = _repair_shorts_json(candidates_json, segments)
    problems = validate_shorts(candidates_json.get("shorts", []), segments)
    if not problems:
        return candidates_json

    logger.warning("Candidate shorts failed validation, retrying once: %s", problems)
    retry_user = f"{user}\n\n{build_repair_prompt(problems)}"
    retry_json = await _complete(client, SYSTEM_PROMPT, retry_user, "candidates")
    return _repair_shorts_json(retry_json, segments)


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


async def generate_suggestions(
    client: LLMClient, transcript: Transcript, duration_sec: float
) -> Suggestions:
    want_youtube = duration_sec > YOUTUBE_MIN_VIDEO_DURATION_SEC
    segments = split_long_segments(transcript)
    lines = build_segment_lines(transcript)
    chunks = chunk_segment_lines(lines)

    if len(chunks) <= 1:
        user = build_suggestions_prompt(lines, duration_sec, want_youtube)
        raw = await _request_validated(client, SYSTEM_PROMPT, user, segments)
    else:
        logger.info("Transcript exceeds single-request budget; chunking into %d parts", len(chunks))
        all_shorts: list[RawShort] = []
        short_summaries: list[str] = []
        all_youtube: list[RawYoutubePlan] = []
        youtube_summaries: list[str] = []

        for chunk_lines in chunks:
            candidates_json = await _fetch_candidates(
                client, chunk_lines, duration_sec, want_youtube, segments
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
