"""`suggest`: the short and long ideas a video holds, in Mongolian, and the
translation of the lines they cut."""
from __future__ import annotations

from typing import Any

from app import provider_settings
from app.ai.localize import localize_suggestions
from app.ai.openrouter_client import build_client as build_llm_client
from app.ai.schema import count_limits, default_counts
from app.ai.suggest import generate_suggestions
from app.db import session_scope
from app.handlers.common import _project_id
from app.jobs import queue
from app.jobs.queue import JobCancelled, JobHandle
from app.languages import suggested_ranges, translation_holes
from app.models import Suggestions
from app.store import load, save
from app.utils.logging import get_logger

logger = get_logger(__name__)


def _suggest_counts(payload: dict | None, duration_sec: float) -> tuple[int, int]:
    """The counts this job was queued with, held to what this video can take.

    A job queued before counts existed carries none and gets what the button
    always meant. The limit is applied again here rather than trusted from the
    route: a row can sit in the queue across a deploy that tightened it, and a
    count this video cannot hold would only buy near-duplicates.
    """
    shorts_max, youtube_max = count_limits(duration_sec)
    shorts_default, youtube_default = default_counts(duration_sec)
    payload = payload or {}

    def pick(key: str, default: int, most: int, least: int) -> int:
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            return default
        return max(least, min(most, value))

    return (
        pick("shorts", shorts_default, shorts_max, 1),
        pick("youtube", youtube_default, youtube_max, 0),
    )


async def handle_suggest(handle: JobHandle) -> dict:
    with session_scope() as db:
        settings = provider_settings.effective(db)
        project = load(db, _project_id(handle))
    if project.video is None:
        raise RuntimeError("This project has no video")
    if project.transcript is None or not project.transcript.segments:
        raise RuntimeError("Transcribe the video before asking for suggestions")
    # What goes INTO the prompt, held for the result below: the project is
    # read again before saving, and its transcript is not this one's concern.
    transcript = project.transcript

    shorts, youtube = _suggest_counts(handle.payload, project.video.duration_sec)

    await handle.set_progress(0.1, stage="requesting", message="Asking the model for suggestions")
    client = build_llm_client(settings)
    try:
        suggestions: Suggestions = await generate_suggestions(
            client, transcript, project.video.duration_sec,
            shorts=shorts, youtube=youtube,
        )
        # Before anyone reads them: the cards in Mongolian, whatever the
        # model wrote them in.
        suggestions, localized = await _localize(client, suggestions)
    finally:
        await client.aclose()

    handle.raise_if_cancelled()

    with session_scope() as db:
        project = load(db, _project_id(handle), for_update=True)
        project.suggestions = suggestions
        save(db, project)

    result: dict[str, Any] = {
        "shorts": len(suggestions.shorts),
        "youtube": len(suggestions.youtube),
        # What was asked for, beside what came back. The bill follows the ask
        # (the model generates what it was told to), so an estimate built from
        # past runs has to know which count those runs were for.
        "requested_shorts": shorts,
        "requested_youtube": youtube,
        # What went INTO the prompt, recorded beside what came out of it. The
        # bill scales with this, so a cost without it can be reported but not
        # projected onto the next run (see app.spend.suggest_rate).
        "characters": len(transcript.full_text),
    }
    if localized:
        result["localized"] = localized
    follow = _translate_suggested(project)
    if follow:
        result["translation"] = follow
    return result


async def _localize(client, suggestions: Suggestions) -> tuple[Suggestions, int]:
    """The suggestion texts in Mongolian (app.ai.localize), or as they are.

    Never a reason to fail: the ideas are already paid for, and a card in
    English is worth far less than the ideas it names. The failure is logged
    and the texts stay as the model wrote them.
    """
    try:
        return await localize_suggestions(client, suggestions)
    except JobCancelled:
        raise
    except Exception:  # noqa: BLE001 - the ideas are paid for; keep them whatever this does
        logger.exception("Could not translate the suggestion texts; they stay as the model wrote them")
        return suggestions, 0


def _translate_suggested(project) -> dict | None:
    """Queues the translation of the lines the new suggestions cut, when an
    export of them would show or read one that has none yet.

    After the suggestions, not before them: they are chosen from what was
    said, so a translation run first bought the whole transcript to export
    a few minutes of it. Only the lines an export of them uses
    (languages.translation_holes) — the rest can still be translated from
    the page — and only when the export uses the translation at all: a
    producer who chose subtitles in the spoken language and no Mongolian
    voice is not billed for a translation nothing shows.

    Queued as its own job rather than run here, so the ideas are on the page
    while it runs, a failure costs only the translation, and a re-run is the
    same button as any other. Its dedupe key is the manual one: a translation
    of the project already running is not paid for twice.
    """
    missing = translation_holes(project, suggested_ranges(project))
    if not missing:
        return None
    try:
        job_id = queue.enqueue(
            "translate",
            project_id=project.id,
            payload={"force": False, "scope": "suggested"},
            dedupe_key=f"translate:{project.id}",
        )
    except Exception:  # noqa: BLE001 - the ideas are saved; the page offers the translation itself
        logger.exception("Could not queue the translation of the suggested lines")
        return None
    return {"job_id": job_id, "lines": missing}
