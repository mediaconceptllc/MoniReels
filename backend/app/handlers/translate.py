"""`translate`: the transcript's lines in Mongolian."""
from __future__ import annotations

from app import provider_settings
from app.ai.openrouter_client import build_client as build_llm_client
from app.ai.translate import translate_transcript
from app.db import session_scope
from app.handlers.common import _project_id
from app.jobs.queue import JobCancelled, JobHandle
from app.languages import lines_used, needs_translation, suggested_ranges
from app.store import load, save


async def handle_translate(handle: JobHandle) -> dict:
    with session_scope() as db:
        settings = provider_settings.effective(db)
        project = load(db, _project_id(handle))
    if not needs_translation(project.language):
        raise RuntimeError("This video is already in Mongolian")
    if project.transcript is None or not project.transcript.segments:
        raise RuntimeError("Transcribe the video before translating it")
    force = (handle.payload or {}).get("force") is True
    # "suggested": only the lines the suggestions cut — what follows a
    # suggestion run on its own (_translate_suggested). Anything else, the
    # whole transcript, as the page's button always did.
    scope = "suggested" if (handle.payload or {}).get("scope") == "suggested" else "all"
    only = (
        {s.id for s in lines_used(project, suggested_ranges(project))}
        if scope == "suggested"
        else None
    )
    read = project.transcript

    async def on_progress(p: float) -> None:
        await handle.set_progress(0.05 + p * 0.9, stage="translating", message="Translating the transcript")

    await handle.set_progress(0.05, stage="translating", message="Translating the transcript")
    client = build_llm_client(settings)
    try:
        translated, report = await translate_transcript(
            client, read, force=force, only=only, on_progress=on_progress
        )
    finally:
        await client.aclose()

    # A cancel is honoured AFTER the fold, not before it: the lines that came
    # back are paid for, and a re-run without them would buy them again. The
    # job still ends canceled, saying how many it kept.

    # Folded onto the transcript as it is NOW, not as it was read. A run takes
    # minutes, and in that time the producer may correct an English line
    # (which clears its translation) or hand-edit a Mongolian one. Writing back
    # the copy read at the start would silently undo both. So a line takes the
    # new translation only if neither its text nor its translation moved while
    # this ran; one that did is kept as the producer left it, and counted.
    before = {s.id: (s.text, s.translation) for s in read.segments}
    fresh = {s.id: s for s in translated.segments}
    applied = kept = 0
    with session_scope() as db:
        project = load(db, _project_id(handle), for_update=True)
        if project.transcript is None:
            raise RuntimeError("The transcript was removed while it was being translated")
        for seg in project.transcript.segments:
            new = fresh.get(seg.id)
            if new is None or not new.translation or new.translation == seg.translation:
                continue
            if (seg.text, seg.translation) != before.get(seg.id):
                kept += 1
                continue
            seg.translation = new.translation
            applied += 1
        save(db, project)

    if report.stopped or handle.cancel_requested:
        raise JobCancelled({"lines_applied": applied, "lines_asked": report.asked})
    return {**report.to_dict(), "applied": applied, "kept_edits": kept, "scope": scope}
