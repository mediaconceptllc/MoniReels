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
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import providers, quotas, r2, security, spend, worker_report
from app.ai.schema import count_limits, default_counts
from app.config import get_settings
from app.db import get_db
from app.dbmodels import Job, Output, SubtitleTemplate
from app.export.ideas import idea_ranges, pick_ideas
from app.jobs import queue
from app.languages import (
    needs_translation,
    removes_source_speech,
    suggested_ranges,
    translation_holes,
    translation_uses,
    translation_view,
    voice_over_on,
)
from app.models import Project
from app.schemas import (
    MULTIPART_SIGN_BATCH,
    UPLOAD_MAX_BYTES,
    CreateProjectIn,
    CreateProjectOut,
    ExportSelectionIn,
    MultipartPart,
    MultipartSignIn,
    MultipartSignOut,
    MultipartStartOut,
    MultipartStatusOut,
    MultipartUploadIn,
    OutputOut,
    ReuploadIn,
    SelectRangesIn,
    SubtitleTemplateIn,
    SuggestIn,
    TranscribeIn,
    TranslateIn,
    UpdateProjectIn,
    UpdateTranscriptIn,
    UploadCompleteOut,
)
from app.security import Principal, current_user
from app.store import ProjectNotFound, get_row, list_for_owner, load, save, summary, to_domain
from app.timeline.builder import build_clips_from_ranges
from app.tts.voiceover import speakers
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])

# How far back the project page's job history reaches. Bounded because the
# list travels inside every project read; the 30-day prune
# (config.job_keep_days) is the other, harder bound, and the payload says so
# rather than letting a short history read as a quiet project.
JOB_HISTORY_LIMIT = 30


def _not_found(project_id: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Төсөл олдсонгүй.")


def _require_r2() -> None:
    """Refuse the request while naming the variable that is wrong.

    The reason is safe to hand back: it names environment variables, never
    their values.
    """
    problem = get_settings().r2_config_error
    if problem:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Файлын сан (R2) энэ сервер дээр тохируулагдаагүй байна: {problem}",
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
    quotas.admit_project(db, principal)

    project = Project(
        id=uuid.uuid4().hex,
        name=body.name,
        language=body.language,
        upload_size_bytes=body.size_bytes,
    )
    # Derived from the project id, which never changes — a rename must not
    # orphan the object that was already uploaded under the old name. Held
    # as the PENDING key: the row's video_key is set only by upload-complete,
    # so a cancelled upload is not listed as a video (UX-6).
    key = r2.source_key(project.id, Path(body.filename).suffix)
    project.upload_key = key
    save(db, project, owner_id=principal.id)
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

    Its SIZE is verified too. The presigned PUT binds no length, so the
    `size_bytes` checked at creation was only ever a claim: a client could
    declare 1 KB and store 50 GB, which the import would then download and
    the bucket bill for. An object that is not what was declared, or is over
    the limit, is deleted and refused here.
    """
    _require_r2()
    try:
        row = get_row(db, project_id, principal.id, for_update=True)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    project = to_domain(row)
    # The pending key; a project made before it existed set video_key at
    # creation and still uploads there.
    key = project.upload_key or row.video_key
    size = r2.object_size(key) if key else None
    if key is None or size is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Хуулсан файл хадгалах санд хараахан ирээгүй байна.",
        )
    refused = _upload_refusal(size, project.upload_size_bytes)
    if refused:
        try:
            r2.delete(key)
        except Exception:  # noqa: BLE001 - the refusal stands either way
            logger.exception("Could not delete refused upload %s", key)
        raise HTTPException(status_code=refused[0], detail=refused[1])

    # Verified: only now is this project's video "in".
    row.video_key = key
    if project.upload_key:
        project.upload_key = None
        save(db, project)
    job_id = queue.enqueue(
        "import_video", project_id=project_id, dedupe_key=f"import:{project_id}", db=db
    )
    return UploadCompleteOut(project_id=project_id, job_id=job_id)


def _size(n: int) -> str:
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.1f} MB"


def _upload_refusal(size: int, declared: object) -> tuple[int, str] | None:
    """(status, reason) when the stored object must not be imported."""
    if size > UPLOAD_MAX_BYTES:
        return 413, (
            f"Хуулсан файл ({_size(size)}) дээд хэмжээнээс ({_size(UPLOAD_MAX_BYTES)}) том байна. "
            "Файл устгагдлаа."
        )
    # bool is an int to Python; a declared `true` is not a size.
    if isinstance(declared, int) and not isinstance(declared, bool) and size != declared:
        return 400, (
            f"Хуулсан файлын хэмжээ ({size} байт) зарласнаас ({declared} байт) өөр байна — "
            "хуулалт дутуу эсвэл өөр файл. Файл устгагдлаа; шинэ төсөл үүсгэж дахин хуулна уу."
        )
    return None


# ---------------------------------------------------------------------------
# Upload again, and multipart upload (audit UP-1, UX-6, UX-7)
# ---------------------------------------------------------------------------

_UPLOAD_GONE = (
    "Хуулалт олдсонгүй: хугацаа нь дууссан эсвэл цуцлагдсан байна. Видеог дахин хуулна уу."
)


@router.post("/{project_id}/upload", response_model=CreateProjectOut)
def reupload(
    project_id: str,
    body: ReuploadIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> CreateProjectOut:
    """A new source for a project whose video never arrived, was refused, or
    failed to import. Before this a failed import left the project saying
    "Видео боловсруулагдаж байна…" for ever, with no way out but deleting it.

    Only before a successful import: a transcript and ideas belong to the
    video they were made from, and swapping it under them would desync both.
    """
    _require_r2()
    settings = get_settings()
    try:
        row = get_row(db, project_id, principal.id, for_update=True)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    project = to_domain(row)
    if project.video is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ төслийн видео аль хэдийн боловсруулагдсан. Өөр видеонд шинэ төсөл үүсгэнэ үү.",
        )
    if queue.active_with_key(db, f"import:{project_id}"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Видео боловсруулагдаж байна. Дуусах эсвэл цуцлагдахыг хүлээнэ үү.",
        )
    _discard_source(project_id)

    key = r2.source_key(project.id, Path(body.filename).suffix)
    row.video_key = None
    project.upload_key = key
    project.upload_size_bytes = body.size_bytes
    save(db, project)
    db.commit()
    return CreateProjectOut(
        project_id=project.id,
        upload_url=r2.presign_put(key, r2.content_type_for(body.filename)),
        upload_key=key,
        upload_expires_in_s=settings.r2_presign_ttl_s,
    )


def _discard_source(project_id: str) -> None:
    """Whatever an earlier attempt left: unfinished parts (billed, and in no
    listing) and a stored object, possibly under another suffix. Best effort
    — the prefix is the project's own, so deleting the project removes any
    leftover anyway."""
    prefix = f"sources/{project_id}/"
    for what, step in (("abort uploads under", r2.abort_uploads_under), ("delete", r2.delete_prefix)):
        try:
            step(prefix)
        except Exception:  # noqa: BLE001 - see the docstring
            logger.exception("Could not %s %s", what, prefix)


def _pending_upload(db: Session, project_id: str, principal: Principal) -> tuple[str, int]:
    """(key, declared size) of the upload in progress, or the HTTP error
    that says why there is none."""
    _require_r2()
    try:
        row = get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
    project = to_domain(row)
    if not project.upload_key or not project.upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Энэ төсөлд хуулагдаж буй видео алга.",
        )
    return project.upload_key, project.upload_size_bytes


@router.post("/{project_id}/upload/multipart", response_model=MultipartStartOut)
def start_multipart(
    project_id: str,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> MultipartStartOut:
    """Begin a multipart upload — or say a single PUT will do.

    The decision is made here, from the size declared at creation, so the
    browser holds no second copy of the rule. A client that predates this
    route gets a 404 and falls back to the PUT it was already given.
    """
    key, size = _pending_upload(db, project_id, principal)
    if size < r2.MULTIPART_MIN_BYTES:
        return MultipartStartOut(mode="single")
    # One upload per project: an earlier attempt's parts are billed until
    # aborted, and only this one will ever be completed.
    try:
        r2.abort_uploads_under(f"sources/{project_id}/")
    except Exception:  # noqa: BLE001 - housekeeping aborts leftovers by age anyway
        logger.exception("Could not abort earlier uploads for %s", project_id)
    upload_id = r2.create_multipart(key, r2.content_type_for(key))
    part_size, part_count = r2.part_plan(size)
    return MultipartStartOut(
        mode="multipart",
        upload_id=upload_id,
        part_size=part_size,
        part_count=part_count,
        sign_batch=MULTIPART_SIGN_BATCH,
    )


@router.post("/{project_id}/upload/multipart/sign", response_model=MultipartSignOut)
def sign_parts(
    project_id: str,
    body: MultipartSignIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> MultipartSignOut:
    """Presigned PUTs for some parts. Signed in batches as the upload goes,
    so a 20 GB upload that takes longer than one URL lives does not hit an
    expired one. The key is the project's own; a wrong upload id only gets
    URLs that R2 refuses."""
    key, size = _pending_upload(db, project_id, principal)
    _, part_count = r2.part_plan(size)
    beyond = [n for n in body.part_numbers if n > part_count]
    if beyond:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Энэ файл {part_count} хэсэгтэй — {beyond[0]}-р хэсэг байхгүй.",
        )
    return MultipartSignOut(
        urls={str(n): r2.presign_upload_part(key, body.upload_id, n) for n in sorted(set(body.part_numbers))},
        expires_in_s=get_settings().r2_presign_ttl_s,
    )


@router.post("/{project_id}/upload/multipart/status", response_model=MultipartStatusOut)
def multipart_status(
    project_id: str,
    body: MultipartUploadIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> MultipartStatusOut:
    """The parts R2 already holds, so a reloaded page uploads only the rest."""
    key, size = _pending_upload(db, project_id, principal)
    try:
        parts = r2.list_parts(key, body.upload_id)
    except r2.NoSuchUpload as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UPLOAD_GONE) from e
    part_size, part_count = r2.part_plan(size)
    return MultipartStatusOut(
        part_size=part_size,
        part_count=part_count,
        sign_batch=MULTIPART_SIGN_BATCH,
        parts=[MultipartPart(part_number=p["part_number"], size=p["size"]) for p in parts],
    )


@router.post("/{project_id}/upload/multipart/complete")
def complete_multipart(
    project_id: str,
    body: MultipartUploadIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Assemble the object — from R2's own list of parts, never the
    browser's: every part must be there, each the size the plan says, or
    nothing is assembled. Then the browser calls upload-complete, which
    verifies the object and queues the import exactly as for a single PUT."""
    key, size = _pending_upload(db, project_id, principal)
    try:
        parts = r2.list_parts(key, body.upload_id)
    except r2.NoSuchUpload as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UPLOAD_GONE) from e
    _, part_count = r2.part_plan(size)
    held = {p["part_number"]: p for p in parts}
    missing = [n for n in range(1, part_count + 1) if n not in held]
    wrong = [
        n for n, p in held.items()
        if n > part_count or p["size"] != r2.expected_part_size(size, n)
    ]
    if missing or wrong:
        problems = []
        if missing:
            problems.append(f"{len(missing)} хэсэг ирээгүй")
        if wrong:
            problems.append(f"{len(wrong)} хэсгийн хэмжээ буруу")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Хуулалт дутуу байна: {', '.join(problems)}. Үргэлжлүүлж хуулна уу.",
        )
    try:
        r2.complete_multipart(key, body.upload_id, [held[n] for n in range(1, part_count + 1)])
    except r2.NoSuchUpload as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UPLOAD_GONE) from e
    return {"completed": True, "parts": part_count}


@router.post("/{project_id}/upload/multipart/abort")
def abort_multipart(
    project_id: str,
    body: MultipartUploadIn,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Throw away the stored parts of a cancelled upload: they are billed
    until aborted, and appear in no object listing."""
    key, _ = _pending_upload(db, project_id, principal)
    return {"aborted": r2.abort_multipart(key, body.upload_id)}


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

    project = to_domain(row)
    data = project.model_dump(mode="json")
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
    duration = (data.get("video") or {}).get("duration_sec") or 0.0
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
    transcript = (data.get("transcript") or {}).get("full_text") or ""
    data["spend"] = spend.view(
        db,
        project_id=project_id,
        owner_id=row.owner_id,
        characters=len(transcript),
        keep_days=get_settings().job_keep_days,
    )
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
        project = load(db, project_id, principal.id, for_update=True)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e

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
        project = load(db, project_id, principal.id, for_update=True)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
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
    try:
        project = load(db, project_id, principal.id, for_update=True)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e
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
    try:
        row = get_row(db, project_id, principal.id)
    except ProjectNotFound as e:
        raise _not_found(project_id) from e

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


def _require_translation_ready(project: Project, ranges: list[tuple[float, float]]) -> None:
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


def _require_voice_ready(db: Session, project: Project) -> None:
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


def _require_recogniser_for(db: Session, language: str) -> None:
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


@router.post("/{project_id}/transcribe")
def transcribe(
    project_id: str,
    body: TranscribeIn | None = None,
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    project = _require_project(db, project_id, principal)
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
    _require_recogniser_for(db, project.language)
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
    project = _require_project(db, project_id, principal)
    if not needs_translation(project.language):
        raise HTTPException(status_code=400, detail="Энэ видео монгол хэлтэй — орчуулах шаардлагагүй.")
    if project.transcript is None or not project.transcript.segments:
        raise HTTPException(status_code=400, detail="Эхлээд яриаг таниулна уу.")
    scope = body.scope if body else "all"
    if scope == "suggested" and not suggested_ranges(project):
        raise HTTPException(status_code=400, detail="Санал алга — эхлээд санал боловсруулна уу.")
    _require_provider(db, providers.LLM)
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
    project = _require_project(db, project_id, principal)
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
    _require_provider(db, providers.LLM)

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


@router.get("/tts/voices", include_in_schema=False)
async def tts_voices(
    principal: Principal = Depends(current_user),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """The voices a speaker can be given, for the page that gives them.

    Thinner than the admin view, like `providers/status`: the voices, the
    default a speaker without one falls back to, and why the list could not
    be read — nothing about the model or the account. Free: ElevenLabs is
    asked for its list and nothing is synthesised. Never raises; an
    unreachable provider is the answer, not a 500.
    """
    from app import provider_settings
    from app.tts import elevenlabs

    settings = provider_settings.effective(db)
    out: dict = {
        "voices": [],
        "default_voice_id": settings.elevenlabs_tts_voice_id or None,
        "error": None,
    }
    if not settings.elevenlabs_api_key:
        out["error"] = "ElevenLabs-ийн API түлхүүр тавигдаагүй байна."
        return out
    client = elevenlabs.build_client(settings)
    try:
        out["voices"] = await client.voices()
    except Exception as e:  # noqa: BLE001 - reachability is part of the answer
        out["error"] = str(e) or type(e).__name__
    finally:
        await client.aclose()
    return out


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

    # The worker's own selection (export.ideas.pick_ideas), so the lines asked
    # about here are exactly the lines it will render.
    wanted, _ = pick_ideas(project.suggestions, {"pick": pick} if pick else {})
    _require_translation_ready(project, idea_ranges(wanted))
    _require_voice_ready(db, project)

    # The selection is part of the job's identity. Without it, exporting one
    # short and then another while the first is queued would hand back the
    # FIRST job's id and render the wrong thing under the right progress bar.
    digest = hashlib.sha256(
        json.dumps(pick, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12] if pick else "all"
    key = f"export_all:{project_id}:{digest}"
    # A voice-over buys speech; without one an export bills nothing outside.
    quotas.admit_job(db, principal, "export_all", paid=voice_over_on(project), dedupe_key=key)
    return {
        "job_id": queue.enqueue(
            "export_all",
            project_id=project_id,
            payload={"pick": pick} if pick else None,
            dedupe_key=key,
            db=db,
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
    _require_translation_ready(project, [(c.start, c.end) for c in project.clips])
    _require_voice_ready(db, project)
    # Keyed by what the render reads, not by the project alone: re-exporting
    # after changing the clips, the settings or the text is a normal thing to
    # want and gets its own job, while a second click on the SAME render gets
    # the first one back. There was no key at all, and 25 identical exports
    # queued behind one another (SEC-4).
    render = project.model_dump(
        mode="json",
        include={"language", "clips", "transition", "subtitle_style", "export", "transcript"},
    )
    digest = hashlib.sha256(
        json.dumps(render, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]
    key = f"export:{project_id}:{digest}"
    quotas.admit_job(db, principal, "export", paid=voice_over_on(project), dedupe_key=key)
    return {"job_id": queue.enqueue("export", project_id=project_id, dedupe_key=key, db=db)}


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
    # Outputs rendered before keys carried the job id could share a key — a
    # later export wrote over the earlier file. Such a key now holds the file
    # of a row that is still listed; removing it would delete that video.
    still_used = set(db.scalars(select(Output.r2_key).where(Output.r2_key.in_(keys)))) | set(
        db.scalars(select(Output.srt_key).where(Output.srt_key.in_(keys)))
    )
    if r2.enabled():
        for key in keys:
            if key in still_used:
                continue
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
