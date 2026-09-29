"""Which recogniser hears a video, and which words its subtitles carry.

The failure this exists for was silent: both recognisers were pinned to
Mongolian, so an English video came back as Mongolian-shaped nonsense with no
error anywhere, and was billed.
"""
from __future__ import annotations

from typing import get_args

from app import languages
from app.config import get_settings
from app.languages import (
    ENGLISH,
    MONGOLIAN,
    SOURCE_LANGUAGES,
    subtitle_segments,
    subtitles_need_translation,
    translation_uses,
    translation_view,
    untranslated_lines,
    voice_over_on,
)
from app.models import (
    Cut,
    ExportSettings,
    Project,
    Segment,
    ShortIdea,
    Suggestions,
    Transcript,
    Word,
)
from app.schemas import SourceLanguage
from app.stt import factory


def test_a_mongolian_video_goes_to_whichever_recogniser_the_operator_chose():
    settings = get_settings().model_copy(update={"stt_provider": "duudlaga"})
    assert factory.for_language(settings, MONGOLIAN) is settings


def test_an_english_video_goes_to_scribe_in_english_whatever_is_selected():
    """duudlaga.dev is a Mongolian recogniser whose API takes no language."""
    settings = get_settings().model_copy(update={"stt_provider": "duudlaga"})
    routed = factory.for_language(settings, ENGLISH)
    assert routed.stt_provider == factory.ELEVENLABS
    assert routed.elevenlabs_stt_language == "eng"


def test_routing_never_touches_the_operators_stored_settings():
    settings = get_settings().model_copy(update={"stt_provider": "duudlaga"})
    factory.for_language(settings, ENGLISH)
    assert settings.stt_provider == "duudlaga"


def test_a_missing_language_is_mongolian():
    """Every project made before the field existed was Mongolian."""
    settings = get_settings()
    assert factory.for_language(settings, None) is settings
    assert Project(name="p").language == MONGOLIAN


def test_the_request_schema_offers_exactly_the_languages_that_can_be_heard():
    """A language accepted at the door with no recogniser code behind it
    would be a project nothing can transcribe."""
    assert set(get_args(SourceLanguage)) == set(SOURCE_LANGUAGES)
    assert set(SOURCE_LANGUAGES) <= set(languages.SCRIBE_CODES)


# --------------------------------------------------------------------------
# Subtitles
# --------------------------------------------------------------------------

def _project(language: str, *, subtitle_language: str = "mn", burn: bool = True, srt: bool = True,
             translated: tuple[str | None, ...] = ("Сайн уу", "Баяртай"), voice: bool = False,
             cuts: tuple[tuple[float, float], ...] = ((0.0, 4.0),)) -> Project:
    """Two lines, at 0-1.5s and 2-3.5s, and one idea whose `cuts` take both
    by default. `cuts=()` is a project with no suggestions yet."""
    words = [Word(text="Hello", start=0.0, end=0.5)]
    segments = [
        Segment(id=f"s{i}", start=i * 2.0, end=i * 2.0 + 1.5, text=text, words=words, translation=tr)
        for i, (text, tr) in enumerate(zip(("Hello", "Goodbye"), translated, strict=True))
    ]
    ideas = Suggestions(shorts=[ShortIdea(
        id="sh1", title="t", hook_text="h", hook_quote="q", caption="c", why_it_works="w",
        cuts=[Cut(start=a, end=b, role="hook", reason="r") for a, b in cuts],
    )]) if cuts else None
    return Project(
        name="p",
        language=language,
        transcript=Transcript(language="eng", segments=segments, full_text="Hello Goodbye"),
        export=ExportSettings(
            subtitle_language=subtitle_language, burn_subtitles=burn, write_srt=srt, voice_over=voice
        ),
        suggestions=ideas,
    )


def test_a_mongolian_video_subtitles_what_was_said():
    project = _project(MONGOLIAN)
    segments, fallback = subtitle_segments(project)
    assert [s.text for s in segments] == ["Hello", "Goodbye"] and fallback == 0


def test_an_english_video_subtitles_the_translation_by_default():
    segments, fallback = subtitle_segments(_project(ENGLISH))
    assert [s.text for s in segments] == ["Сайн уу", "Баяртай"]
    assert fallback == 0


def test_translated_subtitles_carry_no_word_timings_from_the_source():
    """Word timings belong to the English words; laid over Mongolian ones
    they are wrong on every word."""
    segments, _ = subtitle_segments(_project(ENGLISH))
    assert all(s.words == [] for s in segments)
    # The line timings stay — they are when it was said.
    assert (segments[1].start, segments[1].end) == (2.0, 3.5)


def test_source_subtitles_on_an_english_video_are_what_was_said():
    segments, _ = subtitle_segments(_project(ENGLISH, subtitle_language="source"))
    assert [s.text for s in segments] == ["Hello", "Goodbye"]


def test_a_line_with_no_translation_falls_back_and_is_counted():
    segments, fallback = subtitle_segments(_project(ENGLISH, translated=("Сайн уу", None)))
    assert [s.text for s in segments] == ["Сайн уу", "Goodbye"]
    assert fallback == 1


def test_the_stored_transcript_is_not_rewritten_by_building_subtitles():
    project = _project(ENGLISH)
    subtitle_segments(project)
    assert project.transcript.segments[0].text == "Hello"
    assert project.transcript.segments[0].words


def test_missing_translations_matter_only_when_they_would_reach_the_output():
    assert subtitles_need_translation(_project(ENGLISH))
    assert not subtitles_need_translation(_project(MONGOLIAN))
    assert not subtitles_need_translation(_project(ENGLISH, subtitle_language="source"))
    assert not subtitles_need_translation(_project(ENGLISH, burn=False, srt=False))
    assert subtitles_need_translation(_project(ENGLISH, burn=False, srt=True))


def test_untranslated_lines_counts_only_lines_with_words():
    project = _project(ENGLISH, translated=(None, None))
    project.transcript.segments[1].text = "  "
    assert untranslated_lines(project) == 1


def test_the_view_counts_what_the_page_shows():
    view = translation_view(_project(ENGLISH, translated=("Сайн уу", None)))
    assert view == {"needed": True, "lines": 2, "translated": 1, "missing": 1, "blocks_export": True,
                    "used_for": ["subtitles"], "suggested_lines": 2, "suggested_missing": 1,
                    "suggested_ids": ["s0", "s1"]}


def test_a_line_no_idea_cuts_is_counted_but_holds_nothing_back():
    """The translation after the suggestions translates what they cut; the
    rest of the transcript is shown by nothing."""
    view = translation_view(_project(ENGLISH, translated=("Сайн уу", None), cuts=((0.0, 1.0),)))
    assert view["missing"] == 1
    assert (view["suggested_lines"], view["suggested_missing"], view["suggested_ids"]) == (1, 0, ["s0"])
    assert view["blocks_export"] is False


def test_before_there_are_ideas_nothing_is_held_back_yet():
    view = translation_view(_project(ENGLISH, translated=(None, None), cuts=()))
    assert view["missing"] == 2 and view["suggested_lines"] == 0 and view["blocks_export"] is False


def test_a_line_with_no_words_is_neither_translated_nor_missing():
    project = _project(ENGLISH, translated=(None, None))
    project.transcript.segments[1].text = "  "
    view = translation_view(project)
    assert (view["lines"], view["translated"], view["missing"]) == (1, 0, 1)


def test_missing_lines_block_nothing_when_they_would_not_reach_the_output():
    view = translation_view(_project(ENGLISH, subtitle_language="source", translated=(None, None)))
    assert view["missing"] == 2 and view["blocks_export"] is False


def test_a_mongolian_video_is_never_missing_a_translation():
    """Its lines have no translation because they need none — counted as
    missing, the page would offer to translate Mongolian into Mongolian."""
    view = translation_view(_project(MONGOLIAN, translated=(None, None), voice=True))
    assert view == {"needed": False, "lines": 2, "translated": 0, "missing": 0, "blocks_export": False,
                    "used_for": [], "suggested_lines": 0, "suggested_missing": 0, "suggested_ids": []}


def test_a_project_with_no_transcript_yet_has_nothing_to_count():
    project = _project(ENGLISH)
    project.transcript = None
    view = translation_view(project)
    assert (view["lines"], view["missing"], view["blocks_export"]) == (0, 0, False)



# --------------------------------------------------------------------------
# The voice reads the translation too
# --------------------------------------------------------------------------

def test_only_a_video_not_in_mongolian_is_voiced_over():
    assert voice_over_on(_project(ENGLISH, voice=True))
    assert not voice_over_on(_project(ENGLISH, voice=False))
    assert not voice_over_on(_project(MONGOLIAN, voice=True))


def test_what_the_translation_is_used_for_is_named():
    assert translation_uses(_project(ENGLISH)) == ["subtitles"]
    assert translation_uses(_project(ENGLISH, voice=True)) == ["subtitles", "voice"]
    assert translation_uses(_project(ENGLISH, subtitle_language="source", voice=True)) == ["voice"]
    assert translation_uses(_project(ENGLISH, subtitle_language="source")) == []


def test_a_voice_blocks_an_export_even_with_subtitles_in_the_source_language():
    """Switching the subtitles to English is the way out for subtitles only:
    a Mongolian voice has nothing else to read."""
    view = translation_view(
        _project(ENGLISH, subtitle_language="source", translated=("Сайн уу", None), voice=True)
    )
    assert view["blocks_export"] is True and view["used_for"] == ["voice"]


# --------------------------------------------------------------------------
# Which lines an export uses — one rule for the translation, the guard and
# the page
# --------------------------------------------------------------------------

def _ids(project, ranges):
    return [s.id for s in languages.lines_used(project, ranges)]


def test_a_line_touching_a_cut_by_its_edge_is_used():
    """The subtitles split a line at the cut and show its part
    (app.subtitle.shift), so an edge is enough."""
    project = _project(ENGLISH)  # lines at 0-1.5 and 2-3.5
    assert _ids(project, [(1.4, 1.9)]) == ["s0"]
    assert _ids(project, [(1.0, 2.1)]) == ["s0", "s1"]


def test_a_line_that_only_meets_a_cut_is_not_used():
    project = _project(ENGLISH)
    assert _ids(project, [(1.5, 2.0)]) == []
    assert _ids(project, []) == []


def test_a_line_with_no_words_is_never_used():
    project = _project(ENGLISH)
    project.transcript.segments[0].text = "  "
    assert _ids(project, [(0.0, 4.0)]) == ["s1"]


def test_every_line_the_voice_reads_is_one_the_translation_covers():
    """The voice reads the lines whose middle is inside a clip
    (app.tts.voiceover.lines_in) — a subset of what is translated, or a line
    would be read aloud in no language at all."""
    from app.tts.voiceover import lines_in

    project = _project(ENGLISH)
    segments = project.transcript.segments
    for start in (0.0, 0.5, 0.8, 1.2, 1.9, 2.5):
        for end in (start + 0.1, start + 0.8, start + 2.0, 4.0):
            used = set(_ids(project, [(start, end)]))
            assert {s.id for s in lines_in(segments, start, end)} <= used, (start, end)


def test_holes_are_counted_only_where_the_export_uses_the_translation():
    missing = (None, None)
    assert languages.translation_holes(_project(ENGLISH, translated=missing), [(0.0, 4.0)]) == 2
    assert languages.translation_holes(_project(ENGLISH, translated=missing), [(0.0, 1.0)]) == 1
    source = _project(ENGLISH, translated=missing, subtitle_language="source")
    assert languages.translation_holes(source, [(0.0, 4.0)]) == 0

