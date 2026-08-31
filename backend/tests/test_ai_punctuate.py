"""Restoring sentences and speakers.

One read of the text, because both come from the same missing information and
a second reader would disagree with the first about where a sentence ended —
split by length when the answer will not fit one call, never by question.
"""
from __future__ import annotations

from app.ai import boundaries
from app.ai.punctuate import (
    CHARS_PER_TOKEN,
    MAX_ANSWER_TOKENS,
    answer_tokens,
    apply,
    build_prompt,
    call_budget,
    is_punctuated,
    plan_chunks,
    words_of,
)
from app.models import Segment, Transcript
from app.stt.elevenlabs_client import segments_from_words


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


# --- chunking -------------------------------------------------------------
#
# The pass asks the model to return the transcript again, so its ANSWER is
# what bounds a call. In production one call was asked to re-emit a whole
# 28-minute Mongolian transcript inside a fixed ceiling; it generated for four
# and a half minutes and came back empty, every time.


def _long_transcript(lines: int, chars: int) -> Transcript:
    return _transcript(*["ү" * chars for _ in range(lines)])


def test_a_transcript_whose_answer_cannot_fit_one_call_is_split():
    t = _long_transcript(400, 200)
    assert answer_tokens(t.segments, list(range(400))) > MAX_ANSWER_TOKENS

    chunks = plan_chunks(t.segments)

    assert len(chunks) > 1, "an answer this size has never fitted a single call"
    for chunk in chunks:
        assert answer_tokens(t.segments, chunk) <= MAX_ANSWER_TOKENS
    # Every line is asked about exactly once, in order.
    assert [i for chunk in chunks for i in chunk] == list(range(400))


def test_a_short_transcript_is_still_one_call():
    chunks = plan_chunks(_long_transcript(5, 20).segments)
    assert chunks == [[0, 1, 2, 3, 4]]


def test_a_single_line_too_big_for_the_budget_still_gets_asked():
    """It is the transcript. It cannot be dropped, and the alternative is a
    loop that never places it."""
    t = _long_transcript(1, int(MAX_ANSWER_TOKENS * CHARS_PER_TOKEN * 2))

    chunks = plan_chunks(t.segments)

    assert chunks == [[0]]
    assert call_budget(t.segments, [0]) > answer_tokens(t.segments, [0]), "asked for less than it needs"


def test_the_next_chunk_is_shown_the_speaker_numbers_the_last_one_gave_out():
    """Without this each chunk numbers its speakers from 1 independently and
    "speaker 1" is a different person in every chunk — labels that look
    complete and are wrong."""
    t = _transcript("нэг", "хоёр", "гурав")

    prompt = build_prompt(t.segments, [2], context=[(1, "S2: Хоёр.")])

    assert "S2: Хоёр." in prompt
    assert "same speaker numbers" in prompt
    assert "do not return these" in prompt
    assert "[2] гурав" in prompt


def test_a_line_outside_the_chunk_cannot_overwrite_a_settled_one():
    """A model shown context lines will sometimes helpfully return them too.
    Accepting that would let a later chunk undo an earlier one."""
    t = _transcript("нэг", "хоёр")
    answer = {"speakers": 1, "lines": [_line(0, "Нэг!"), _line(1, "Хоёр.")]}

    out, _, rejected = apply(t, answer, indices=[1])

    assert [s.text for s in out.segments] == ["нэг", "Хоёр."]
    assert rejected == []


# --- is the pass needed at all? -------------------------------------------


def test_scribe_style_text_is_recognised_as_already_punctuated():
    t = _transcript("Тэр өдөр бид уулзсан.", "Чи хаана байсан бэ?", "Мэдэхгүй ээ.")
    assert is_punctuated(t.segments) is True


def test_unpunctuated_asr_text_is_not():
    t = _transcript("тэр өдөр бид уулзсан", "чи хаана байсан бэ", "мэдэхгүй ээ")
    assert is_punctuated(t.segments) is False


def test_no_segments_is_not_punctuated():
    assert is_punctuated([]) is False


def test_text_far_too_sparse_to_be_punctuated_prose_is_not():
    """One mark in eighty words is a transcript the pass can still help."""
    t = _transcript(" ".join(["үг"] * 80) + ".")
    assert is_punctuated(t.segments) is False


def test_the_answer_does_not_depend_on_how_the_text_was_cut_into_segments():
    """The same words, cut four ways.

    Counting how many SEGMENTS end on a mark sounds equivalent to reading the
    text and is not: app.stt.elevenlabs_client closes a segment on a duration
    cap, and such a segment ends mid-sentence by construction. Under that
    measure this one text reads as 100% punctuated at a 15s cap and 25% at a
    3s one — so lowering MAX_SEGMENT_SEC would quietly start paying for a pass
    the transcript does not need, with nothing naming the connection.
    """
    words, t = [], 0.0
    for _ in range(15):
        for j in range(30):
            words.append(
                {"text": ("үг." if j == 29 else "үг"), "start": t, "end": t + 0.34,
                 "type": "word", "speaker_id": "speaker_1"}
            )
            t += 0.38

    verdicts = {}
    end_shares = set()
    for cap in (3.0, 6.0, 15.0, 60.0):
        segments = segments_from_words(words, max_sec=cap)
        verdicts[cap] = is_punctuated(segments)
        end_shares.add(
            round(sum(boundaries.ends_sentence(s.text) for s in segments) / len(segments), 2)
        )

    assert len(end_shares) > 1, "the caps must actually cut this text differently"
    assert set(verdicts.values()) == {True}, verdicts
