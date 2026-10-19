"""Cloudflare R2 object storage — the single home for every byte of media.

Two rules this module exists to enforce:

1. **Media never passes through the API.** Uploads are a presigned PUT the
   browser performs directly; downloads and playback are a presigned GET.
   Streaming a multi-GB source video through a Railway dyno costs a request
   timeout, the dyno's whole memory budget, and egress twice over.

2. **A key is an address, a filename is presentation.** Object keys are
   derived from the project id and never change — renaming a project must
   not orphan the objects that already exist under the old name. The
   human-friendly name a browser saves the file under is set per-request via
   the presigned URL's `response-content-disposition`, so no migration is
   needed and files uploaded before a naming change still download correctly.

R2 is S3-compatible; boto3 talks to it with SigV4 against the account
endpoint. Region must be the literal "auto" — R2 rejects a real AWS region.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)

_client = None
_client_lock = threading.Lock()

# Prefixes the sweeper is allowed to touch. Anything outside them is either
# an upload in flight or something a human put there deliberately.
MANAGED_PREFIXES = ("sources/", "outputs/", "audio/", "thumbnails/")

# Ceiling on the multipart listing walk. 1000 uploads a page is the S3
# maximum, so this covers far more leftovers than a bucket should ever
# hold, and bounds a walk that otherwise depends entirely on the server
# eventually saying IsTruncated: false.
MAX_MPU_PAGES = 100

_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# One path segment of a key: no dots either, so it can never read as `..`.
_SEGMENT_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


class R2Error(Exception):
    pass


class R2Disabled(R2Error):
    """Raised instead of a confusing boto3 error when R2 is unconfigured."""


def _client_or_raise():
    global _client
    settings = get_settings()
    problem = settings.r2_config_error
    if problem:
        raise R2Disabled(problem)
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from botocore.config import Config

                _client = boto3.client(
                    "s3",
                    endpoint_url=settings.resolved_r2_endpoint,
                    aws_access_key_id=settings.r2_access_key_id,
                    aws_secret_access_key=settings.r2_secret_access_key,
                    # R2 only accepts "auto"; a real AWS region name is
                    # rejected outright at signature verification.
                    region_name="auto",
                    config=Config(
                        signature_version="s3v4",
                        retries={"max_attempts": 3, "mode": "standard"},
                        # Connect only. A TCP handshake to a reachable R2
                        # never takes ten seconds, so this turns "endpoint
                        # unreachable" from three minutes of silence into a
                        # prompt error. read_timeout keeps botocore's 60s:
                        # this same client streams source videos and renders,
                        # and a tighter read would abandon a large transfer
                        # over a slow link that is working fine.
                        connect_timeout=10,
                    ),
                )
    return _client


def enabled() -> bool:
    return get_settings().r2_enabled


# ---------------------------------------------------------------------------
# Key construction. Every key is rooted at the project id, which is assigned
# once and never changes.
# ---------------------------------------------------------------------------


def source_key(project_id: str, suffix: str) -> str:
    return f"sources/{project_id}/source{_safe_suffix(suffix)}"


def audio_key(project_id: str, name: str) -> str:
    return f"audio/{project_id}/{safe_filename(name)}"


def thumbnail_key(project_id: str) -> str:
    return f"thumbnails/{project_id}/thumb.jpg"


def output_key(project_id: str, job_id: str, kind: str, index: int, ext: str) -> str:
    """One rendered file. The JOB is part of the key.

    The index restarts at 1 in every export, so project + kind + index alone
    made a second export write over the first one's files: the older rows
    went on to play the new video, and deleting an "old" output deleted the
    new one. A retry of the same job lands on the same keys on purpose — the
    worker updates the row it already wrote instead of adding another.
    """
    job = _SEGMENT_UNSAFE.sub("_", job_id).strip("_") or "job"
    return f"outputs/{project_id}/{job}/{kind}_{index}{_safe_suffix('.' + ext.lstrip('.'))}"


def _safe_suffix(suffix: str) -> str:
    """One leading dot plus alphanumerics — nothing else.

    The suffix comes from a filename the client chose. Allowing dots or
    slashes through would let a crafted name reach outside the project's own
    prefix, or produce keys like `source....etcp` that are merely confusing.
    Reducing to `[a-z0-9]` removes both possibilities outright rather than
    filtering for the specific sequences known to be dangerous.
    """
    stem = re.sub(r"[^A-Za-z0-9]", "", (suffix or "").rsplit(".", 1)[-1])
    return f".{stem.lower()[:7]}" if stem else ".mp4"


def safe_filename(name: str) -> str:
    """ASCII-only filename for a Content-Disposition header.

    Cyrillic needs RFC 5987 encoding that older browsers mangle, and a quote
    or a slash breaks the header itself — so the download name is transliterated
    down to ASCII rather than trusted through.
    """
    stem, _, ext = name.rpartition(".")
    if not stem:
        stem, ext = name, ""
    safe = _ASCII_UNSAFE.sub("_", stem).strip("_") or "file"
    ext = _ASCII_UNSAFE.sub("", ext)
    return f"{safe[:120]}.{ext}" if ext else safe[:120]


def download_name(project_name: str, kind: str, index: int, ext: str) -> str:
    """`MyProject-Reel-2.mp4`.

    Both the project and the item index are in the name: downloading six
    outputs from one project otherwise lands `reel(1).mp4`, `reel(2).mp4` in
    the browser's Downloads folder with no way to tell which is which — and
    two different projects collide on the same name entirely.
    """
    label = {"reel": "Reel", "youtube": "Youtube", "export": "Export"}.get(kind, kind.title())
    return safe_filename(f"{project_name}-{label}-{index}.{ext.lstrip('.')}")


# ---------------------------------------------------------------------------
# Presigning
# ---------------------------------------------------------------------------


def presign_put(key: str, content_type: str = "application/octet-stream", ttl_s: int | None = None) -> str:
    settings = get_settings()
    return _client_or_raise().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.r2_bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=ttl_s or settings.r2_presign_ttl_s,
    )


def presign_get(key: str, filename: str | None = None, ttl_s: int | None = None) -> str:
    """A signed read URL.

    Pass `filename` ONLY for a download link. `attachment` makes a browser
    save the object instead of playing it, so an object that is both played
    in a `<video>` and offered as a download needs two separate URLs — never
    one with a filename attached.
    """
    settings = get_settings()
    params: dict = {"Bucket": settings.r2_bucket, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{safe_filename(filename)}"'
    return _client_or_raise().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=ttl_s or settings.r2_presign_ttl_s
    )


# ---------------------------------------------------------------------------
# Server-side transfer (worker only — the API never moves bytes)
# ---------------------------------------------------------------------------


def upload_file(local_path: Path, key: str, content_type: str | None = None) -> int:
    settings = get_settings()
    extra = {"ContentType": content_type} if content_type else None
    _client_or_raise().upload_file(str(local_path), settings.r2_bucket, key, ExtraArgs=extra)
    return local_path.stat().st_size


def download_file(key: str, local_path: Path) -> Path:
    settings = get_settings()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    _client_or_raise().download_file(settings.r2_bucket, key, str(local_path))
    return local_path


def head(key: str) -> dict | None:
    settings = get_settings()
    try:
        return _client_or_raise().head_object(Bucket=settings.r2_bucket, Key=key)
    except Exception:  # noqa: BLE001 - a missing object is a normal answer here
        return None


def exists(key: str) -> bool:
    return head(key) is not None


def object_size(key: str) -> int | None:
    """The stored object's length in bytes, or None when it is not there."""
    meta = head(key)
    if meta is None:
        return None
    size = meta.get("ContentLength")
    return int(size) if isinstance(size, int) else None


def delete(key: str) -> None:
    settings = get_settings()
    _client_or_raise().delete_object(Bucket=settings.r2_bucket, Key=key)


def delete_prefix(prefix: str) -> int:
    """Delete everything under `prefix`. Refuses prefixes outside
    MANAGED_PREFIXES so a bad caller can never sweep the whole bucket."""
    if not any(prefix.startswith(p) for p in MANAGED_PREFIXES):
        raise R2Error(f"Refusing to delete unmanaged prefix: {prefix!r}")
    settings = get_settings()
    client = _client_or_raise()
    deleted = 0
    token: str | None = None
    while True:
        kwargs = {"Bucket": settings.r2_bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=settings.r2_bucket, Delete={"Objects": keys})
            deleted += len(keys)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return deleted


def abort_stale_uploads() -> int:
    """Abort multipart uploads older than R2_MPU_ABORT_H.

    Anything over ~8MB is uploaded in parts. If the process dies mid-upload
    — OOM, a redeploy, a lost connection — the parts stay, are BILLED, and
    appear in no ordinary object listing because no object was ever
    completed. They can only be found by age: leftovers can originate under
    any prefix, so filtering by prefix would miss most of them.
    """
    settings = get_settings()
    client = _client_or_raise()
    cutoff = datetime.now(UTC) - timedelta(hours=settings.r2_mpu_abort_h)
    aborted = 0
    marker: dict = {}
    seen: set[tuple[str, str]] = set()
    for _ in range(MAX_MPU_PAGES):
        page = client.list_multipart_uploads(Bucket=settings.r2_bucket, **marker)
        for upload in page.get("Uploads", []):
            if upload["Initiated"] < cutoff:
                client.abort_multipart_upload(
                    Bucket=settings.r2_bucket, Key=upload["Key"], UploadId=upload["UploadId"]
                )
                aborted += 1
        if not page.get("IsTruncated"):
            break
        marker = {
            "KeyMarker": page.get("NextKeyMarker", ""),
            "UploadIdMarker": page.get("NextUploadIdMarker", ""),
        }
        # A truncated page whose marker does not move means asking again
        # returns the same page: trusting IsTruncated alone requests it
        # forever. This runs at worker startup, so that loop is a queue that
        # never starts. Neither guard should ever fire; both cost one line
        # and are silent disasters without it.
        cursor = (marker["KeyMarker"], marker["UploadIdMarker"])
        if cursor in seen:
            logger.warning("R2 multipart listing stopped advancing at %r; giving up", cursor)
            break
        seen.add(cursor)
    else:
        logger.warning("R2 multipart listing exceeded %d pages; giving up", MAX_MPU_PAGES)
    if aborted:
        logger.info("Aborted %d stale multipart upload(s) older than %dh", aborted, settings.r2_mpu_abort_h)
    return aborted


# ---------------------------------------------------------------------------
# Browser multipart upload (audit UP-1)
#
# One presigned PUT cannot carry more than ~5 GiB — R2, like S3, refuses a
# larger single object PUT — while the upload limit is 20 GiB, so a 6 GB
# video failed after uploading for an hour; and a dropped connection started
# a 4 GB upload over from the first byte. Multipart fixes both: the browser
# PUTs fixed-size parts straight to R2 on per-part presigned URLs (the bytes
# still never touch the API), a failed part is retried alone, and the parts
# already stored survive a page reload.
#
# The API never trusts the browser's account of the parts. It completes the
# upload from R2's OWN listing, after checking every part is there and the
# size the plan says — so the browser needs no ETag (no bucket CORS change)
# and cannot stitch together an object other than the one declared.
# ---------------------------------------------------------------------------

MIB = 1024**2
#: Below this a single PUT is simpler and loses little on a retry.
MULTIPART_MIN_BYTES = 64 * MIB
#: R2: every part but the last at least 5 MiB, all of them the SAME size,
#: at most 10,000 of them. 16 MiB keeps a retried part cheap and a 20 GiB
#: file at 1,280 parts.
PART_MIN_BYTES = 16 * MIB
MAX_PARTS = 10_000
#: list_parts returns at most 1,000 a page; 10,000 parts is ten pages, and
#: the walk stops there whatever IsTruncated says (see abort_stale_uploads).
MAX_PART_PAGES = MAX_PARTS // 1000 + 1


class NoSuchUpload(R2Error):
    """The multipart upload is gone: completed, aborted, or expired."""


def part_plan(size: int) -> tuple[int, int]:
    """(part_size, part_count) for a file of `size` bytes.

    The same function answers for the start, the signing and the completion,
    so all three agree on what "part 7" is without storing anything.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    per_part = -(-size // MAX_PARTS)  # ceil
    part_size = max(PART_MIN_BYTES, -(-per_part // MIB) * MIB)
    return part_size, -(-size // part_size)


def expected_part_size(size: int, part_number: int) -> int:
    part_size, count = part_plan(size)
    return part_size if part_number < count else size - part_size * (count - 1)


def _is_no_such_upload(err: Exception) -> bool:
    code = getattr(err, "response", {}).get("Error", {}).get("Code")
    return code in ("NoSuchUpload", "404")


def create_multipart(key: str, content_type: str) -> str:
    settings = get_settings()
    reply = _client_or_raise().create_multipart_upload(
        Bucket=settings.r2_bucket, Key=key, ContentType=content_type
    )
    return str(reply["UploadId"])


def presign_upload_part(key: str, upload_id: str, part_number: int, ttl_s: int | None = None) -> str:
    settings = get_settings()
    return _client_or_raise().generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": settings.r2_bucket,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=ttl_s or settings.r2_presign_ttl_s,
    )


def list_parts(key: str, upload_id: str) -> list[dict]:
    """[{part_number, size, etag}] of the parts R2 holds, in order."""
    settings = get_settings()
    client = _client_or_raise()
    parts: list[dict] = []
    marker = 0
    for _ in range(MAX_PART_PAGES):
        kwargs: dict = {"Bucket": settings.r2_bucket, "Key": key, "UploadId": upload_id, "MaxParts": 1000}
        if marker:
            kwargs["PartNumberMarker"] = marker
        try:
            page = client.list_parts(**kwargs)
        except Exception as e:  # noqa: BLE001 - re-raised, typed when it means "gone"
            if _is_no_such_upload(e):
                raise NoSuchUpload(upload_id) from e
            raise
        for part in page.get("Parts", []):
            parts.append(
                {
                    "part_number": int(part["PartNumber"]),
                    "size": int(part["Size"]),
                    "etag": str(part["ETag"]),
                }
            )
        nxt = int(page.get("NextPartNumberMarker") or 0)
        if not page.get("IsTruncated") or nxt <= marker:
            break
        marker = nxt
    return sorted(parts, key=lambda p: p["part_number"])


def complete_multipart(key: str, upload_id: str, parts: list[dict]) -> None:
    settings = get_settings()
    try:
        _client_or_raise().complete_multipart_upload(
            Bucket=settings.r2_bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in parts]
            },
        )
    except Exception as e:  # noqa: BLE001 - re-raised, typed when it means "gone"
        if _is_no_such_upload(e):
            raise NoSuchUpload(upload_id) from e
        raise


def abort_multipart(key: str, upload_id: str) -> bool:
    """Abort one upload. False when it was already gone, which is the state
    the caller wanted anyway."""
    settings = get_settings()
    try:
        _client_or_raise().abort_multipart_upload(Bucket=settings.r2_bucket, Key=key, UploadId=upload_id)
    except Exception as e:  # noqa: BLE001 - re-raised unless it means "gone"
        if _is_no_such_upload(e):
            return False
        raise
    return True


def abort_uploads_under(prefix: str) -> int:
    """Abort every unfinished multipart upload under `prefix`.

    A deleted project, or one whose upload is started again, must not leave
    parts behind: they are billed, and no object listing shows them.
    """
    if not any(prefix.startswith(p) for p in MANAGED_PREFIXES):
        raise R2Error(f"Refusing to abort uploads outside a managed prefix: {prefix!r}")
    settings = get_settings()
    client = _client_or_raise()
    aborted = 0
    marker: dict = {}
    seen: set[tuple[str, str]] = set()
    for _ in range(MAX_MPU_PAGES):
        page = client.list_multipart_uploads(Bucket=settings.r2_bucket, Prefix=prefix, **marker)
        for upload in page.get("Uploads", []):
            if abort_multipart(upload["Key"], upload["UploadId"]):
                aborted += 1
        if not page.get("IsTruncated"):
            break
        marker = {
            "KeyMarker": page.get("NextKeyMarker", ""),
            "UploadIdMarker": page.get("NextUploadIdMarker", ""),
        }
        cursor = (marker["KeyMarker"], marker["UploadIdMarker"])
        if cursor in seen:
            break
        seen.add(cursor)
    return aborted


def content_type_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".srt": "text/plain; charset=utf-8",
        ".json": "application/json",
    }.get(ext, "application/octet-stream")


def now() -> float:
    return time.time()
