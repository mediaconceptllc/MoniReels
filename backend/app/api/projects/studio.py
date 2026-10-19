"""The studio's shared pieces the project page reads: subtitle templates and
fonts, what the providers can run, the voices a speaker can be given.

Under /projects but about no one project, so every path here has two
segments: a single-segment one would be matched by /projects/{project_id}
first (see `subtitle_fonts`).
"""
from __future__ import annotations

import time
import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import security
from app.api.projects.common import projects_router
from app.db import get_db
from app.dbmodels import SubtitleTemplate
from app.schemas import SubtitleTemplateIn
from app.security import Principal, current_user

router = projects_router()


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
    from app import brand, provider_settings, providers

    settings = provider_settings.effective(db)
    return {
        "capabilities": [
            {"name": c.name, "label": c.label, "ready": c.ready, "blocked": c.blocked}
            for c in providers.describe(settings)
        ],
        # Which brand files exist. The export panel offered the logo, the
        # intro and the outro whether or not anything had been uploaded, and
        # the worker skipped a missing one with a log line nobody reads — so
        # "Лого тавих" could be ticked, saved, exported and silently absent.
        # Whether, not what: the keys and URLs stay on the admin page.
        "brand": {asset: bool(brand.get(db, asset)) for asset in brand.ASSETS},
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
    found = await elevenlabs.read_voices(settings)
    return {
        "voices": found["voices"],
        "default_voice_id": settings.elevenlabs_tts_voice_id or None,
        "error": found["error"],
    }
