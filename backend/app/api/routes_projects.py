"""Projects: create, upload, edit, and start pipeline stages.

Two things every route here obeys:

* **Ownership is checked in the store**, not per route (app.store.get_row),
  so a route added later cannot forget it and expose another account's work.
* **No route does heavy work.** ffprobe, STT, LLM and ffmpeg all run in the
  worker; a handler's job is to validate, queue, and return a job id.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import providers, r2, security
from app.config import get_settings
from app.db import get_db
from app.dbmodels import Output, SubtitleTemplate
from app.jobs import queue
from app.models import Project
from app.schemas import (
    CreateProjectIn,
    CreateProjectOut,
    ExportSelectionIn,
    OutputOut,
    SelectRangesIn,
    SubtitleTemplateIn,
    UpdateProjectIn,
    UpdateTranscriptIn,
    UploadCompleteOut,
)
from app.security import Principal, current_user
from app.store import ProjectNotFound, get_row, list_for_owner, load, save, summary, to_domain
from app.timeline.builder import build_clips_from_ranges
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


def _not_found(project_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Project {project_id} not found")


def _require_r2() -> None:
    """Refuse the request while naming the variable that is wrong.

    The reason is safe to hand back: it names environment variables, never
    their values.
    """
    problem = get_settings().r2_config_error
    if problem:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Object storage is not configured on this server: {problem}",
        )


# ---------------------------------------------------------------------------
# Create + upload
# ---------------------------------------------------------------------------


@router.post("", response_model=CreateProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: CreateProjectIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> CreateProjectOut:
    """Create the project and hand back a presigned PUT.

    The browser uploads straight to R2. Routing a multi-gigabyte video
    through this process would exhaust the request timeout and the dyno's
    memory, and pay for the same bytes twice.
    """
    _require_r2()
    settings = get_settings()

    project = Project(id=uuid.uuid4().hex, name=body.name)
    row = save(db, project, owner_id=principal.id)

    # Derived from the project id, which never changes — a rename must not
    # orphan the object that was already uploaded under the old name.
    key = r2.source_key(project.id, Path(body.filename).suffix)
    row.video_key = key
    db.commit()

    return CreateProjectOut(
        project_id=project.id,
        upload_url=r2.presign_put(key, r2.content_type_for(body.filename)),
        upload_key=key,
        upload_expires_in_s=settings.r2_presign_ttl_s,
    )


@router.post("/{project_id}/upload-complete", response_model=UploadCompleteOut)
def upload_complete(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> UploadCompleteOut:
    """Called by the browser once its PUT finishes; queues the import.

    The object is verified to exist before queueing rather than trusted: a
    PUT that failed client-side would otherwise produce a job that downloads
    nothing and fails with a confusing storage error.
    """
    _require_r2()
    try:
        row = get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    if not row.video_key or not r2.exists(row.video_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Хуулсан файл хадгалах санд хараахан ирээгүй байна.",
        )

    job_id = queue.enqueue(
        "import_video", project_id=project_id, dedupe_key=f"import:{project_id}"
    )
    return UploadCompleteOut(project_id=project_id, job_id=job_id)


# ---------------------------------------------------------------------------
# Read / update / delete
# ---------------------------------------------------------------------------


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
    items = []
    for row in list_for_owner(db, principal.id):
        item = summary(row)
        item["thumbnail_url"] = (
            r2.presign_get(row.thumbnail_key) if storage and row.thumbnail_key else None
        )
        items.append(item)
    return items


@router.get("/{project_id}")
def get_project(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    try:
        row = get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e

    data = to_domain(row).model_dump(mode="json")
    # Signed, short-lived, and regenerated on every read — a URL embedded in
    # a page the user leaves open would otherwise expire silently mid-session.
    data["media"] = {
        "source_url": r2.presign_get(row.video_key) if row.video_key and r2.enabled() else None,
        "thumbnail_url": (
            r2.presign_get(row.thumbnail_key) if row.thumbnail_key and r2.enabled() else None
        ),
        "expires_in_s": get_settings().r2_presign_ttl_s,
    }
    data["jobs"] = queue.list_for_project(project_id, limit=10)
    return data


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
    """
    try:
        project = load(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e

    if body.name is not None:
        project.name = body.name
    for section, target in (
        (body.export, project.export),
        (body.subtitle_style, project.subtitle_style),
        (body.transition, project.transition),
    ):
        if section is None:
            continue
        for field, value in section.model_dump(exclude_none=True).items():
            setattr(target, field, value)

    save(db, project)
    db.commit()
    return project.model_dump(mode="json")


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
    try:
        project = load(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    if project.transcript is None:
        raise HTTPException(status_code=400, detail="Энэ төсөлд транскрипт хараахан алга.")

    edits = {e.id: e.text for e in body.segments}
    changed = 0
    for segment in project.transcript.segments:
        new_text = edits.get(segment.id)
        if new_text is not None and new_text != segment.text:
            segment.text = new_text
            changed += 1

    project.transcript.full_text = " ".join(s.text for s in project.transcript.segments if s.text)
    save(db, project)
    db.commit()
    # An id the client sent that matched nothing is named rather than
    # silently dropped: it usually means the client is holding a stale copy.
    unknown = sorted(edits.keys() - {s.id for s in project.transcript.segments})
    return {"updated": changed, "unknown_ids": unknown}


@router.post("/{project_id}/select")
def select_ranges(
    project_id: str,
    body: SelectRangesIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Build the timeline from explicit ranges — the "cut it myself" path
    alongside exporting the AI's suggestions."""
    try:
        project = load(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    if project.video is None:
        raise HTTPException(status_code=400, detail="Энэ төсөлд видео алга.")

    duration = project.video.duration_sec
    for start, end in body.ranges:
        if end <= start:
            raise HTTPException(status_code=422, detail=f"Range [{start}, {end}] is empty or reversed")
        if start < 0 or end > duration + 0.5:
            raise HTTPException(
                status_code=422, detail=f"Range [{start}, {end}] falls outside the {duration:.1f}s video"
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
    try:
        row = get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e

    # Queued work is cancelled FIRST: a worker that claimed a job for a
    # project that no longer exists just fails noisily for no reason.
    queue.drop_project_jobs(project_id)
    db.delete(row)
    db.commit()

    removed = 0
    if r2.enabled():
        for prefix in (f"sources/{project_id}/", f"outputs/{project_id}/",
                       f"audio/{project_id}/", f"thumbnails/{project_id}/"):
            try:
                removed += r2.delete_prefix(prefix)
            except Exception:  # noqa: BLE001 - the project row is already gone; storage is best effort
                logger.exception("Failed to clean up %s", prefix)
    return {"deleted": True, "objects_removed": removed}


# ---------------------------------------------------------------------------
# Pipeline stages — each queues one job
# ---------------------------------------------------------------------------


def _require_provider(db: Session, capability: str) -> None:
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


@router.post("/{project_id}/transcribe")
def transcribe(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = _require_project(db, project_id, principal)
    if project.video is None:
        raise HTTPException(status_code=400, detail="Эхлээд видео оруулна уу.")
    _require_provider(db, providers.STT)
    return {
        "job_id": queue.enqueue(
            "transcribe", project_id=project_id, dedupe_key=f"transcribe:{project_id}"
        )
    }


@router.post("/{project_id}/suggest")
def suggest(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = _require_project(db, project_id, principal)
    if project.transcript is None or not project.transcript.segments:
        raise HTTPException(status_code=400, detail="Эхлээд яриаг таниулна уу.")
    _require_provider(db, providers.LLM)
    return {
        "job_id": queue.enqueue("suggest", project_id=project_id, dedupe_key=f"suggest:{project_id}")
    }


@router.get("/subtitle/templates", include_in_schema=False)
def list_subtitle_templates(
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """The studio's saved subtitle styles, newest first.

    Readable by anyone signed in: a template nobody can apply is not a house
    style. Creating and deleting one is admin's, the same line the brand
    assets draw.
    """
    rows = db.scalars(
        select(SubtitleTemplate).order_by(SubtitleTemplate.created_at.desc())
    ).all()
    return {
        "templates": [
            {"id": r.id, "name": r.name, "style": r.style, "created_at": r.created_at}
            for r in rows
        ]
    }


@router.post("/subtitle/templates", include_in_schema=False, status_code=status.HTTP_201_CREATED)
def create_subtitle_template(
    body: SubtitleTemplateIn,
    principal: Principal = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Save a style under a name.

    The style is validated through SubtitleStyle on the way in, so a template
    cannot carry a font this image lacks — a saved house style that silently
    renders in something else is worse than no template at all.
    """
    if db.scalar(select(SubtitleTemplate).where(SubtitleTemplate.name == body.name)):
        raise HTTPException(status_code=409, detail="Ийм нэртэй загвар аль хэдийн бий.")

    row = SubtitleTemplate(
        id=uuid.uuid4().hex, name=body.name, style=body.style.model_dump(), created_at=time.time()
    )
    db.add(row)
    db.commit()
    return {"id": row.id, "name": row.name, "style": row.style, "created_at": row.created_at}


@router.delete("/subtitle/templates/{template_id}", include_in_schema=False)
def delete_subtitle_template(
    template_id: str,
    principal: Principal = Depends(security.require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    row = db.get(SubtitleTemplate, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Загвар олдсонгүй.")
    db.delete(row)
    db.commit()
    # Projects already using it keep their own copy: a template is a starting
    # point that was COPIED into the project, not a live link. Deleting one
    # must not restyle finished work.
    return {"deleted": template_id}


@router.get("/subtitle/fonts", include_in_schema=False)
def subtitle_fonts(principal: Principal = Depends(current_user)) -> dict:  # noqa: B008
    """Families this image can really render subtitles in.

    Two path segments, not one: a single-segment `/projects/subtitle-fonts`
    is matched by `/projects/{project_id}` first and comes back as "project
    not found" — same reason `providers/status` is shaped this way.

    Asked of the image rather than hard-coded, because libass substitutes a
    missing family without a word: a typed-in font name is a setting that
    looks applied and is not.
    """
    from app.subtitle import fonts

    return {"families": list(fonts.available()), "default": fonts.DEFAULT_FAMILY}


@router.get("/providers/status", include_in_schema=False)
def provider_readiness(
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """What can and cannot run, for the page that has the paid buttons.

    Deliberately thinner than the admin view: no balances, no key hints, no
    reachability probe. Someone who cannot change a setting still has to know
    why a button will not work, and that is all this says.
    """
    from app import provider_settings, providers

    settings = provider_settings.effective(db)
    return {
        "capabilities": [
            {"name": c.name, "label": c.label, "ready": c.ready, "blocked": c.blocked}
            for c in providers.describe(settings)
        ]
    }


@router.post("/{project_id}/export-all")
def export_all(
    project_id: str,
    body: ExportSelectionIn | None = None,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Render the model's ideas — all of them, or the ones named.

    The body is optional and an omitted one still means everything, so a
    client that predates choosing keeps working unchanged.

    An id that is not in the CURRENT suggestions is refused here rather than
    dropped later: the producer is standing at the button, and "2 of the 3 you
    picked" discovered twenty minutes into a render is the worst place to find
    out.
    """
    project = _require_project(db, project_id, principal)
    if project.suggestions is None or not (project.suggestions.shorts or project.suggestions.youtube):
        raise HTTPException(status_code=400, detail="Экспортлох санал алга.")

    pick: dict = {}
    if body and body.shorts is not None:
        known = {s.id for s in project.suggestions.shorts}
        missing = [i for i in body.shorts if i not in known]
        if missing:
            raise HTTPException(
                status_code=422, detail=f"Ийм богино видео алга: {', '.join(missing)}"
            )
        pick["shorts"] = body.shorts
    if body and body.youtube is not None:
        plans = project.suggestions.youtube
        out_of_range = [i for i in body.youtube if not 0 <= i < len(plans)]
        if out_of_range:
            raise HTTPException(
                status_code=422,
                detail=f"YouTube хураангуйн дугаар буруу: {out_of_range}",
            )
        # The title travels with the index so the worker can tell whether the
        # plan it is about to render is still the one that was chosen.
        pick["youtube"] = [{"i": i, "title": plans[i].title} for i in body.youtube]

    if pick and not (pick.get("shorts") or pick.get("youtube")):
        raise HTTPException(status_code=400, detail="Нэг ч санал сонгогдоогүй байна.")

    # The selection is part of the job's identity. Without it, exporting one
    # short and then another while the first is queued would hand back the
    # FIRST job's id and render the wrong thing under the right progress bar.
    digest = hashlib.sha256(
        json.dumps(pick, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12] if pick else "all"
    return {
        "job_id": queue.enqueue(
            "export_all",
            project_id=project_id,
            payload={"pick": pick} if pick else None,
            dedupe_key=f"export_all:{project_id}:{digest}",
        )
    }


@router.post("/{project_id}/export")
def export_timeline(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = _require_project(db, project_id, principal)
    if not project.clips:
        raise HTTPException(status_code=400, detail="Timeline дээр клип алга.")
    # No dedupe key: re-exporting the same timeline after changing render
    # settings is a normal thing to want, unlike re-running a paid stage.
    return {"job_id": queue.enqueue("export", project_id=project_id)}


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@router.get("/{project_id}/outputs", response_model=list[OutputOut])
def list_outputs(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> list[OutputOut]:
    try:
        row = get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e

    counts: dict[str, int] = {}
    result: list[OutputOut] = []
    for out in sorted(row.outputs, key=lambda o: o.created_at):
        counts[out.kind] = counts.get(out.kind, 0) + 1
        name = r2.download_name(row.name, out.kind, counts[out.kind], "mp4")
        result.append(
            OutputOut(
                id=out.id,
                kind=out.kind,
                title=out.title,
                duration_sec=out.duration_sec,
                size_bytes=out.size_bytes,
                created_at=out.created_at,
                # Separate URLs: the download one carries a Content-Disposition
                # that makes a browser save instead of play.
                play_url=r2.presign_get(out.r2_key),
                download_url=r2.presign_get(out.r2_key, filename=name),
                srt_url=r2.presign_get(out.srt_key) if out.srt_key else None,
            )
        )
    return result


@router.delete("/{project_id}/outputs/{output_id}")
def delete_output(
    project_id: str,
    output_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    try:
        get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    out = db.get(Output, output_id)
    if out is None or out.project_id != project_id:
        raise HTTPException(status_code=404, detail="Гаралт олдсонгүй.")

    keys = [out.r2_key] + ([out.srt_key] if out.srt_key else [])
    db.delete(out)
    db.commit()
    if r2.enabled():
        for key in keys:
            try:
                r2.delete(key)
            except Exception:  # noqa: BLE001 - the row is gone; storage is best effort
                logger.exception("Failed to delete %s", key)
    return {"deleted": True}


def _require_project(db: Session, project_id: str, principal: Principal) -> Project:
    try:
        return load(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
