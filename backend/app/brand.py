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
INTRO_KEY = "brand_intro_key"
OUTRO_KEY = "brand_outro_key"

# The three assets an admin uploads, by the name the API uses for each.
ASSETS = {"logo": LOGO_KEY, "intro": INTRO_KEY, "outro": OUTRO_KEY}

# What a logo may be. SVG is absent on purpose: ffmpeg cannot rasterise it
# without librsvg, which this image does not carry, so accepting one would
# fail at render time rather than at upload.
LOGO_CONTENT_TYPES = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
}


# What an intro or outro may be. Whatever it is, it gets normalised to the
# export's own resolution and frame rate before the join, so only the
# container has to be one ffmpeg reads without a hunt.
CLIP_CONTENT_TYPES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def new_key(asset: str, content_type: str) -> str:
    if asset not in ASSETS:
        raise ValueError(f"Unknown brand asset {asset!r}")
    allowed = LOGO_CONTENT_TYPES if asset == "logo" else CLIP_CONTENT_TYPES
    ext = allowed.get(content_type)
    if ext is None:
        raise ValueError(f"Unsupported {asset} type {content_type!r}")
    # Timestamped, not a fixed name: the admin page previews these through a
    # signed GET, and a replacement behind an unchanged URL is served from
    # cache — the operator uploads a new file, sees the old one, uploads again.
    return f"brand/{asset}-{int(time.time())}{ext}"


def get(db: Session, asset: str = "logo") -> str | None:
    row = db.scalars(select(Setting).where(Setting.key == ASSETS[asset])).first()
    return row.value if row and row.value else None


def set_asset(db: Session, asset: str, r2_key: str | None) -> None:
    """Points a brand asset at a new object and removes the one it replaces.

    Deleting the old object is best-effort: an orphan in R2 costs a few
    kilobytes, while a failed delete that propagated would cost the operator
    the upload they just made.
    """
    setting_key = ASSETS[asset]
    previous = get(db, asset)
    row = db.scalars(select(Setting).where(Setting.key == setting_key)).first()
    if row is None:
        db.add(Setting(key=setting_key, value=r2_key or ""))
    else:
        row.value = r2_key or ""

    if previous and previous != r2_key and r2.enabled():
        try:
            r2.delete(previous)
        except Exception:  # noqa: BLE001 - an orphan object is not worth failing on
            logger.warning("Could not delete the replaced %s %s", asset, previous, exc_info=True)
