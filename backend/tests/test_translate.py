"""Translating a transcript into Mongolian subtitles, line by line.

What these hold: the translation lands BESIDE what was said and never over
it; the model's answer is taken only for the lines asked; a failed chunk
costs only its own lines; and a second run is billed for the gaps, not the
transcript.
"""
from __future__ import annotations

import pytest

from app.ai import translate
from app.ai.translate import (
    MAX_ANSWER_TOKENS,
    MIN_LINE_CHARS,
    READING_CPS,
    TranslationError,
    apply,
    build_prompt,
    call_budget,
    line_budget,
    plan_chunks,
    translate_transcript,
)
from app.models import Segment, Transcript, Word


def _seg(i: int, text: str = "Hello there", start: float | None = None, dur: float = 3.0, **kw) -> Segment:
    start = float(i * 3) if start is None else start
    return Segment(id=f"s{i}", start=start, end=start + dur, text=text, **kw)


def _transcript(n: int, **kw) -> Transcript:
    segments = [_seg(i, **kw) for i in range(n)]
    return Transcript(language="eng", segments=segments, full_text=" ".join(s.text for s in segments))


class FakeClient:
    """Answers each call by translating every line it was asked for, unless
    told to fail a particular call number."""

    def __init__(self, fail_calls: set[int] | None = None, reply=None):
        self.calls: list[dict] = []
        self.fail_calls = fail_calls or set()
        self.reply = reply

    async def complete_json(self, system, user, schema, name, max_tokens=None, temperature=0.4):
        self.calls.append({"system": system, "user": user, "name": name, "max_tokens": max_tokens})
        n = len(self.calls)
        if n in self.fail_calls:
            raise RuntimeError("provider said no")
        if self.reply is not None:
            return self.reply
        body = user.split("Translate these")[1]
        asked = [int(line[1 : line.index("]")]) for line in body.splitlines() if line.startswith("[")]
        return {"lines": [{"i": i, "text": f"мн-{i}-{n}"} for i in asked]}


# --------------------------------------------------------------------------
# The reading budget
# --------------------------------------------------------------------------

def test_a_line_gets_what_a_viewer_can_read_while_it_is_on_screen():
    assert line_budget(_seg(0, dur=4.0)) == round(4.0 * READING_CPS)


def test_a_line_on_screen_for_a_blink_still_has_room_for_a_word():
    assert line_budget(_seg(0, dur=0.2)) == MIN_LINE_CHARS


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------

def test_only_the_lines_asked_for_are_planned():
    t = _transcript(10)
    chunks = plan_chunks(t.segments, [1, 4, 7])
    assert [i for c in chunks for i in c] == [1, 4, 7]


def test_a_transcript_too_big_for_one_answer_is_split(monkeypatch):
    monkeypatch.setattr(translate, "MAX_ANSWER_TOKENS", 200)
    t = _transcript(40)
    chunks = plan_chunks(t.segments, list(range(40)))
    assert len(chunks) > 1
    assert [i for c in chunks for i in c] == list(range(40))


def test_one_line_bigger_than_the_whole_budget_still_gets_its_own_chunk(monkeypatch):
    monkeypatch.setattr(translate, "MAX_ANSWER_TOKENS", 50)
    t = Transcript(language="eng", segments=[_seg(0, "word " * 400, dur=90.0), _seg(1)], full_text="x")
    chunks = plan_chunks(t.segments, [0, 1])
    assert [0] in chunks and [i for c in chunks for i in c] == [0, 1]


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

def test_every_line_carries_its_time_on_screen_and_its_budget():
    t = _transcript(2)
    prompt = build_prompt(t.segments, [0, 1])
    assert f"[0] (3.0s, at most {line_budget(t.segments[0])} characters) Hello there" in prompt


def test_the_next_chunk_is_shown_what_the_last_one_decided():
    """Without it a name is spelt one way in the first five minutes of
    subtitles and another way in the next five."""
    t = _transcript(3)
    prompt = build_prompt(t.segments, [2], context=[(1, "Meet John", "Жонтой танилц")])
    assert "Meet John" in prompt and "Жонтой танилц" in prompt
    assert "do not return these lines" in prompt


# --------------------------------------------------------------------------
# Applying an answer
# --------------------------------------------------------------------------

def test_the_translation_lands_beside_what_was_said_never_over_it():
    t = _transcript(2)
    words = [Word(text="Hello", start=0.0, end=0.5)]
    t.segments[0] = t.segments[0].model_copy(update={"words": words})
    out, done, _ = apply(t, {"lines": [{"i": 0, "text": "Сайн уу"}]}, [0, 1])

    assert out.segments[0].translation == "Сайн уу"
    assert out.segments[0].text == "Hello there"
    assert out.segments[0].words == words
    assert (out.segments[0].start, out.segments[0].end) == (0.0, 3.0)
    assert done == [0]


def test_an_answer_for_a_line_not_asked_for_is_ignored():
    """A model shown context lines sometimes returns them too; accepting that
    would let a later chunk overwrite what an earlier one settled."""
    t = _transcript(3)
    out, done, _ = apply(t, {"lines": [{"i": 0, "text": "а"}, {"i": 2, "text": "б"}]}, [2])
    assert out.segments[0].translation is None
    assert done == [2]


def test_an_empty_answer_for_a_line_is_no_answer():
    t = _transcript(1)
    t.segments[0] = t.segments[0].model_copy(update={"translation": "хуучин"})
    out, done, _ = apply(t, {"lines": [{"i": 0, "text": "   "}]}, [0])
    assert out.segments[0].translation == "хуучин"
    assert done == []


def test_a_line_longer_than_a_viewer_can_read_is_kept_and_counted():
    """Truncating it would cut a sentence mid-word; the count is what lets the
    producer find and shorten it."""
    t = Transcript(language="eng", segments=[_seg(0, dur=1.0)], full_text="x")
    long = "у" * (line_budget(t.segments[0]) + 5)
    out, done, over = apply(t, {"lines": [{"i": 0, "text": long}]}, [0])
    assert out.segments[0].translation == long
    assert over == [0]


def test_a_bool_is_not_a_line_index():
    """JSON `true` is 1 to Python. Taken as an index, a malformed answer
    would land on line 1 — here, the very line that was asked for."""
    t = _transcript(2)
    out, done, _ = apply(t, {"lines": [{"i": True, "text": "а"}]}, [1])
    assert out.segments[1].translation is None and done == []


def test_a_malformed_answer_changes_nothing():
    t = _transcript(1)
    for answer in (None, {"lines": "x"}, {"lines": [{"i": True, "text": "а"}]}, {"lines": [["a"]]}):
        out, done, _ = apply(t, answer, [0])
        assert out.segments[0].translation is None and done == []


# --------------------------------------------------------------------------
# A whole run
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_first_run_translates_every_line_with_words():
    t = _transcript(3)
    t.segments.append(_seg(3, text="   "))  # nothing to translate
    client = FakeClient()
    out, report = await translate_transcript(client, t)

    assert [s.translation for s in out.segments[:3]] == ["мн-0-1", "мн-1-1", "мн-2-1"]
    assert out.segments[3].translation is None
    assert report.to_dict()["asked"] == 3 and report.translated == 3 and report.missing == 0


@pytest.mark.asyncio
async def test_a_second_run_is_billed_for_the_gaps_not_the_transcript():
    t = _transcript(3)
    t.segments[0] = t.segments[0].model_copy(update={"translation": "бий"})
    client = FakeClient()
    out, report = await translate_transcript(client, t)

    assert "[0]" not in client.calls[0]["user"].split("Translate these")[1]
    assert out.segments[0].translation == "бий"
    assert report.asked == 2


@pytest.mark.asyncio
async def test_force_sends_every_line_again():
    t = _transcript(2)
    t.segments[0] = t.segments[0].model_copy(update={"translation": "бий"})
    out, report = await translate_transcript(FakeClient(), t, force=True)
    assert report.asked == 2
    assert out.segments[0].translation == "мн-0-1"


@pytest.mark.asyncio
async def test_nothing_to_translate_makes_no_call():
    t = _transcript(2)
    t.segments = [s.model_copy(update={"translation": "бий"}) for s in t.segments]
    client = FakeClient()
    _, report = await translate_transcript(client, t)
    assert client.calls == [] and report.asked == 0


@pytest.mark.asyncio
async def test_a_failed_chunk_costs_only_its_own_lines(monkeypatch):
    monkeypatch.setattr(translate, "MAX_ANSWER_TOKENS", 60)
    t = _transcript(6)
    client = FakeClient(fail_calls={1})
    out, report = await translate_transcript(client, t)

    assert len(client.calls) > 1
    first_chunk = plan_chunks(t.segments, list(range(6)))[0]
    assert all(out.segments[i].translation is None for i in first_chunk)
    assert all(out.segments[i].translation for i in range(6) if i not in first_chunk)
    assert report.failed_chunks == 1 and report.missing == len(first_chunk)


@pytest.mark.asyncio
async def test_a_forced_rerun_that_fails_never_leaves_a_line_worse_off(monkeypatch):
    t = _transcript(2)
    t.segments = [s.model_copy(update={"translation": f"хуучин-{i}"}) for i, s in enumerate(t.segments)]
    with pytest.raises(TranslationError):
        await translate_transcript(FakeClient(fail_calls={1}), t, force=True)
    assert [s.translation for s in t.segments] == ["хуучин-0", "хуучин-1"]


@pytest.mark.asyncio
async def test_a_run_where_nothing_came_back_is_a_failure():
    with pytest.raises(TranslationError):
        await translate_transcript(FakeClient(reply={"lines": []}), _transcript(2))


@pytest.mark.asyncio
async def test_the_names_decided_in_one_chunk_are_shown_to_the_next(monkeypatch):
    monkeypatch.setattr(translate, "MAX_ANSWER_TOKENS", 60)
    t = _transcript(6)
    client = FakeClient()
    await translate_transcript(client, t)
    assert len(client.calls) > 1
    assert "мн-" in client.calls[1]["user"].split("Translate these")[0]


@pytest.mark.asyncio
async def test_a_chunk_too_big_for_the_usual_ceiling_is_given_room_for_its_answer():
    """A line too long for one budget gets a chunk of its own — and a ceiling
    sized from it. A constant would cut the answer off mid-sentence, and the
    line would come back with nothing."""
    t = Transcript(
        language="eng", segments=[_seg(0, text="word " * 6000, dur=600.0)], full_text="",
    )
    client = FakeClient()
    await translate_transcript(client, t)
    assert client.calls[0]["max_tokens"] == call_budget(t.segments, [0]) > MAX_ANSWER_TOKENS
