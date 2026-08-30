"""Brand assets an admin uploads once: the logo, and later the intro/outro.

Global, not per project. A studio has ONE mark, and re-uploading it per
project is how two versions of a logo end up in circulation with nobody able
to say which is current. The per-project decision — whether to use it, which
corner, how big — is `models.LogoSettings`, because that is the part that
actually differs between exports.

The image itself never passes through the API. It goes to R2 by presigned PUT
like every other media file here (see app.r2), and only the KEY is stored.

Keys carry a timestamp rather than being a fixed name. A stable key would be
simpler, but the admin page previews the logo through a presigned GET, and a
replaced logo behind an unchanged URL is served from cache — the operator
uploads a new mark, sees the old one, and uploads again.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import r2
from app.dbmodels import Setting
from app.utils.logging import get_logger

logger = get_logger(__name__)

LOGO_KEY = "brand_logo_key"

# What a logo may be. SVG is absent on purpose: ffmpeg cannot rasterise it
# without librsvg, which this image does not carry, so accepting one would
# fail at render time rather than at upload.
LOGO_CONTENT_TYPES = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
}


def new_logo_key(content_type: str) -> str:
    ext = LOGO_CONTENT_TYPES.get(content_type)
    if ext is None:
        raise ValueError(f"Unsupported logo type {content_type!r}")
    return f"brand/logo-{int(time.time())}{ext}"


def get(db: Session, key: str = LOGO_KEY) -> str | None:
    row = db.scalars(select(Setting).where(Setting.key == key)).first()
    return row.value if row and row.value else None


def set_logo(db: Session, r2_key: str | None) -> None:
    """Points the brand logo at a new object and removes the one it replaces.

    Deleting the old object is best-effort: an orphan in R2 costs a few
    kilobytes, while a failed delete that propagated would lose the operator
    the upload they just made.
    """
    previous = get(db)
    row = db.scalars(select(Setting).where(Setting.key == LOGO_KEY)).first()
    if row is None:
        row = Setting(key=LOGO_KEY, value=r2_key or "")
        db.add(row)
    else:
        row.value = r2_key or ""

    if previous and previous != r2_key and r2.enabled():
        try:
            r2.delete(previous)
        except Exception:  # noqa: BLE001 - an orphan object is not worth failing on
            logger.warning("Could not delete the replaced logo %s", previous, exc_info=True)
