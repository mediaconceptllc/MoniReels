"""Pipeline stages: each route checks what the stage needs and queues one job."""
from __future__ import annotations

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app import providers, quotas
from app.ai.schema import count_limits, default_counts
from app.api.projects.common import projects_router, require_project
from app.api.projects.guards import require_provider, require_recogniser_for
from app.db import get_db
from app.jobs import queue
from app.languages import needs_translation, suggested_ranges
from app.schemas import SuggestIn, TranscribeIn, TranslateIn
from app.security import Principal, current_user

router = projects_router()


@router.post("/{project_id}/transcribe")
def transcribe(
    project_id: str,
    body: TranscribeIn | None = None,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = require_project(db, project_id, principal)
    if project.video is None:
        raise HTTPException(status_code=400, detail="Эхлээд видео оруулна уу.")
    if project.transcript and project.transcript.segments and not (body and body.force):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Энэ төсөлд {len(project.transcript.segments)} мөр текст аль хэдийн бий. "
                "Дахин таниулбал текст бүхэлдээ — гар засвар, орчуулгатай нь — солигдож, "
                "дахин төлбөр гарна. Баталгаажуулж байж эхлүүлнэ."
            ),
        )
    require_recogniser_for(db, project.language)
    key = f"transcribe:{project_id}"
    quotas.admit_job(db, principal, "transcribe", dedupe_key=key)
    return {"job_id": queue.enqueue("transcribe", project_id=project_id, dedupe_key=key, db=db)}


@router.post("/{project_id}/translate")
def translate(
    project_id: str,
    body: TranslateIn | None = None,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Translate a non-Mongolian transcript into Mongolian subtitles — all of
    it, or only the lines the suggestions cut (`scope`).

    Refused for a Mongolian video rather than run as a no-op: a paid call
    whose answer is the text it was given is a bill for nothing. The same for
    the suggested lines of a project with no suggestions.

    One dedupe key for both scopes: a translation of the project is one job
    at a time, so a second click never bills the same lines twice.
    """
    project = require_project(db, project_id, principal)
    if not needs_translation(project.language):
        raise HTTPException(status_code=400, detail="Энэ видео монгол хэлтэй — орчуулах шаардлагагүй.")
    if project.transcript is None or not project.transcript.segments:
        raise HTTPException(status_code=400, detail="Эхлээд яриаг таниулна уу.")
    scope = body.scope if body else "all"
    if scope == "suggested" and not suggested_ranges(project):
        raise HTTPException(status_code=400, detail="Санал алга — эхлээд санал боловсруулна уу.")
    require_provider(db, providers.LLM)
    quotas.admit_job(db, principal, "translate", dedupe_key=f"translate:{project_id}")
    return {
        "job_id": queue.enqueue(
            "translate",
            project_id=project_id,
            payload={"force": bool(body and body.force), "scope": scope},
            dedupe_key=f"translate:{project_id}",
            db=db,
        )
    }


@router.post("/{project_id}/suggest")
def suggest(
    project_id: str,
    body: SuggestIn | None = None,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Ask the model for ideas — as many as the producer chose.

    A count this video cannot hold is REFUSED, not clamped: a silent clamp
    turns "8" into "4" after the click, and the producer finds out only when
    fewer come back than they paid to ask for.

    The count is not part of the dedupe key, on purpose. The result replaces
    the project's suggestions, so two runs at once would race to overwrite
    each other; a second click while one is running gets that one's id.
    """
    project = require_project(db, project_id, principal)
    if project.transcript is None or not project.transcript.segments:
        raise HTTPException(status_code=400, detail="Эхлээд яриаг таниулна уу.")
    if project.suggestions and project.suggestions.shorts and not (body and body.force):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Энэ төсөлд {len(project.suggestions.shorts)} санал аль хэдийн бий. "
                "Дахин авбал одоогийнх солигдож, дахин төлбөр гарна. Баталгаажуулж байж эхлүүлнэ."
            ),
        )
    require_provider(db, providers.LLM)

    duration = project.video.duration_sec if project.video else 0.0
    shorts_max, youtube_max = count_limits(duration)
    shorts_default, youtube_default = default_counts(duration)
    shorts = body.shorts if body and body.shorts is not None else shorts_default
    youtube = body.youtube if body and body.youtube is not None else youtube_default
    if shorts > shorts_max:
        raise HTTPException(
            status_code=422,
            detail=f"Энэ видеоноос дээд тал нь {shorts_max} богино видео гаргах боломжтой.",
        )
    if youtube > youtube_max:
        raise HTTPException(
            status_code=422,
            detail=(
                "20 минутаас богино видеонд YouTube хураангуй гаргахгүй."
                if youtube_max == 0
                else f"Дээд тал нь {youtube_max} YouTube хураангуй гаргах боломжтой."
            ),
        )
    quotas.admit_job(db, principal, "suggest", dedupe_key=f"suggest:{project_id}")
    return {
        "job_id": queue.enqueue(
            "suggest",
            project_id=project_id,
            payload={"shorts": shorts, "youtube": youtube},
            dedupe_key=f"suggest:{project_id}",
            db=db,
        )
    }
