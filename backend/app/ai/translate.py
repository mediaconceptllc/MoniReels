"""Translating a transcript into Mongolian subtitles.

A video that is not in Mongolian is transcribed in its own language
(stt.factory.for_language), and that text is the RECORD of what was said.
The translation is stored beside it on each segment — never over it — so the
original can always be read against it, corrected, and translated again.

Line by line, never merged or split. Every segment is timed to when it was
said, and cuts, subtitles and (later) a Mongolian voice all hang off those
times; a translation that re-flowed the text across lines would put a
sentence on screen while a different person is speaking. So the model is
given indices, answers by index, and anything outside the ask is ignored.

Each line carries a CHARACTER BUDGET: how much a viewer can read in the time
the line is on screen. Mongolian runs longer than English, and a line that
does not fit is a subtitle nobody finishes reading. The budget is asked for,
not enforced by truncation — cutting a sentence mid-word is worse than a line
that runs long — and every line over it is counted in the result.

Split by length, like app.ai.punctuate: the answer is the whole transcript
again, so a long one does not fit one call's output. Each chunk is shown the
last few translated lines so names and terms stay the same across chunks.

A chunk that fails costs only its own lines. Those stay untranslated — or
keep a previous translation, on a re-run — and the next run fills exactly the
gaps, so recovering from a failure is never another full bill.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.ai.llm_client import LLMClient
from app.models import Segment, Transcript
from app.utils.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = """You translate a video's transcript into Mongolian subtitles for
a Mongolian audience.

Each line is one subtitle, shown for the seconds given beside it.

Rules:
- Translate the MEANING into natural, spoken Mongolian in Cyrillic script — not
  word for word. A viewer reads it at speed, beside a moving picture.
- Return exactly one translation for EVERY line given, under the same index.
  Never merge two lines or split one: each line is timed to when it was said,
  and a merged line would stay on screen while someone else is speaking.
- Stay within the character limit shown for each line. It is how much a viewer
  can read while the line is on screen. If the meaning does not fit, keep what
  matters and drop what does not.
- Keep names, numbers, brands and titles recognisable, written the way a
  Mongolian reader expects them.
- Keep the register: how formal the speaker is, and whom they are addressing.
- Never add explanations, commentary, or text in brackets.

Return JSON: {"lines": [{"i": <line index>, "text": "<Mongolian subtitle>"}]}"""

SCHEMA = {
    "name": "translated_lines",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["lines"],
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["i", "text"],
                    "properties": {"i": {"type": "integer"}, "text": {"type": "string"}},
                },
            },
        },
    },
}

#: How much a viewer can read, in characters per second — the common broadcast
#: guideline for adult viewers. A budget, not a rule the code enforces: the
#: model is asked to fit it and every line that does not is counted.
READING_CPS = 17.0

#: Room for at least one real word, however briefly a line is on screen. A
#: 0.3-second line would otherwise be given a budget of five characters.
MIN_LINE_CHARS = 12

#: The same arithmetic as app.ai.punctuate, for the same reason: the answer is
#: the transcript again, and one call cannot return a long one.
MAX_ANSWER_TOKENS = 6000
#: Mongolian Cyrillic compresses badly, and the ANSWER is Mongolian.
CHARS_PER_TOKEN = 2.0
#: `{"i": 123, "text": ""},` before a single word of subtitle.
PER_LINE_TOKENS = 12
#: Translated lines shown to the next chunk, so a name keeps its spelling.
CONTEXT_LINES = 4


class TranslationError(Exception):
    pass


@dataclass(frozen=True)
class Report:
    """What a run did, in counts a producer can act on."""

    lines: int  # lines in the transcript
    asked: int  # lines this run tried to translate
    translated: int  # of those, how many came back
    over_budget: int  # translated, but longer than a viewer can read in time
    failed_chunks: int

    @property
    def missing(self) -> int:
        return self.asked - self.translated

    def to_dict(self) -> dict:
        return {
            "lines": self.lines,
            "asked": self.asked,
            "translated": self.translated,
            "missing": self.missing,
            "over_budget": self.over_budget,
            "failed_chunks": self.failed_chunks,
        }


def line_budget(segment: Segment) -> int:
    """Characters a viewer can read while this line is on screen."""
    return max(MIN_LINE_CHARS, round((segment.end - segment.start) * READING_CPS))


def _answer_tokens(segment: Segment) -> float:
    # The model is asked to fit the budget, but a line that runs long still has
    # to fit in the call — so the larger of the budget and the source line.
    return PER_LINE_TOKENS + max(line_budget(segment), len(segment.text or "")) / CHARS_PER_TOKEN


def plan_chunks(segments: list[Segment], indices: list[int]) -> list[list[int]]:
    """Groups of the asked lines whose answers each fit one call.

    A single line too long for the whole budget still gets a chunk of its own:
    it is part of the transcript, and dropping it would leave a hole in the
    subtitles with nothing to say why.
    """
    chunks: list[list[int]] = []
    current: list[int] = []
    used = 0.0
    for i in indices:
        cost = _answer_tokens(segments[i])
        if current and used + cost > MAX_ANSWER_TOKENS:
            chunks.append(current)
            current, used = [], 0.0
        current.append(i)
        used += cost
    if current:
        chunks.append(current)
    return chunks


def call_budget(segments: list[Segment], chunk: list[int]) -> int:
    """The output ceiling for this chunk — sized from it, so a lone over-long
    line gets room for its own answer instead of a constant that cuts it off."""
    return max(MAX_ANSWER_TOKENS, int(sum(_answer_tokens(segments[i]) for i in chunk) * 1.25))


def build_prompt(
    segments: list[Segment], chunk: list[int], context: list[tuple[int, str, str]] | None = None
) -> str:
    lines = "\n".join(
        f"[{i}] ({segments[i].end - segments[i].start:.1f}s, at most {line_budget(segments[i])} "
        f"characters) {segments[i].text}"
        for i in chunk
    )
    head = ""
    if context:
        shown = "\n".join(f"[{i}] {source}\n     → {translated}" for i, source, translated in context)
        head = (
            "These lines came just before and are already translated. Keep names and terms "
            f"the same as they are there; do not return these lines.\n\n{shown}\n\n"
        )
    return f"{head}Translate these {len(chunk)} lines:\n\n{lines}"


def apply(
    transcript: Transcript, answer: dict, chunk: list[int]
) -> tuple[Transcript, list[int], list[int]]:
    """Folds an answer onto the transcript.

    Returns the transcript, the lines translated, and those of them over their
    budget. Only lines in `chunk` are taken: a model shown context lines will
    sometimes return them too, and accepting those would let a later chunk
    overwrite what an earlier one settled. An empty answer for a line is no
    answer — the line keeps whatever it had.

    `text` and the timings are never touched. The translation is stored beside
    what was said, not over it.
    """
    asked = set(chunk)
    by_index: dict[int, str] = {}
    for line in answer.get("lines", []) if isinstance(answer, dict) else []:
        i = line.get("i") if isinstance(line, dict) else None
        text = (line.get("text") or "").strip() if isinstance(line, dict) else ""
        if isinstance(i, int) and not isinstance(i, bool) and i in asked and text:
            by_index[i] = text

    segments = list(transcript.segments)
    over: list[int] = []
    for i, text in by_index.items():
        segments[i] = segments[i].model_copy(update={"translation": text})
        if len(text) > line_budget(segments[i]):
            over.append(i)
    return transcript.model_copy(update={"segments": segments}), sorted(by_index), sorted(over)


async def translate_transcript(
    client: LLMClient,
    transcript: Transcript,
    *,
    force: bool = False,
    on_progress: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[Transcript, Report]:
    """Translates the lines that need it — or every line, with `force`.

    Without `force` only lines with no translation are sent, which is what
    makes a failed chunk cheap to recover: the next run is billed for the gap,
    not the transcript. With it, every line is sent again — and a line whose
    chunk fails this time keeps the translation it had, because a failure
    must never leave a line worse off than it started.
    """
    segments = transcript.segments
    todo = [
        i for i, seg in enumerate(segments)
        if (seg.text or "").strip() and (force or not seg.translation)
    ]
    if not todo:
        return transcript, Report(len(segments), 0, 0, 0, 0)

    chunks = plan_chunks(segments, todo)
    if len(chunks) > 1:
        logger.info("Translating %d line(s) in %d chunks", len(todo), len(chunks))

    translated: list[int] = []
    over: list[int] = []
    failed = 0
    context: list[tuple[int, str, str]] = []
    for n, chunk in enumerate(chunks, 1):
        try:
            answer = await client.complete_json(
                SYSTEM_PROMPT,
                build_prompt(transcript.segments, chunk, context),
                SCHEMA["schema"],
                SCHEMA["name"],
                max_tokens=call_budget(transcript.segments, chunk),
            )
        except Exception:  # noqa: BLE001 - one chunk's failure must not cost the others
            logger.exception("Could not translate lines %d-%d; they stay as they were", chunk[0], chunk[-1])
            failed += 1
            continue

        transcript, done, long = apply(transcript, answer, chunk)
        translated += done
        over += long
        if len(done) < len(chunk):
            logger.warning(
                "%d of %d line(s) came back without a translation",
                len(chunk) - len(done), len(chunk),
            )
        context = [
            (i, transcript.segments[i].text, transcript.segments[i].translation or "")
            for i in chunk[-CONTEXT_LINES:]
            if transcript.segments[i].translation
        ]
        if on_progress:
            await on_progress(n / len(chunks))

    if not translated:
        raise TranslationError(f"None of the {len(todo)} line(s) could be translated")

    report = Report(len(segments), len(todo), len(translated), len(over), failed)
    if report.missing or report.over_budget:
        logger.info(
            "Translated %d/%d line(s); %d missing, %d over their reading budget",
            report.translated, report.asked, report.missing, report.over_budget,
        )
    return transcript, report
