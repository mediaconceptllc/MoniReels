"""Refusals made before a job is queued.

Each one is a job the configured providers, the translation or the voice
could not finish — checked here, in the request, because the alternative is
a worker that claims a slot, downloads the source video and only then fails.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import providers, worker_report
from app.languages import (
    needs_translation,
    removes_source_speech,
    translation_holes,
    translation_uses,
    voice_over_on,
)
from app.models import Project


def require_provider(db: Session, capability: str) -> None:
    """Refuse a job the configured providers cannot finish.

    Checked HERE and not in the worker: a job queued without a key claims a
    slot, downloads the source video and then dies, and the operator's first
    notice is a failure with a stack trace in it. Reading the stored settings
    is one query.
    """
    from app import provider_settings, providers

    reason = providers.blocker(provider_settings.effective(db), capability)
    if reason:
        raise HTTPException(status_code=503, detail=reason)


def require_translation_ready(project: Project, ranges: list[tuple[float, float]]) -> None:
    """Refuse a render whose Mongolian subtitles or voice would have holes.

    Checked before the render rather than discovered in it: an export is
    minutes of encoding, and the lines a translation run missed would come out
    in the source language, mid-video, with nothing to say why. The message
    names the ways forward that actually work — switching the subtitles to the
    source language fixes nothing while a Mongolian voice still reads the
    translation.

    Only the lines of `ranges` — what this export cuts — are asked about
    (languages.translation_holes): the rest of the transcript is never on
    screen, and the translation that follows the suggestions leaves it alone.
    """
    missing = translation_holes(project, ranges)
    if not missing:
        return
    if "voice" in translation_uses(project):
        detail = (
            f"{missing} мөр орчуулагдаагүй байна. Монгол дуу орчуулгыг уншдаг тул эхлээд "
            "монгол руу орчуулна уу, эсвэл монгол дууг унтраана уу."
        )
    else:
        detail = (
            f"{missing} мөр орчуулагдаагүй байна. Эхлээд монгол руу орчуулна уу, "
            "эсвэл хадмалыг эх хэлээр нь гаргана уу."
        )
    raise HTTPException(status_code=409, detail=detail)


def voice_view(db: Session, project: Project) -> dict:
    """Whether this project's export reads a Mongolian voice, and why it
    cannot right now — the guard's own verdict, read by the page."""
    from app import provider_settings

    # One dict, built once: two literals for the two answers is how a field
    # gets renamed in one and not the other — and the page reads both.
    on = voice_over_on(project)
    blocked = providers.blocker(provider_settings.effective(db), providers.TTS) if on else None
    if blocked is None and removes_source_speech(project):
        # Only the worker knows whether it can separate; it has said so.
        separation = providers.separation(worker_report.read(db))
        if not separation.ready:
            blocked = (
                f"Эх яриаг арилгах боломжгүй: {separation.blocked} "
                "Эсвэл «Намсгах»-ыг сонгож болно."
            )
    return {"on": on, "blocked": blocked}


def require_voice_ready(db: Session, project: Project) -> None:
    """Refuse a voice-over export the voice cannot be made for.

    Before the render, for the same reason as every provider check here: the
    alternative is a job that claims the render slot, downloads the source,
    and only then learns there is no key or no voice. Refused rather than
    exported silent: the producer asked for a Mongolian voice, and a video
    without one looks finished.
    """
    reason = voice_view(db, project)["blocked"]
    if reason:
        # Every reason names the voice itself, so none needs a prefix.
        raise HTTPException(status_code=503, detail=reason)


def require_recogniser_for(db: Session, language: str) -> None:
    """Refuse a transcription the recogniser for THIS video cannot do.

    The readiness check used to read the operator's selected recogniser only.
    For an English video that is the wrong question: it is heard by ElevenLabs
    Scribe whatever is selected (stt.factory.for_language), so a missing
    Scribe key must stop it here — not after the video has been downloaded
    and the job has failed with a message about a recogniser nobody chose.
    """
    from app import provider_settings
    from app.stt.factory import for_language

    settings = for_language(provider_settings.effective(db), language)
    reason = providers.blocker(settings, providers.STT)
    if reason and needs_translation(language):
        reason = f"Монголоос бусад хэлний яриаг зөвхөн ElevenLabs Scribe танина. {reason}"
    if reason:
        raise HTTPException(status_code=503, detail=reason)
