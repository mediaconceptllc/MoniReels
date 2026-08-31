"""One pass that gives the transcript sentences and speakers.

duudlaga.dev returns Mongolian ASR text with no terminal punctuation and no
idea who is talking. Two consequences the rest of the pipeline has been
living with:

- There are no sentence boundaries, so "start on the first word of a real
  sentence" is a rule nothing can check and the word-splitter in
  app.ai.prompts cuts wherever the arithmetic lands.
- There is no notion of a turn, so a cut can begin with an interviewer's
  question and end in the middle of the guest's answer to a different one.

Both come from the same missing information, so both are restored in ONE
call. Splitting them would double the bill for a single read of the same
text — and the second reader would disagree with the first about where a
sentence ended, which is worse than either answer alone.

What comes back is applied ONLY as punctuation and speaker labels. The words
themselves are the transcript and are checked to be unchanged: a model asked
to punctuate will occasionally also "fix" what it thinks it misheard, and a
transcript that quietly disagrees with the audio is a subtitle that accuses
someone of saying something they did not.
"""
from __future__ import annotations

import re

from app.models import Segment, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """You restore punctuation and speaker turns in a Mongolian
transcript produced by speech recognition. The recogniser emits no
punctuation and no speaker labels.

Rules:
- Return the SAME WORDS in the SAME ORDER. Never add, remove, correct or
  reorder a word, however wrong it looks — this text is the record of what
  was said, not a draft to improve.
- Add sentence-ending punctuation (. ? !) and commas where a Mongolian
  reader would expect them.
- Number the speakers from 1 in the order they first talk. Most of these are
  interviews or conversations, so expect two; a monologue is one. Do not
  invent a speaker to make the conversation look livelier.
- A line's speaker is who says the words on THAT line.

Return JSON: {"speakers": <count>, "lines": [{"i": <line index>,
"speaker": <number>, "text": "<the same words, punctuated>"}]}"""

SCHEMA: dict = {
    "name": "punctuated_transcript",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["speakers", "lines"],
        "properties": {
            "speakers": {"type": "integer"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["i", "speaker", "text"],
                    "properties": {
                        "i": {"type": "integer"},
                        "speaker": {"type": "integer"},
                        "text": {"type": "string"},
                    },
                },
            },
        },
    },
}

_WORD = re.compile(r"\w+", re.UNICODE)


def words_of(text: str) -> list[str]:
    """The words, lowercased and stripped of everything else.

    Comparison basis for "did the model change the transcript". Case and
    punctuation are exactly what it is allowed to change, so neither may
    count as a difference.
    """
    return [w.lower() for w in _WORD.findall(text or "")]


def build_prompt(segments: list[Segment]) -> str:
    lines = "\n".join(f"[{i}] {seg.text}" for i, seg in enumerate(segments))
    return f"Punctuate these {len(segments)} lines and label the speakers:\n\n{lines}"


def apply(transcript: Transcript, answer: dict) -> tuple[Transcript, int, list[str]]:
    """Folds the model's answer back onto the transcript.

    Returns the transcript, the speaker count, and the lines that were
    REJECTED. A line whose words changed keeps its original text — one bad
    line must not cost the whole pass, and it must not silently rewrite what
    somebody said either.

    Timing is untouched throughout. Punctuation does not move when a word was
    spoken, and the segment boundaries are what every cut, subtitle and
    export in this system is built on.
    """
    by_index = {line.get("i"): line for line in answer.get("lines", []) if isinstance(line.get("i"), int)}
    rejected: list[str] = []
    segments: list[Segment] = []

    for i, seg in enumerate(transcript.segments):
        line = by_index.get(i)
        if line is None:
            segments.append(seg)
            continue

        text = (line.get("text") or "").strip()
        if not text or words_of(text) != words_of(seg.text):
            rejected.append(f"[{i}]")
            segments.append(seg)
            continue

        speaker = line.get("speaker")
        segments.append(
            seg.model_copy(
                update={
                    "text": text,
                    "speaker": f"S{speaker}" if isinstance(speaker, int) else seg.speaker,
                }
            )
        )

    if rejected:
        logger.warning(
            "%d/%d line(s) came back with different words and were left as transcribed: %s",
            len(rejected), len(transcript.segments), " ".join(rejected[:20]),
        )

    speakers = answer.get("speakers")
    if not isinstance(speakers, int) or speakers < 1:
        speakers = len({s.speaker for s in segments if s.speaker}) or 1

    return (
        transcript.model_copy(update={"segments": segments, "full_text": " ".join(s.text for s in segments)}),
        speakers,
        rejected,
    )
