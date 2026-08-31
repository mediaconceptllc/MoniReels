"""Where a cut is allowed to begin and end.

The system prompt has asked for "start on the first word of a real sentence
and end on the last word of one" since it was written, and nothing has ever
been able to check it — duudlaga.dev returns Mongolian ASR text with no
terminal punctuation, so there are no sentences in the transcript to start or
end on. A rule a model is asked to follow and nobody verifies is a rule that
holds when it happens to.

Two independent signals say where a thought ends, and this module uses
whichever it has:

**Punctuation**, restored by one LLM pass over the transcript
(app.ai.punctuate). Strongest, and the only one that knows meaning.

**Pauses**, measured during transcription and free — `detect_silences`
already runs to decide where to cut the audio into chunks, and a speaker
stopping for a third of a second is where a sentence ended far more often
than not. Weaker, but it costs nothing and it is available even when the LLM
step is off or has failed.

Everything here is pure: indices and floats in, indices out. The cost of
being wrong is a short that begins mid-word, so the arithmetic belongs where
a test can read it.
"""
from __future__ import annotations

import re

Segments = list[tuple[float, float, str]]

#: How far a cut edge may travel to reach a boundary. Beyond this the edit is
#: no longer the one the model chose — it picked those segments for what they
#: SAY, and dragging a cut five segments to tidy its edge silently swaps the
#: content for something else.
MAX_SNAP_SEGMENTS = 2

#: A segment end counts as pause-backed when a detected silence begins within
#: this window of it. Generous on purpose: segment times inside a chunk are
#: apportioned by character count, so they are estimates by construction and
#: an exact match would almost never happen.
PAUSE_TOLERANCE_S = 0.6

_ENDS_SENTENCE = re.compile(r"[.!?…]['\"»)\]]*\s*$")


def ends_sentence(text: str) -> bool:
    """Whether this text finishes a sentence.

    The single definition. app.ai.punctuate decides from it whether the
    transcript needs punctuating at all, and this module decides from it where
    a cut may land — two answers that must never disagree about the same line.
    """
    return bool(_ENDS_SENTENCE.search(text or ""))


def sentence_end_indices(segments: Segments) -> set[int]:
    """Segments whose text finishes a sentence.

    Empty when nothing has restored punctuation, which is the honest answer:
    with no terminal marks in the transcript there are no sentence ends to
    find, and inventing some would be worse than admitting none.
    """
    return {i for i, (_, _, text) in enumerate(segments) if ends_sentence(text)}


def pause_end_indices(
    segments: Segments, pauses: list[float], tolerance: float = PAUSE_TOLERANCE_S
) -> set[int]:
    """Segments that end where the speaker stopped.

    `pauses` are the starts of detected silences, in source seconds — the
    same measurement that decided where the audio was chunked, reused rather
    than recomputed.
    """
    if not pauses:
        return set()
    ordered = sorted(pauses)
    found: set[int] = set()
    for i, (_, end, _) in enumerate(segments):
        # A linear scan is fine: a 30-minute source has a few hundred pauses
        # and this runs once per suggestion request.
        if any(abs(p - end) <= tolerance for p in ordered):
            found.add(i)
    return found


def snap_cut(
    start_index: int,
    end_index: int,
    segments: Segments,
    *,
    sentence_ends: set[int],
    pause_ends: set[int],
    max_move: int = MAX_SNAP_SEGMENTS,
) -> tuple[int, int]:
    """Moves a cut's edges onto the nearest boundary, or leaves them alone.

    The END wants to BE a boundary — that is where the thought finishes. The
    START wants to be one PAST a boundary, because a sentence begins after
    the previous one ended.

    Sentence marks win over pauses: a pause is where someone breathed, which
    is usually but not always where a sentence ended. Neither within
    `max_move` means the cut is left exactly as the model chose it — an edge
    that cannot be tidied honestly is better left untidy than dragged onto
    content nobody picked.
    """
    if not segments:
        return start_index, end_index

    last = len(segments) - 1
    start_index = max(0, min(start_index, last))
    end_index = max(0, min(end_index, last))
    if end_index < start_index:
        start_index, end_index = end_index, start_index

    # A tie between an earlier and a later boundary takes the earlier one:
    # overshooting the 35-60s budget is the failure this pipeline actually
    # produces (see app.ai.prompts), and the shorter side of a tie is the
    # side that does not add to it.
    new_end = _nearest(end_index, sentence_ends, pause_ends, max_move, low=start_index, high=last)

    # A start sits one PAST a boundary: index i begins a sentence when i-1
    # ended one. Index 0 is NOT offered as a target even though it trivially
    # begins one — every cut starting near the top would be dragged onto the
    # video's opening seconds, which is the greeting the prompt spends a line
    # telling the model to stay out of. A cut already at 0 is left there.
    if start_index == 0:
        return 0, new_end

    starts = {i + 1 for i in sentence_ends}
    pause_starts = {i + 1 for i in pause_ends}
    new_start = _nearest(start_index, starts, pause_starts, max_move, low=0, high=new_end)

    return new_start, new_end


def _nearest(
    index: int, strong: set[int], weak: set[int], max_move: int, *, low: int, high: int
) -> int:
    """The closest allowed index within `max_move`, preferring `strong`.

    Ties go to the LATER index for an end and the EARLIER for a start by
    virtue of the caller's ranges; within this function a tie simply takes
    the first found while widening outward, which keeps the move minimal.
    """
    if index in strong:
        return index
    for distance in range(1, max_move + 1):
        for candidate in (index - distance, index + distance):
            if low <= candidate <= high and candidate in strong:
                return candidate
    if index in weak:
        return index
    for distance in range(1, max_move + 1):
        for candidate in (index - distance, index + distance):
            if low <= candidate <= high and candidate in weak:
                return candidate
    return index


def unfinished_cuts(short: dict, segments: Segments, sentence_ends: set[int]) -> list[str]:
    """Cuts that begin or end mid-sentence, named for the operator.

    Returns nothing when `sentence_ends` is empty. The rule binds exactly
    when it can be evaluated: with no punctuation in the transcript EVERY cut
    is mid-sentence, and failing every short over a signal that was never
    available would take the tool offline rather than improve it.
    """
    if not sentence_ends:
        return []

    starts = {i + 1 for i in sentence_ends} | {0}
    problems: list[str] = []
    for n, cut in enumerate(short.get("cuts", []), 1):
        s, e = cut.get("start_index"), cut.get("end_index")
        if not isinstance(s, int) or not isinstance(e, int):
            continue
        if s not in starts:
            problems.append(f"Cut {n} starts mid-sentence at segment {s}.")
        if e not in sentence_ends:
            problems.append(f"Cut {n} ends mid-sentence at segment {e}.")
    return problems
