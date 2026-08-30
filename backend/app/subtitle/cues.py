"""Transcript segments -> readable subtitle cues.

A transcript segment is whatever the STT chunking produced. With a provider
that returns no punctuation - duudlaga.dev does not - the sentence split in
`app.stt.chunking.synthesize_segments_from_text` finds nothing to cut on and
one segment is one whole audio chunk, up to DUUDLAGA_MAX_AUDIO_SEC of speech.
Measured on a real 17:44 transcript: 32 of 68 segments over 15s, the longest
at the full 30s.

As a subtitle that is unusable in a way no viewer can work around: a wall of
text parked on screen for half a minute, on screen long after it was read and
covering a third of a vertical frame while it sits there.

The limits here are the subtitle's own and deliberately not the cut planner's
(`app.ai.prompts.split_long_segments`, which allows 15s because a model only
has to choose a cut unit, not read it off a frame in real time). Both call
the same splitter with their own numbers.

Applied inside `segments_to_srt` and `build_ass_document` rather than by
their callers: those two are every subtitle this system emits, the sidecar
file and the burned-in text, and they must not be able to disagree about what
a cue is. Doing it at render time also means a transcript already stored with
coarse segments produces correct subtitles without being transcribed again.
"""
from __future__ import annotations

import uuid

from app.models import Segment
from app.utils.text import split_span

# Broadcast subtitle conventions, and the reasons they are what they are:
#
# 7s is the long-established maximum a single cue holds the screen - past it a
# viewer has read the line several times and starts to wonder whether the
# subtitle is stuck.
MAX_CUE_SEC = 7.0
# 42 characters is the standard line length, two lines the standard maximum -
# a third line starts eating the picture, and on a 9:16 frame it eats a lot.
MAX_LINE_CHARS = 42
MAX_CUE_CHARS = MAX_LINE_CHARS * 2


def _wrap(text: str) -> str:
    """Balances a cue over two lines when one would run past the frame.

    Balanced rather than filled: breaking at exactly 42 characters leaves a
    long line above a short one, which reads worse than two even lines and
    draws the eye to the ragged edge.
    """
    if len(text) <= MAX_LINE_CHARS:
        return text
    words = text.split()
    if len(words) < 2:
        return text

    best, best_cost = None, None
    for i in range(1, len(words)):
        top, bottom = " ".join(words[:i]), " ".join(words[i:])
        # Prefer even halves, but never a line that overflows on its own.
        cost = abs(len(top) - len(bottom)) + 1000 * (len(top) > MAX_LINE_CHARS)
        cost += 1000 * (len(bottom) > MAX_LINE_CHARS)
        if best_cost is None or cost < best_cost:
            best, best_cost = (top, bottom), cost
    return "\n".join(best) if best else text


def to_cues(segments: list[Segment]) -> list[Segment]:
    """Splits anything past the subtitle limits and wraps what is left.

    A segment already within the limits is returned as-is, keeping its id and
    any word timings. Pieces of a split one cannot: their word timings would
    have to be divided on an estimate, and a wrong word timing is worse than
    none, so they carry the speaker and nothing else.
    """
    cues: list[Segment] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue

        pieces = split_span(
            seg.start, seg.end, text, max_sec=MAX_CUE_SEC, max_chars=MAX_CUE_CHARS
        )
        if len(pieces) == 1:
            cues.append(seg.model_copy(update={"text": _wrap(text)}))
            continue

        cues.extend(
            Segment(
                id=uuid.uuid4().hex,
                start=start,
                end=end,
                text=_wrap(piece),
                speaker=seg.speaker,
                words=[],
            )
            for start, end, piece in pieces
        )
    return cues
