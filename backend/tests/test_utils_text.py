"""split_span — the one splitter behind both the cut planner and the subtitles.

A production transcribe job sat on this function for twenty minutes: no error,
no network, one core burning, the job's heartbeat still moving because the spin
was inside a worker thread. It could not finish and nothing said so.
"""
from __future__ import annotations

import random
import signal
from contextlib import contextmanager

import pytest

from app.utils.text import split_span


@contextmanager
def _must_finish(seconds: int = 5):
    """Turn a hang into a failure.

    Without this a regression here does not fail the suite — it stops it, in
    CI, with no output, which is how this bug reached production in the first
    place.
    """

    def _bang(signum, frame):
        raise AssertionError(f"split_span did not return within {seconds}s — it is looping")

    previous = signal.signal(signal.SIGALRM, _bang)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _intact(pieces, start: float, end: float, text: str) -> None:
    assert " ".join(t for _, _, t in pieces).split() == text.split(), "words were lost or reordered"
    assert pieces[0][0] == start and pieces[-1][1] == end, "the original span's edges moved"
    for (_, a, _), (b, _, _) in zip(pieces, pieces[1:], strict=False):
        assert a == b, "a gap or an overlap opened between pieces"


def test_one_huge_word_among_short_ones_still_returns():
    """MEASURED, the shape that hung production: word lengths [1, 3, 80, 1].

    `lay_out` skips the groups it leaves empty, so asking it for n pieces can
    return fewer than n. The old loop derived its next request from the length
    of the RESULT, so it asked for the same number again, got the same answer,
    and never advanced.
    """
    text = " ".join(["x", "xxx", "x" * 80, "x"])

    with _must_finish():
        pieces = split_span(0.0, 10.0, text, max_sec=2.0, max_chars=20)

    _intact(pieces, 0.0, 10.0, text)


@pytest.mark.parametrize("lengths", [[1, 3, 80, 1], [1, 1, 1, 40, 3], [1, 1, 1, 40, 1]])
def test_every_stalling_shape_found_by_search_returns(lengths):
    text = " ".join("x" * n for n in lengths)
    with _must_finish():
        pieces = split_span(0.0, 12.0, text, max_sec=2.0, max_chars=15)
    _intact(pieces, 0.0, 12.0, text)


def test_no_shape_of_words_can_make_it_loop():
    """The specific shapes above were found by search; this is the rule they
    are instances of."""
    rng = random.Random(7)
    for _ in range(300):
        words = ["x" * rng.choice([1, 1, 2, 3, 12, 40, 80]) for _ in range(rng.randint(2, 8))]
        text = " ".join(words)
        with _must_finish():
            pieces = split_span(0.0, 12.0, text, max_sec=rng.choice([1.0, 2.0, 5.0]),
                                max_chars=rng.choice([None, 15, 42]))
        _intact(pieces, 0.0, 12.0, text)


def test_a_span_already_within_the_limits_is_left_alone():
    assert split_span(0.0, 2.0, "богино мөр", max_sec=7.0, max_chars=42) == [(0.0, 2.0, "богино мөр")]


def test_a_single_word_too_big_for_the_limits_comes_back_whole():
    """Losing transcript to the arithmetic is worse than one oversized piece."""
    word = "ү" * 200
    with _must_finish():
        pieces = split_span(0.0, 30.0, word, max_sec=2.0, max_chars=10)
    assert pieces == [(0.0, 30.0, word)]
