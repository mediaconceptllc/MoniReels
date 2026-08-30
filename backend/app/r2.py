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

_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


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


def output_key(project_id: str, kind: str, index: int, ext: str) -> str:
    return f"outputs/{project_id}/{kind}_{index}{_safe_suffix('.' + ext.lstrip('.'))}"


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
    while True:
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
    if aborted:
        logger.info("Aborted %d stale multipart upload(s) older than %dh", aborted, settings.r2_mpu_abort_h)
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
