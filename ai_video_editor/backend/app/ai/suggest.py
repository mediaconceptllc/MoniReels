"""Orchestrates transcript -> Suggestions: chunking for long transcripts, schema
validation with one retry, and post-validation/auto-correction.
"""
from __future__ import annotations

from app.ai.openai_client import OpenAIClient
from app.ai.prompts import (
    SYSTEM_PROMPT,
    build_candidates_prompt,
    build_pick_best_prompt,
    build_segment_lines,
    build_suggestions_prompt,
    chunk_segment_lines,
)
from app.ai.schema import (
    SUGGESTIONS_JSON_SCHEMA,
    RawSuggestions,
    SuggestionValidationError,
    postprocess_suggestions,
    validate_llm_output,
)
from app.models import Suggestions, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)

YOUTUBE_MIN_VIDEO_DURATION_SEC = 1200.0


async def _request_validated(client: OpenAIClient, system: str, user: str) -> RawSuggestions:
    """One call + validate; on schema OR "exactly 3 shorts" failure, retry once
    with the error appended to the prompt, then let the error propagate (job fails).
    """
    raw_json = await client.complete_json(system, user, SUGGESTIONS_JSON_SCHEMA, "suggestions")
    try:
        raw = validate_llm_output(raw_json)
        if len(raw.shorts) != 3:
            raise SuggestionValidationError(f"Expected exactly 3 shorts, got {len(raw.shorts)}")
        return raw
    except SuggestionValidationError as e:
        logger.warning("Suggestion validation failed, retrying once: %s", e)
        retry_user = f"{user}\n\nYour previous response was invalid: {e}\nFix this and return corrected JSON."
        raw_json = await client.complete_json(system, retry_user, SUGGESTIONS_JSON_SCHEMA, "suggestions")
        raw = validate_llm_output(raw_json)
        if len(raw.shorts) != 3:
            raise SuggestionValidationError(f"Expected exactly 3 shorts, got {len(raw.shorts)}") from e
        return raw


async def generate_suggestions(
    client: OpenAIClient, transcript: Transcript, duration_sec: float
) -> Suggestions:
    want_youtube = duration_sec > YOUTUBE_MIN_VIDEO_DURATION_SEC
    lines = build_segment_lines(transcript)
    chunks = chunk_segment_lines(lines)

    if len(chunks) <= 1:
        user = build_suggestions_prompt(lines, duration_sec, want_youtube)
        raw = await _request_validated(client, SYSTEM_PROMPT, user)
    else:
        logger.info("Transcript exceeds single-request budget; chunking into %d parts", len(chunks))
        candidate_summaries: list[str] = []
        for chunk_lines in chunks:
            user = build_candidates_prompt(chunk_lines, duration_sec, want_youtube)
            candidates_json = await client.complete_json(
                SYSTEM_PROMPT, user, SUGGESTIONS_JSON_SCHEMA, "candidates"
            )
            candidates = validate_llm_output(candidates_json)
            for s in candidates.shorts:
                candidate_summaries.append(
                    f"- {s.start:.1f}-{s.end:.1f}s: {s.title} — {s.hook} ({s.description})"
                )
            if candidates.youtube:
                for r in candidates.youtube.ranges:
                    candidate_summaries.append(f"- keep-range {r.start:.1f}-{r.end:.1f}s: {r.reason}")

        if not candidate_summaries:
            raise SuggestionValidationError("No candidate moments were produced from any transcript chunk")

        pick_user = build_pick_best_prompt(candidate_summaries, duration_sec, want_youtube)
        raw = await _request_validated(client, SYSTEM_PROMPT, pick_user)

    return postprocess_suggestions(raw, duration_sec)
