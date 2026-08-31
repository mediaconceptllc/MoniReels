"""Restoring sentences and speakers.

One paid call, because both come from the same missing information and a
second reader would disagree with the first about where a sentence ended.
"""
from __future__ import annotations

from app.ai.punctuate import apply, build_prompt, words_of
from app.models import Segment, Transcript


def _transcript(*texts: str) -> Transcript:
    segments = [
        Segment(id=str(i), start=float(i), end=float(i + 1), text=t) for i, t in enumerate(texts)
    ]
    return Transcript(language="mn", segments=segments, full_text=" ".join(texts))


def _line(i: int, text: str, speaker: int = 1) -> dict:
    return {"i": i, "speaker": speaker, "text": text}


def test_punctuation_and_speakers_are_applied():
    t = _transcript("тэр өдөр бид уулзсан", "чи хаана байсан бэ")
    answer = {
        "speakers": 2,
        "lines": [_line(0, "Тэр өдөр бид уулзсан.", 1), _line(1, "Чи хаана байсан бэ?", 2)],
    }

    out, speakers, rejected = apply(t, answer)

    assert [s.text for s in out.segments] == ["Тэр өдөр бид уулзсан.", "Чи хаана байсан бэ?"]
    assert [s.speaker for s in out.segments] == ["S1", "S2"]
    assert speakers == 2
    assert rejected == []


def test_a_line_whose_WORDS_changed_is_rejected():
    # A model asked to punctuate will occasionally also "fix" what it thinks
    # it misheard. A transcript that quietly disagrees with the audio is a
    # subtitle that accuses someone of saying something they did not.
    t = _transcript("тэр өдөр бид уулзсан")
    answer = {"speakers": 1, "lines": [_line(0, "Тэр өдөр бид уулзаагүй.")]}

    out, _, rejected = apply(t, answer)

    assert out.segments[0].text == "тэр өдөр бид уулзсан"  # left as transcribed
    assert rejected == ["[0]"]


def test_one_bad_line_does_not_cost_the_others():
    t = _transcript("нэг", "хоёр", "гурав")
    answer = {
        "speakers": 1,
        "lines": [_line(0, "Нэг."), _line(1, "Тавь."), _line(2, "Гурав.")],
    }

    out, _, rejected = apply(t, answer)

    assert [s.text for s in out.segments] == ["Нэг.", "хоёр", "Гурав."]
    assert rejected == ["[1]"]


def test_timing_is_never_touched():
    # Punctuation does not move when a word was spoken, and every cut,
    # subtitle and export in this system is built on these boundaries.
    t = _transcript("нэг", "хоёр")
    answer = {"speakers": 1, "lines": [_line(0, "Нэг!"), _line(1, "Хоёр?")]}

    out, _, _ = apply(t, answer)

    assert [(s.start, s.end) for s in out.segments] == [(0.0, 1.0), (1.0, 2.0)]


def test_a_missing_line_keeps_its_original_text():
    t = _transcript("нэг", "хоёр")
    out, _, rejected = apply(t, {"speakers": 1, "lines": [_line(0, "Нэг.")]})

    assert [s.text for s in out.segments] == ["Нэг.", "хоёр"]
    assert rejected == []  # absent is not the same as wrong


def test_a_nonsense_speaker_count_falls_back_to_what_was_labelled():
    t = _transcript("нэг", "хоёр")
    answer = {"speakers": 0, "lines": [_line(0, "Нэг.", 1), _line(1, "Хоёр.", 2)]}

    _, speakers, _ = apply(t, answer)

    assert speakers == 2


def test_case_and_punctuation_are_not_word_changes():
    assert words_of("Тэр өдөр, бид уулзсан!") == words_of("тэр өдөр бид уулзсан")


def test_the_full_text_follows_the_segments():
    t = _transcript("нэг", "хоёр")
    out, _, _ = apply(t, {"speakers": 1, "lines": [_line(0, "Нэг."), _line(1, "Хоёр.")]})
    assert out.full_text == "Нэг. Хоёр."


def test_the_prompt_numbers_every_line():
    t = _transcript("нэг", "хоёр")
    prompt = build_prompt(t.segments)
    assert "[0] нэг" in prompt
    assert "[1] хоёр" in prompt
