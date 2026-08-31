"""User administration. Admin only."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import r2
from app.db import get_db
from app.dbmodels import Job, Project, User
from app.schemas import BrandSaveIn, BrandUploadIn, CreateUserIn, Password, ProviderSettingsIn
from app.security import Principal, hash_password, require_admin, stamp_password_change

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/users")
def list_users(db: Session = Depends(get_db)) -> list[dict]:  # noqa: B008
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    counts = dict(
        db.execute(select(Project.owner_id, func.count()).group_by(Project.owner_id)).all()  # type: ignore[arg-type]
    )
    return [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "active": u.active,
            "created_at": u.created_at,
            "projects": counts.get(u.id, 0),
        }
        for u in rows
    ]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: CreateUserIn, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail=f"User {body.username!r} already exists")
    salt, digest = hash_password(body.password)
    user = User(username=body.username, pw_salt=salt, pw_hash=digest, role=body.role)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    new_password: Password,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Хэрэглэгч олдсонгүй.")
    salt, digest = hash_password(new_password)
    user.pw_salt, user.pw_hash = salt, digest
    # Same rule as a self-service change: an admin reset must end the
    # sessions it was performed to end.
    stamp_password_change(user)
    db.commit()
    return {"updated": True}


@router.post("/users/{user_id}/active")
def set_active(
    user_id: str,
    active: bool,
    principal: Principal = Depends(require_admin),  # noqa: B008
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Хэрэглэгч олдсонгүй.")
    # Locking yourself out leaves nobody able to unlock anyone.
    if user.id == principal.id and not active:
        raise HTTPException(status_code=400, detail="Өөрийн бүртгэлээ хаах боломжгүй.")
    user.active = active
    db.commit()
    return {"active": user.active}


@router.get("/providers")
async def provider_status(db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Whether the paid providers are usable RIGHT NOW.

    `insufficient_credits` is a documented duudlaga.dev failure, and it
    surfaces at the worst possible moment: after a transcribe job has already
    claimed a worker slot and downloaded the source video. This answers the
    question before any of that is spent.

    Never raises. A provider being unreachable is itself the answer, and a
    diagnostics page that 500s tells the operator nothing.
    """
    from app import provider_settings, providers
    from app.stt import factory

    # Stored overrides included: a page that reports the environment while
    # the jobs use something else answers the wrong question.
    settings = provider_settings.effective(db)

    # The SELECTED recogniser, not duudlaga by name: probing the one that is
    # not running answers a question nobody asked and leaves the one that is
    # running unchecked.
    stt: dict = {
        "provider": settings.stt_provider,
        "configured": bool(factory.api_key_for(settings)),
    }
    if stt["configured"]:
        try:
            client = factory.build_client(settings)
        except factory.UnknownSttProvider as e:
            stt["ok"] = False
            stt["error"] = str(e)
            client = None
        if client is not None:
            try:
                stt["account"] = await client.account_info()
                stt["ok"] = True
            except Exception as e:  # noqa: BLE001 - reachability is part of the answer
                stt["ok"] = False
                stt["error"] = str(e) or f"{type(e).__name__}"
            finally:
                await client.aclose()

    return {
        # What serves what, and what an operator can do about each — the keys
        # sat in one list with no indication of which feature they powered,
        # and one of them powers nothing at all yet.
        "capabilities": [c.to_dict() for c in providers.describe(settings)],
        "stt": stt,
        "stt_providers": list(factory.PROVIDERS),
        # The key itself is never echoed — only whether one is present. The
        # model is not a secret and is the thing most likely to be wrong.
        "openrouter": {
            "configured": bool(settings.openrouter_api_key),
            "model": settings.openrouter_model,
        },
        # `error` names the variable that is wrong, never its value — this
        # page is the first place an operator looks, and a diagnostics page
        # that prints a credential is a diagnostics page that leaks one.
        "storage": {
            "configured": settings.r2_enabled,
            "bucket": settings.r2_bucket,
            "error": settings.r2_config_error,
        },
    }


@router.get("/settings")
def provider_settings_read(db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Where each provider value comes from, and a hint at its contents.

    Never the value itself — the response is the same whether or not the
    caller already knows the key.
    """
    from app import provider_settings

    return provider_settings.describe(db)


@router.put("/settings")
def provider_settings_write(
    body: ProviderSettingsIn,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Store the fields that were sent. Takes effect on the next job.

    The worker reads these per job from the same table, so no restart and no
    redeploy is needed — and nothing has to be pushed to the worker, which
    could not be reached from here anyway.
    """
    from app import provider_settings

    try:
        changed = provider_settings.apply(db, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Names only. The audit middleware records the request without its body,
    # and this response is the one other place a value could escape.
    return {"changed": changed, "settings": provider_settings.describe(db)}


@router.get("/brand")
def brand_read(db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Every brand asset, with a short-lived presigned GET for each.

    The URL lets the admin page show what is actually stored rather than what
    was last uploaded from this browser.
    """
    from app import brand

    assets: dict[str, dict | None] = {}
    for name in brand.ASSETS:
        key = brand.get(db, name)
        url = None
        if key and r2.enabled():
            try:
                url = r2.presign_get(key)
            except Exception:  # noqa: BLE001 - a missing preview must not 500 the page
                url = None
        assets[name] = {"key": key, "url": url} if key else None
    return {**assets, "storage": r2.enabled()}


@router.post("/brand/{asset}/upload-url")
def brand_upload_url(asset: str, body: BrandUploadIn) -> dict:
    """A presigned PUT. The file never passes through here.

    Same rule as every other media file in this system (app.r2): the browser
    uploads straight to R2 and the API only ever learns the key. The content
    type is checked now rather than at render time, where a format ffmpeg
    cannot read would fail an export these assets are only decorating.
    """
    from app import brand

    if not r2.enabled():
        raise HTTPException(
            status_code=503, detail="Энэ сервер дээр хадгалах сан (R2) тохируулагдаагүй байна."
        )
    try:
        key = brand.new_key(asset, body.content_type)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"key": key, "url": r2.presign_put(key, body.content_type)}


@router.put("/brand/{asset}")
def brand_save(asset: str, body: BrandSaveIn, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Adopt an uploaded object, or clear the slot with a null key.

    The key is checked to live under `brand/` so this route cannot adopt
    someone else's source video, and to exist, because a client that
    presigned a URL and then failed to PUT would otherwise leave every export
    hunting a file that was never stored.
    """
    from app import brand

    if asset not in brand.ASSETS:
        raise HTTPException(status_code=404, detail=f"Тодорхойгүй брэнд материал: {asset}")
    if body.key is not None:
        if not body.key.startswith(f"brand/{asset}-"):
            raise HTTPException(status_code=400, detail=f"Энэ түлхүүр {asset}-ийнх биш байна.")
        if r2.enabled() and not r2.exists(body.key):
            raise HTTPException(status_code=400, detail="Тэр хуулалт бүрэн дуусаагүй байна.")
    brand.set_asset(db, asset, body.key)
    db.commit()
    return brand_read(db)


@router.get("/jobs")
def recent_jobs(limit: int = 100, db: Session = Depends(get_db)) -> list[dict]:  # noqa: B008
    from app.jobs import queue

    rows = db.scalars(select(Job).order_by(Job.created_at.desc()).limit(min(limit, 500))).all()
    return [queue.to_dict(j) for j in rows]


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Return a failed job to the queue by hand.

    Automatic recovery runs inside the worker's own loop, so when a worker
    dies the recovery dies with it. This is the escape hatch that does not
    depend on the thing that broke.
    """
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ажил олдсонгүй.")
    if job.state in ("queued", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job.state}")
    job.state = "queued"
    job.attempts = 0
    job.error = None
    job.progress = 0.0
    job.stage = ""
    job.message = "Requeued by an administrator"
    job.claimed_by = None
    job.heartbeat_at = None
    job.finished_at = None
    db.commit()
    return {"requeued": True}
