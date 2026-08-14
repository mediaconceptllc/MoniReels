"""Orchestrates transcript -> Suggestions: chunking for long transcripts, schema
validation with one repair-prompt retry, and post-validation/index-resolution.
"""
from __future__ import annotations

from app.ai.openai_client import OpenAIClient
from app.ai.prompts import (
    SUGGESTIONS_SCHEMA,
    SYSTEM_PROMPT,
    build_candidates_prompt,
    build_pick_best_prompt,
    build_repair_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
    split_long_segments,
    validate_shorts,
)
from app.ai.schema import (
    RawCut,
    RawSuggestions,
    Segments,
    SuggestionValidationError,
    postprocess_suggestions,
    validate_llm_output,
)
from app.models import Suggestions, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)

YOUTUBE_MIN_VIDEO_DURATION_SEC = 1200.0


async def _complete(client: OpenAIClient, system: str, user: str, schema_name: str) -> dict:
    return await client.complete_json(system, user, SUGGESTIONS_SCHEMA["schema"], schema_name)


async def _request_validated(
    client: OpenAIClient, system: str, user: str, segments: Segments, schema_name: str = "suggestions"
) -> RawSuggestions:
    """One call + validate; on schema OR business-rule failure (validate_shorts,
    "exactly 3 shorts"), retry once with the problems appended as a repair
    prompt, then let the error propagate (job fails).
    """
    raw_json = await _complete(client, system, user, schema_name)
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
    raw_json = await _complete(client, system, retry_user, schema_name)
    raw = validate_llm_output(raw_json)
    if len(raw.shorts) != 3:
        raise SuggestionValidationError(f"Expected exactly 3 shorts, got {len(raw.shorts)}")
    remaining = validate_shorts(raw_json.get("shorts", []), segments)
    if remaining:
        raise SuggestionValidationError("; ".join(remaining))
    return raw


def _cut_summary(cuts: list[RawCut]) -> str:
    return "; ".join(f"{c.role}[{c.start_index}-{c.end_index}]" for c in cuts)


async def generate_suggestions(
    client: OpenAIClient, transcript: Transcript, duration_sec: float
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
        candidate_summaries: list[str] = []
        for chunk_lines in chunks:
            user = build_candidates_prompt(chunk_lines, duration_sec, want_youtube)
            candidates_json = await _complete(client, SYSTEM_PROMPT, user, "candidates")
            candidates = validate_llm_output(candidates_json)
            for s in candidates.shorts:
                candidate_summaries.append(f"- {s.title}: {_cut_summary(s.cuts)} — {s.why_it_works}")
            for plan in candidates.youtube:
                ranges = ", ".join(f"[{r.start_index}-{r.end_index}]" for r in plan.keep_ranges)
                candidate_summaries.append(f"- youtube keep-ranges {ranges}: {plan.throughline}")

        if not candidate_summaries:
            raise SuggestionValidationError("No candidate moments were produced from any transcript chunk")

        pick_user = build_pick_best_prompt(candidate_summaries, lines, duration_sec, want_youtube)
        raw = await _request_validated(client, SYSTEM_PROMPT, pick_user, segments)

    return postprocess_suggestions(raw, segments, duration_sec)
