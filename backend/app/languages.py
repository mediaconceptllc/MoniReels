"""The languages a video can be spoken in, and what each one needs.

One module, because three places ask the same question and must not answer it
differently: which recogniser can hear this video, whether its text needs
translating before a Mongolian audience can read it, and which subtitles an
export burns by default.

The audience is always Mongolian — this is a Mongolian studio's tool — so the
only thing that varies is the SOURCE. A Mongolian video needs nothing; any
other language needs its speech recognised in that language and its text
translated.
"""
from __future__ import annotations

MONGOLIAN = "mn"
ENGLISH = "en"

#: What a producer can say a video is spoken in. Kept to the languages the
#: pipeline has actually been built for: adding one here without a recogniser
#: code below would let a project claim a language nothing can transcribe.
SOURCE_LANGUAGES = (MONGOLIAN, ENGLISH)

#: ElevenLabs Scribe takes ISO 639-3. `mon` is what it was always sent.
SCRIBE_CODES = {MONGOLIAN: "mon", ENGLISH: "eng"}


def needs_translation(language: str | None) -> bool:
    """Whether a Mongolian reader needs this video's text translated."""
    return (language or MONGOLIAN) != MONGOLIAN


def subtitles_need_translation(project) -> bool:
    """Whether this project's export would put the TRANSLATION on screen or in
    the .srt — the one case where missing translations matter to a render."""
    export = project.export
    return (
        needs_translation(project.language)
        and export.subtitle_language == MONGOLIAN
        and (export.burn_subtitles or export.write_srt)
    )


def voice_over_on(project) -> bool:
    """Whether this project's export reads the translation aloud. Only a video
    not in Mongolian has one to read: a Mongolian video already speaks it."""
    return needs_translation(project.language) and bool(project.export.voice_over)


def translation_uses(project) -> list[str]:
    """What this project's export would use the translation for — subtitles,
    the voice, both, or nothing. Named rather than collapsed to a yes/no,
    because the way out of a missing translation depends on which: subtitles
    can go out in the source language, a Mongolian voice has nothing else to
    read."""
    uses = []
    if subtitles_need_translation(project):
        uses.append("subtitles")
    if voice_over_on(project):
        uses.append("voice")
    return uses


def untranslated_lines(project) -> int:
    """Lines with words and no translation yet."""
    transcript = project.transcript
    if transcript is None:
        return 0
    return sum(1 for s in transcript.segments if (s.text or "").strip() and not s.translation)


def translation_view(project) -> dict:
    """What the project page shows about translation, from the rules the
    routes enforce.

    `blocks_export` is the export guard's own verdict, not an input to it: a
    page that worked the rule out for itself would be a second copy of it,
    and the first change to either would disable a button the server would
    have accepted — or leave one enabled for a click it then refuses.
    """
    needed = needs_translation(project.language)
    transcript = project.transcript
    lines = (
        sum(1 for s in transcript.segments if (s.text or "").strip()) if transcript else 0
    )
    missing = untranslated_lines(project) if needed else 0
    uses = translation_uses(project)
    return {
        "needed": needed,
        "lines": lines,
        "translated": lines - missing if needed else 0,
        "missing": missing,
        "blocks_export": bool(uses) and missing > 0,
        "used_for": uses,
    }


def subtitle_segments(project) -> tuple[list | None, int]:
    """(the segments an export's subtitles are built from, lines that fell back).

    For Mongolian subtitles on a foreign-language video that is the
    translation, WITHOUT the word timings: those belong to the source words,
    and a karaoke-style timing laid over different words is wrong on every
    one of them. The segment timings stay — they are when the line was said.

    A line with no translation falls back to what was said rather than going
    blank, and is counted. The export route refuses to start while any are
    missing, so a fallback here means a translation was cleared between the
    click and the render — worth a number in the result, not a hole in the
    subtitles.
    """
    transcript = project.transcript
    if transcript is None:
        return None, 0
    if not (needs_translation(project.language) and project.export.subtitle_language == MONGOLIAN):
        return transcript.segments, 0
    fallback = 0
    segments = []
    for s in transcript.segments:
        if not s.translation and (s.text or "").strip():
            fallback += 1
        segments.append(s.model_copy(update={"text": s.translation or s.text, "words": []}))
    return segments, fallback
