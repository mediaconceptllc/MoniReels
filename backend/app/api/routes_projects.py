"""Projects: create, upload, edit, and start pipeline stages.

Two things every route here obeys:

* **Ownership is checked in the store**, not per route (app.store.get_row),
  so a route added later cannot forget it and expose another account's work.
* **No route does heavy work.** ffprobe, STT, LLM and ffmpeg all run in the
  worker; a handler's job is to validate, queue, and return a job id.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import r2
from app.config import get_settings
from app.db import get_db
from app.dbmodels import Output
from app.jobs import queue
from app.models import Project
from app.schemas import (
    CreateProjectIn,
    CreateProjectOut,
    OutputOut,
    SelectRangesIn,
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
    if not r2.enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured on this server.",
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
            detail="The upload has not arrived in storage yet.",
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
    return [summary(row) for row in list_for_owner(db, principal.id)]


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
        raise HTTPException(status_code=400, detail="This project has no transcript yet")

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
        raise HTTPException(status_code=400, detail="This project has no video")

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


@router.post("/{project_id}/transcribe")
def transcribe(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = _require_project(db, project_id, principal)
    if project.video is None:
        raise HTTPException(status_code=400, detail="Upload a video before transcribing")
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
        raise HTTPException(status_code=400, detail="Transcribe the video first")
    return {
        "job_id": queue.enqueue("suggest", project_id=project_id, dedupe_key=f"suggest:{project_id}")
    }


@router.post("/{project_id}/export-all")
def export_all(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = _require_project(db, project_id, principal)
    if project.suggestions is None or not (project.suggestions.shorts or project.suggestions.youtube):
        raise HTTPException(status_code=400, detail="There are no suggestions to export")
    return {
        "job_id": queue.enqueue(
            "export_all", project_id=project_id, dedupe_key=f"export_all:{project_id}"
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
        raise HTTPException(status_code=400, detail="The timeline has no clips")
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
        raise HTTPException(status_code=404, detail="Output not found")

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
