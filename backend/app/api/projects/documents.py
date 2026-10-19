"""Read, change and delete a project: the list, the page, its settings, its
transcript text, a hand-cut timeline."""
from __future__ import annotations

import json

from fastapi import Depends, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import r2, spend
from app.ai.schema import count_limits, default_counts
from app.api.projects.common import project_404, projects_router
from app.api.projects.guards import voice_view
from app.config import get_settings
from app.db import get_db
from app.dbmodels import Job
from app.jobs import queue
from app.languages import translation_view
from app.models import Project
from app.schemas import SelectRangesIn, UpdateProjectIn, UpdateTranscriptIn
from app.security import Principal, current_user
from app.store import (
    SETTINGS_FIELDS,
    get_row,
    list_for_owner,
    load,
    load_settings,
    save,
    save_settings,
    summary,
    to_domain,
)
from app.timeline.builder import build_clips_from_ranges
from app.tts.voiceover import speakers
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = projects_router()

# How far back the project page's job history reaches. Bounded because the
# list travels inside every project read; the 30-day prune
# (config.job_keep_days) is the other, harder bound, and the payload says so
# rather than letting a short history read as a quiet project.
JOB_HISTORY_LIMIT = 30

#: What PATCH /projects/{id} answers with: every field it can change, as
#: saved, and when. The rest of the document is unchanged by it and the
#: client already has it.
PATCH_ANSWER = {"id", "name", "updated_at", *SETTINGS_FIELDS}


@router.get("")
def list_projects(
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> list[dict]:
    """The list, with each project's thumbnail.

    Import has always made one and the detail page has always signed one; the
    list never asked for it, so a wall of video projects read as a wall of
    text. Signed HERE rather than in `store.summary`, exactly as the detail
    route does — a URL is a fact about this request, not about the row.

    Signing is local (boto3 never leaves the process for it), so this stays
    one database query however long the list is.
    """
    storage = r2.enabled()
    rows = list_for_owner(db, principal.id)
    failed = _failed_imports(db, [row.id for row in rows])
    items = []
    for row in rows:
        item = summary(row)
        item["thumbnail_url"] = (
            r2.presign_get(row.thumbnail_key) if storage and row.thumbnail_key else None
        )
        item["import_failed"] = row.id in failed
        items.append(item)
    return items


def _failed_imports(db: Session, project_ids: list[str]) -> set[str]:
    """Projects whose LATEST import ended in failure, in one query.

    The card otherwise read "Хальс бэлдэж байна" for ever under a video that
    would never be processed (UX-6). A later success, or an upload started
    again, makes the latest import a different one.
    """
    if not project_ids:
        return set()
    latest = (
        select(Job.project_id, Job.state)
        .where(Job.kind == "import_video", Job.project_id.in_(project_ids))
        .distinct(Job.project_id)
        .order_by(Job.project_id, Job.created_at.desc())
    )
    return {pid for pid, state in db.execute(latest).all() if state == "failed"}


def _json_object(document: str, extra: dict) -> Response:
    """One JSON object: an already serialized document plus a few more keys.

    The document is serialized by pydantic in one native pass. It used to go
    through `model_dump` and then FastAPI's `jsonable_encoder`, which walks
    every value of a 4 MB transcript in Python before `json.dumps` walks it
    again — half of a 762 ms read of a 2-hour project (audit PERF-10). The
    added keys are small and go the ordinary way; none may be a document
    field, or the object would name one key twice.
    """
    clash = extra.keys() & Project.model_fields.keys()
    if clash:
        raise ValueError(f"would repeat document fields: {sorted(clash)}")
    tail = json.dumps(jsonable_encoder(extra), ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))
    if not (document.startswith("{") and document.endswith("}") and len(document) > 2):
        raise ValueError("the document must serialize to a non-empty JSON object")
    if not (tail.startswith("{") and tail.endswith("}")):
        raise ValueError("the extra keys must serialize to a JSON object")
    body = document if tail == "{}" else f"{document[:-1]},{tail[1:]}"
    return Response(content=body.encode("utf-8"), media_type="application/json")


@router.get("/{project_id}")
def get_project(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> Response:
    with project_404():
        row = get_row(db, project_id, principal.id)

    project = to_domain(row)
    data: dict = {}
    # Signed, short-lived, and regenerated on every read — a URL embedded in
    # a page the user leaves open would otherwise expire silently mid-session.
    data["media"] = {
        "source_url": r2.presign_get(row.video_key) if row.video_key and r2.enabled() else None,
        "thumbnail_url": (
            r2.presign_get(row.thumbnail_key) if row.thumbnail_key and r2.enabled() else None
        ),
        "expires_in_s": get_settings().r2_presign_ttl_s,
    }
    # Deep enough to BE a history: ten rows is three exports and a retry, and
    # the question this answers — "what ran yesterday, how long did it take,
    # what did it cost" — needs more than the last afternoon.
    data["jobs"] = queue.list_for_project(project_id, limit=JOB_HISTORY_LIMIT, db=db)
    data["job_history_limit"] = JOB_HISTORY_LIMIT
    # The range the picker offers, from the same rule the route enforces. A
    # client that worked it out for itself would be a second copy of the rule,
    # and the first change to either would put a number on the page that the
    # server then refuses.
    duration = (project.video.duration_sec if project.video else 0.0) or 0.0
    shorts_max, youtube_max = count_limits(duration)
    shorts_default, youtube_default = default_counts(duration)
    data["suggest_limits"] = {
        "shorts_max": shorts_max,
        "youtube_max": youtube_max,
        "shorts_default": shorts_default,
        "youtube_default": youtube_default,
    }
    # The export guards' verdicts, read by the page rather than re-derived.
    data["translation"] = translation_view(project)
    data["voice"] = voice_view(db, project)
    # Who speaks, for the per-speaker voice picker: counted and quoted, since
    # a bare `speaker_1` identifies nobody.
    data["speakers"] = speakers(project.transcript.segments) if project.transcript else []
    transcript = (project.transcript.full_text if project.transcript else "") or ""
    data["spend"] = spend.view(
        db,
        project_id=project_id,
        owner_id=row.owner_id,
        characters=len(transcript),
        keep_days=get_settings().job_keep_days,
    )
    return _json_object(project.model_dump_json(), data)


@router.patch("/{project_id}")
def update_project(
    project_id: str,
    body: UpdateProjectIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Partial update.

    The desktop build accepted a whole-document PUT, which is safe when one
    process owns the file and destructive as soon as two tabs are open: the
    second save overwrites fields the first tab changed and this one never
    loaded. Only fields actually present in the request are applied.

    Only the settings are read and written (store.load_settings): the
    transcript never leaves Postgres for a change of `crf`. A document from
    an older schema takes the whole-document path once, which migrates it.
    """
    with project_404():
        project = load_settings(db, project_id, principal.id)
        whole = project is None
        if project is None:
            project = load(db, project_id, principal.id, for_update=True)

    if body.name is not None:
        project.name = body.name
    if body.language is not None:
        project.language = body.language
    for section, target in (
        (body.export, project.export),
        (body.subtitle_style, project.subtitle_style),
        (body.transition, project.transition),
    ):
        if section is None:
            continue
        for field, value in section.model_dump(exclude_none=True).items():
            current = getattr(target, field)
            if isinstance(current, BaseModel):
                # A nested section (the logo) is merged like the one around
                # it: only what was sent changes. Assigned whole, a dict would
                # replace the model and every field not sent would be lost.
                value = current.model_copy(update=value)
            setattr(target, field, value)

    if whole:
        save(db, project)
    else:
        save_settings(db, project)
    db.commit()
    # What this route can change, as saved — not the whole document. It used
    # to answer with all of it: 4 MB for a 2-hour transcript, encoded for a
    # client that threw it away and read the project again (audit PERF-10).
    return project.model_dump(mode="json", include=PATCH_ANSWER)


@router.put("/{project_id}/transcript")
def update_transcript(
    project_id: str,
    body: UpdateTranscriptIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Correct transcript text before asking for suggestions.

    Only the text is writable. Timings come from our own cut boundaries and
    are exact by construction, so accepting them from a client could only
    make them worse — and the AI stage addresses segments by INDEX, so a
    shifted boundary would silently move every cut.
    """
    with project_404():
        project = load(db, project_id, principal.id, for_update=True)
    if project.transcript is None:
        raise HTTPException(status_code=400, detail="Энэ төсөлд транскрипт хараахан алга.")

    edits = {e.id: e for e in body.segments}
    changed = cleared = 0
    for segment in project.transcript.segments:
        edit = edits.get(segment.id)
        if edit is None:
            continue
        touched = False
        if edit.text is not None and edit.text != segment.text:
            segment.text = edit.text
            touched = True
            # A translation of the words that USED to be here is a subtitle
            # that says something nobody said. Cleared rather than kept, so
            # the line shows as untranslated and the next run — which sends
            # only untranslated lines — fixes exactly it. Unless this same
            # edit gave the line a new translation, which then stands.
            if segment.translation and edit.translation is None:
                segment.translation = None
                cleared += 1
        if edit.translation is not None:
            new = edit.translation.strip() or None
            if new != segment.translation:
                segment.translation = new
                touched = True
        changed += touched

    project.transcript.full_text = " ".join(s.text for s in project.transcript.segments if s.text)
    save(db, project)
    db.commit()
    # An id the client sent that matched nothing is named rather than
    # silently dropped: it usually means the client is holding a stale copy.
    unknown = sorted(edits.keys() - {s.id for s in project.transcript.segments})
    return {"updated": changed, "unknown_ids": unknown, "translations_cleared": cleared}


@router.post("/{project_id}/select")
def select_ranges(
    project_id: str,
    body: SelectRangesIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Build the timeline from explicit ranges — the "cut it myself" path
    alongside exporting the AI's suggestions."""
    with project_404():
        project = load(db, project_id, principal.id, for_update=True)
    if project.video is None:
        raise HTTPException(status_code=400, detail="Энэ төсөлд видео алга.")

    duration = project.video.duration_sec
    for start, end in body.ranges:
        if end <= start:
            raise HTTPException(
                status_code=422,
                detail=f"Хэсэг [{start}, {end}] хоосон эсвэл эхлэл нь төгсгөлөөсөө хойш байна.",
            )
        if start < 0 or end > duration + 0.5:
            raise HTTPException(
                status_code=422,
                detail=f"Хэсэг [{start}, {end}] нь {duration:.1f} секундын видеоноос гадуур байна.",
            )

    # source_path is filled in by the worker with the copy it downloads —
    # a server-side path here would be meaningless on another instance.
    project.clips = build_clips_from_ranges("", [(s, e) for s, e in body.ranges])
    save(db, project)
    db.commit()
    return {"clips": len(project.clips)}


@router.delete("/{project_id}")
def delete_project(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    with project_404():
        row = get_row(db, project_id, principal.id, doc=False)

    # Queued work is cancelled FIRST: a worker that claimed a job for a
    # project that no longer exists just fails noisily for no reason.
    queue.drop_project_jobs(project_id, db=db)
    db.delete(row)
    db.commit()

    removed = 0
    if r2.enabled():
        try:
            # Unfinished parts are in no object listing, so delete_prefix
            # cannot see them; a cancelled upload's parts would stay billed.
            r2.abort_uploads_under(f"sources/{project_id}/")
        except Exception:  # noqa: BLE001 - housekeeping aborts leftovers by age anyway
            logger.exception("Failed to abort unfinished uploads for %s", project_id)
        for prefix in (f"sources/{project_id}/", f"outputs/{project_id}/",
                       f"audio/{project_id}/", f"thumbnails/{project_id}/"):
            try:
                removed += r2.delete_prefix(prefix)
            except Exception:  # noqa: BLE001 - the project row is already gone; storage is best effort
                logger.exception("Failed to clean up %s", prefix)
    return {"deleted": True, "objects_removed": removed}
