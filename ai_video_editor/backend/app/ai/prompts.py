"""Prompt construction. Transcript segments are sent as `[index] mm:ss-mm:ss text`
so the model can only ever reference timestamps that actually exist in the transcript.
"""
from __future__ import annotations

from app.models import Transcript
from app.utils.timecode import seconds_to_mmss

# Conservative character budget for a single request's transcript block. Coarse
# (char, not token) on purpose — good enough to decide "chunk or not" without
# adding a tokenizer dependency; the real ceiling is the model's context window
# and this stays well under it for any current-generation chat model.
CHAR_BUDGET = 12_000

SYSTEM_PROMPT = """You are an expert short-form video editor and YouTube strategist.
Given a timestamped transcript of a longer video, identify the most compelling moments.

Rules:
- Suggest exactly 3 short-form clips ("shorts"). Each must be a single, self-contained,
  compelling moment between 15 and 90 seconds long.
- Only use start/end timestamps that fall within the transcript you were given — never
  invent a timestamp, and never reference a moment you did not see in the transcript.
- If a YouTube long-form highlight plan is requested, choose multiple non-overlapping
  keep-ranges that together tell a coherent, engaging condensed version of the video,
  targeting a combined duration of about 600 seconds.
- Output must be valid JSON matching the provided schema exactly. No commentary."""


def build_segment_lines(transcript: Transcript) -> list[str]:
    return [
        f"[{i}] {seconds_to_mmss(seg.start)}-{seconds_to_mmss(seg.end)} {seg.text}"
        for i, seg in enumerate(transcript.segments)
    ]


def chunk_segment_lines(lines: list[str], char_budget: int = CHAR_BUDGET) -> list[list[str]]:
    """Groups lines into chunks under char_budget. Never drops a line — if a
    single line alone exceeds the budget it still becomes its own chunk, so
    nothing is silently truncated.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > char_budget:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append(current)
    return chunks


def build_suggestions_prompt(lines: list[str], duration_sec: float, want_youtube: bool) -> str:
    youtube_instruction = (
        "Also produce a YouTube long-form highlight plan (`youtube`), since this video "
        "is longer than 20 minutes."
        if want_youtube
        else "Set `youtube` to null — this video is under 20 minutes long."
    )
    transcript_block = "\n".join(lines)
    return (
        f"Video duration: {duration_sec:.1f} seconds.\n"
        f"{youtube_instruction}\n\n"
        f"Transcript segments:\n{transcript_block}"
    )


def build_candidates_prompt(lines: list[str], duration_sec: float, want_youtube: bool) -> str:
    transcript_block = "\n".join(lines)
    youtube_instruction = (
        "Also suggest candidate keep-ranges for a YouTube highlight reel from this portion "
        "of the video."
        if want_youtube
        else "Set `youtube` to null."
    )
    return (
        f"This is one portion of a longer video (total duration {duration_sec:.1f}s). "
        f"Suggest up to 3 candidate short-form clip moments from THIS PORTION ONLY. "
        f"{youtube_instruction}\n\n"
        f"Transcript segments (this portion):\n{transcript_block}"
    )


def build_pick_best_prompt(candidate_summaries: list[str], duration_sec: float, want_youtube: bool) -> str:
    youtube_instruction = (
        "Build the final YouTube long-form highlight plan from the candidate keep-ranges below, "
        "targeting a combined duration of about 600 seconds."
        if want_youtube
        else "Set `youtube` to null."
    )
    candidates_block = "\n".join(candidate_summaries)
    return (
        f"Video duration: {duration_sec:.1f} seconds. Below are candidate moments gathered from "
        f"different portions of the video. Select the best exactly 3 for the final shorts list. "
        f"{youtube_instruction}\n\n"
        f"Candidates:\n{candidates_block}"
    )
