"""Giving a transcript sentences and speakers, when it arrived without them.

duudlaga.dev returns Mongolian ASR text with no terminal punctuation and no
idea who is talking. Two consequences the rest of the pipeline has been
living with:

- There are no sentence boundaries, so "start on the first word of a real
  sentence" is a rule nothing can check and the word-splitter in
  app.ai.prompts cuts wherever the arithmetic lands.
- There is no notion of a turn, so a cut can begin with an interviewer's
  question and end in the middle of the guest's answer to a different one.

Both come from the same missing information, so both are restored in one
read of the text. Asking for them separately would double the bill for that
read — and the second reader would disagree with the first about where a
sentence ended, which is worse than either answer alone.

That read is split by LENGTH, never by question: the answer is the transcript
again, so a long one does not fit a single call's output budget and the whole
pass returns nothing (`plan_chunks`). Each piece is shown what the last one
decided, so one numbering of the speakers runs through all of them.

A recogniser that already punctuates and diarises — ElevenLabs Scribe does
both — makes the whole pass unnecessary, which `is_punctuated` reads off the
text rather than off the provider's name.

What comes back is applied ONLY as punctuation and speaker labels. The words
themselves are the transcript and are checked to be unchanged: a model asked
to punctuate will occasionally also "fix" what it thinks it misheard, and a
transcript that quietly disagrees with the audio is a subtitle that accuses
someone of saying something they did not.
"""
from __future__ import annotations

import re

from app.ai import boundaries
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

#: The answer is the transcript again, so the OUTPUT is what bounds a call —
#: not the prompt. Kept under app.ai.openrouter_client.MAX_TOKENS so a chunk
#: never needs that client's emergency escalation.
#:
#: This is the arithmetic that was missing. One call was asked to re-emit a
#: whole 28-minute Mongolian transcript inside a fixed 8000-token ceiling; it
#: generated for four and a half minutes and returned nothing, every time,
#: because no answer that size has ever fitted. Splitting the ask is the only
#: thing that makes it answerable.
MAX_ANSWER_TOKENS = 6000

#: Mongolian Cyrillic compresses badly — roughly two characters to a token on
#: the tokenisers behind the models this app uses. Deliberately pessimistic:
#: under-estimating costs a truncated chunk, over-estimating costs one extra
#: cheap round trip.
CHARS_PER_TOKEN = 2.0

#: `{"i": 123, "speaker": 1, "text": ""},` before a single word of transcript.
PER_LINE_TOKENS = 20

#: Lines carried into the next chunk WITH the speaker numbers already given to
#: them. Without this each chunk numbers its speakers from 1 independently and
#: "speaker 1" means a different person in every chunk — the labels would look
#: complete and be wrong, which is worse than having none.
CONTEXT_LINES = 4

_WORD = re.compile(r"\w+", re.UNICODE)

#: app.ai.boundaries' marks as a set — the same characters, asked a different
#: question ("how many are in this text" rather than "does this text end on
#: one"), which is why they are defined once over there.
_TERMINALS = frozenset(boundaries.TERMINALS)


def words_of(text: str) -> list[str]:
    """The words, lowercased and stripped of everything else.

    Comparison basis for "did the model change the transcript". Case and
    punctuation are exactly what it is allowed to change, so neither may
    count as a difference.
    """
    return [w.lower() for w in _WORD.findall(text or "")]


#: At most this many words per sentence-ending mark for the transcript to
#: count as punctuated. Not a quality bar — a discriminator between two
#: recogniser behaviours. ElevenLabs Scribe punctuates what it hears;
#: duudlaga.dev emits no terminal marks at all, so its density is exactly
#: zero and no threshold in a sane range confuses the two. Mongolian speech
#: runs 8-20 words to a sentence, so 40 leaves wide margin on both sides.
#:
#: Measured over the WHOLE text rather than per segment end. Counting how
#: many segments finish on a mark sounds equivalent and is not: a segment
#: closed by app.stt.elevenlabs_client's duration cap ends mid-sentence by
#: construction, so a speaker with long sentences drives that share down
#: while their text stays fully punctuated. MEASURED — 46-second run-ons put
#: it at 33%, below any workable threshold, and the pass would have run
#: against text that needed nothing. It also tied this answer to
#: MAX_SEGMENT_SEC: lowering the cap would have quietly started paying for
#: the pass again, with nothing naming the connection.
WORDS_PER_SENTENCE_MAX = 40


def is_punctuated(segments: list[Segment]) -> bool:
    marks = sum(1 for seg in segments for ch in (seg.text or "") if ch in _TERMINALS)
    if not marks:
        return False
    words = sum(len(words_of(seg.text)) for seg in segments)
    return words <= marks * WORDS_PER_SENTENCE_MAX


def answer_tokens(segments: list[Segment], indices: list[int]) -> int:
    """How big the answer to this chunk would be."""
    return sum(PER_LINE_TOKENS + len(segments[i].text or "") / CHARS_PER_TOKEN for i in indices)


def call_budget(segments: list[Segment], indices: list[int]) -> int:
    """The output ceiling to ask for when sending this chunk.

    Sized from the chunk rather than left at the client's default, so a single
    over-long line — the one case `plan_chunks` cannot split — still gets room
    for its own answer instead of being cut off at a constant.
    """
    return max(MAX_ANSWER_TOKENS, int(answer_tokens(segments, indices) * 1.25))


def plan_chunks(segments: list[Segment]) -> list[list[int]]:
    """Index groups whose answers each fit one call.

    A line longer than the whole budget still gets a chunk of its own: it is
    the transcript, it cannot be dropped, and the alternative is a loop that
    never places it.
    """
    chunks: list[list[int]] = []
    current: list[int] = []
    used = 0.0
    for i, seg in enumerate(segments):
        cost = PER_LINE_TOKENS + len(seg.text or "") / CHARS_PER_TOKEN
        if current and used + cost > MAX_ANSWER_TOKENS:
            chunks.append(current)
            current, used = [], 0.0
        current.append(i)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def build_prompt(
    segments: list[Segment],
    indices: list[int] | None = None,
    context: list[tuple[int, str]] | None = None,
) -> str:
    """The ask for one chunk.

    `context` is the tail of what has already been punctuated, each line with
    the speaker number it was given. It is shown, not asked for, so the model
    continues one numbering across the whole transcript instead of restarting
    at 1 in every chunk.
    """
    indices = list(range(len(segments))) if indices is None else indices
    lines = "\n".join(f"[{i}] {segments[i].text}" for i in indices)
    head = ""
    if context:
        shown = "\n".join(f"[{i}] {text}" for i, text in context)
        head = (
            "These lines came just before and are already labelled. Keep the "
            "same speaker numbers for the same people; do not return these "
            f"lines.\n\n{shown}\n\n"
        )
    return f"{head}Punctuate these {len(indices)} lines and label the speakers:\n\n{lines}"


def apply(
    transcript: Transcript, answer: dict, indices: list[int] | None = None
) -> tuple[Transcript, int, list[str]]:
    """Folds the model's answer back onto the transcript.

    Returns the transcript, the speaker count, and the lines that were
    REJECTED. A line whose words changed keeps its original text — one bad
    line must not cost the whole pass, and it must not silently rewrite what
    somebody said either.

    `indices` is the chunk this answer was asked for. A model given context
    lines will sometimes helpfully return them too; accepting those would let
    a later chunk overwrite a line an earlier one already settled, so anything
    outside the ask is ignored rather than trusted.

    Timing is untouched throughout. Punctuation does not move when a word was
    spoken, and the segment boundaries are what every cut, subtitle and
    export in this system is built on.
    """
    asked = set(range(len(transcript.segments)) if indices is None else indices)
    by_index = {
        line.get("i"): line
        for line in answer.get("lines", [])
        if isinstance(line.get("i"), int) and line.get("i") in asked
    }
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
            len(rejected), len(asked), " ".join(rejected[:20]),
        )

    speakers = answer.get("speakers")
    if not isinstance(speakers, int) or speakers < 1:
        speakers = len({s.speaker for s in segments if s.speaker}) or 1

    return (
        transcript.model_copy(update={"segments": segments, "full_text": " ".join(s.text for s in segments)}),
        speakers,
        rejected,
    )
