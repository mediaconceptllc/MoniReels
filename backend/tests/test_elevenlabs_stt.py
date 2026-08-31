"""ElevenLabs Scribe.

Different in kind from duudlaga.dev: real word timings and a speaker per
word, so `timings_estimated` is finally False and the speaker count is
MEASURED rather than inferred from the text by a paid LLM call.

The contract could not be checked against the live documentation — the
network this was written on blocks elevenlabs.io — so the parser is
deliberately tolerant and these tests pin what it does with each shape.
"""
from __future__ import annotations

import pytest

from app.stt.elevenlabs_client import (
    ElevenLabsError,
    build_transcript,
    segments_from_words,
)


def _word(text: str, start: float, end: float, speaker: str | None = None, kind: str = "word") -> dict:
    w = {"text": text, "start": start, "end": end, "type": kind}
    if speaker is not None:
        w["speaker_id"] = speaker
    return w


def test_word_timings_produce_real_segment_times():
    words = [_word("Сайн", 0.0, 0.4), _word("байна", 0.4, 0.9), _word("уу.", 0.9, 1.2)]
    (segment,) = segments_from_words(words)

    assert segment.start == 0.0
    assert segment.end == 1.2
    assert segment.text == "Сайн байна уу."


def test_a_sentence_ending_closes_a_segment():
    words = [_word("Нэг.", 0.0, 0.5), _word("Хоёр.", 0.5, 1.0)]
    assert [s.text for s in segments_from_words(words)] == ["Нэг.", "Хоёр."]


def test_a_speaker_change_closes_a_segment_before_anything_else():
    # Two people in one subtitle is the failure a viewer notices first.
    words = [
        _word("Чи", 0.0, 0.3, "speaker_0"),
        _word("хаана", 0.3, 0.6, "speaker_0"),
        _word("Би", 0.6, 0.9, "speaker_1"),
    ]
    segments = segments_from_words(words)

    assert [s.text for s in segments] == ["Чи хаана", "Би"]
    assert [s.speaker for s in segments] == ["speaker_0", "speaker_1"]


def test_a_speaker_who_never_pauses_is_still_cut_into_usable_segments():
    words = [_word("үг", float(i), float(i) + 1.0) for i in range(40)]
    segments = segments_from_words(words, max_sec=15.0)

    assert len(segments) > 1
    assert all(s.end - s.start <= 16.0 for s in segments)


def test_audio_events_and_spacing_are_not_dialogue():
    # Keeping them would put "(laughter)" in a subtitle and count it as
    # something somebody said.
    words = [
        _word("Сайн", 0.0, 0.4),
        _word("(инээв)", 0.4, 0.8, kind="audio_event"),
        _word(" ", 0.8, 0.9, kind="spacing"),
        _word("уу.", 0.9, 1.2),
    ]
    (segment,) = segments_from_words(words)
    assert segment.text == "Сайн уу."


def test_the_transcript_counts_the_speakers_it_measured():
    payload = {
        "language_code": "mon",
        "text": "Чи хаана. Би энд.",
        "words": [
            _word("Чи", 0.0, 0.3, "speaker_0"),
            _word("хаана.", 0.3, 0.6, "speaker_0"),
            _word("Би", 0.6, 0.9, "speaker_1"),
            _word("энд.", 0.9, 1.2, "speaker_1"),
        ],
    }
    transcript = build_transcript(payload, 1.2)

    assert transcript.speakers == 2
    assert transcript.timings_estimated is False
    assert transcript.language == "mon"


def test_a_response_with_only_text_still_works():
    # Worse — the times go back to being estimates — but it is exactly what
    # this pipeline ran on before, so it is not a failure.
    transcript = build_transcript({"text": "Сайн байна уу."}, 3.0)

    assert transcript.full_text == "Сайн байна уу."
    assert transcript.timings_estimated is True


def test_a_response_with_neither_text_nor_words_names_what_it_got():
    # A KeyError here would be a stack trace about a dict; this says which
    # keys arrived, which is what tells an operator the contract moved.
    with pytest.raises(ElevenLabsError) as raised:
        build_transcript({"detail": "nope"}, 3.0)
    assert "detail" in str(raised.value)


def test_a_non_dict_response_is_refused():
    with pytest.raises(ElevenLabsError):
        build_transcript(["unexpected"], 3.0)


def test_an_account_level_failure_ends_the_run():
    # Same rule the duudlaga client learned: a spent quota stays spent.
    assert ElevenLabsError("no", status=401).ends_the_run
    assert ElevenLabsError("no", status=402).ends_the_run
    assert not ElevenLabsError("busy", status=429).ends_the_run
    assert not ElevenLabsError("boom", status=500).ends_the_run


def test_an_unknown_provider_name_is_refused_loudly():
    # Falling back to a default means the operator selects one provider, the
    # log says another, and the bill arrives from the second.
    from app.config import get_settings
    from app.stt import factory

    settings = get_settings().model_copy(update={"stt_provider": "whisper"})
    with pytest.raises(factory.UnknownSttProvider):
        factory.build_client(settings)


def test_the_key_read_is_the_selected_providers():
    from app.config import get_settings
    from app.stt import factory

    base = get_settings().model_copy(
        update={"duudlaga_api_key": "dk", "elevenlabs_api_key": "el"}
    )
    assert factory.api_key_for(base.model_copy(update={"stt_provider": "duudlaga"})) == "dk"
    assert factory.api_key_for(base.model_copy(update={"stt_provider": "elevenlabs"})) == "el"
