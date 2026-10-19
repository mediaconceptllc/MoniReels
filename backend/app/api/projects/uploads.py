"""Create a project, upload its video — in one PUT or in parts — and queue
the import once the stored object is verified."""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import quotas, r2
from app.api.projects.common import project_404, projects_router, require_r2
from app.config import get_settings
from app.db import get_db
from app.jobs import queue
from app.models import Project
from app.schemas import (
    MULTIPART_SIGN_BATCH,
    UPLOAD_MAX_BYTES,
    CreateProjectIn,
    CreateProjectOut,
    MultipartPart,
    MultipartSignIn,
    MultipartSignOut,
    MultipartStartOut,
    MultipartStatusOut,
    MultipartUploadIn,
    ReuploadIn,
    UploadCompleteOut,
)
from app.security import Principal, current_user
from app.store import get_row, save, to_domain
from app.utils.logging import get_logger

logger = get_logger(__name__)
router = projects_router()


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
    require_r2()
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
    require_r2()
    with project_404():
        row = get_row(db, project_id, principal.id, for_update=True)
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
    require_r2()
    settings = get_settings()
    with project_404():
        row = get_row(db, project_id, principal.id, for_update=True)
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
    require_r2()
    with project_404():
        row = get_row(db, project_id, principal.id)
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
