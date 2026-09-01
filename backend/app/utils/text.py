"""Cutting a timed span of text into smaller timed spans.

One piece of arithmetic with two callers that would otherwise each grow their
own copy: the cut planner (app.ai.prompts) needs units small enough for a
model to choose between, and the subtitle renderers (app.subtitle.cues) need
cues short enough to read. Both start from the same problem — a transcript
segment far longer than its consumer can use — and both solve it the same
way, so the split lives here and the limits stay with whoever knows them.

Timing inside a split is an estimate: the STT providers here return no word
timings, so a piece's share of the span is proportional to its share of the
characters. Good enough to choose a cut or hang a subtitle on, and the
original span's start and end are preserved exactly.
"""
from __future__ import annotations

from math import ceil


def split_span(
    start: float,
    end: float,
    text: str,
    *,
    max_sec: float,
    max_chars: int | None = None,
) -> list[tuple[float, float, str]]:
    """Splits one timed span into the fewest even pieces that fit the limits.

    Even, not greedy: filling each piece to the limit leaves the remainder as
    a stray tail — a 15.4s span becomes 15.0s + 0.4s — and a one-word
    fragment is no use as either a cut unit or a subtitle.

    Words are atomic. A span of a single word too big for the limits comes
    back whole rather than mangled: losing transcript to the arithmetic is
    worse than one oversized piece.
    """
    words = text.split()
    span = end - start

    def over(piece_span: float, piece_chars: int) -> bool:
        return piece_span > max_sec or (max_chars is not None and piece_chars > max_chars)

    if len(words) < 2 or not over(span, len(text)):
        return [(start, end, text)]

    total_chars = sum(len(w) for w in words) or 1

    def lay_out(n: int) -> list[tuple[float, float, str]]:
        groups: list[list[str]] = [[] for _ in range(n)]
        cum = 0
        for word in words:
            # The word's midpoint decides its piece, so a word straddling a
            # boundary goes to the side holding more of it.
            groups[min(n - 1, int((cum + len(word) / 2) * n / total_chars))].append(word)
            cum += len(word)

        out: list[tuple[float, float, str]] = []
        cursor, chars = start, 0
        for k, group in enumerate(groups):
            if not group:
                continue
            chars += sum(len(w) for w in group)
            # The tail ends exactly on the original end, so float drift
            # accumulated across the shares can never leave a gap or an
            # overhang at the boundary.
            stop = end if k == n - 1 else start + span * (chars / total_chars)
            out.append((cursor, stop, " ".join(group)))
            cursor = stop
        return out

    wanted = ceil(span / max_sec)
    if max_chars is not None:
        wanted = max(wanted, ceil(len(text) / max_chars))

    # An even split can still overshoot by part of one word; one more piece is
    # the fix. Bounded by one piece per word.
    #
    # Count UP from the number asked for, never from the number returned.
    # lay_out skips groups it left empty, so it can hand back fewer pieces
    # than it was asked for — and re-deriving the next request from the
    # length of the result then asks for the same number again, gets the same
    # answer, and never advances. MEASURED: a span of words sized
    # [1, 3, 80, 1] does exactly that, and one production transcribe sat on it
    # burning a core with the job alive and no error, because the spin is
    # inside a worker thread and the heartbeat kept moving.
    pieces = lay_out(max(1, wanted))
    for n in range(max(1, wanted) + 1, len(words) + 1):
        if not any(over(e - s, len(t)) for s, e, t in pieces):
            break
        pieces = lay_out(n)
    return pieces
