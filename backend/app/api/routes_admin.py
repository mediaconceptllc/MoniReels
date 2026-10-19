"""User administration. Admin only."""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import Float, case, cast, func, select
from sqlalchemy.orm import Session

from app import r2
from app.db import get_db
from app.dbmodels import Job, Project, User
from app.schemas import BrandSaveIn, BrandUploadIn, CreateUserIn, ProviderSettingsIn, ResetPasswordIn
from app.security import (
    Principal,
    client_ip,
    hash_password,
    require_admin,
    stamp_password_change,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/users")
def list_users(db: Session = Depends(get_db)) -> list[dict]:  # noqa: B008
    rows = db.scalars(select(User).order_by(User.created_at)).all()
    counts: dict[str, int] = {
        owner: n for owner, n in db.execute(select(Project.owner_id, func.count()).group_by(Project.owner_id))
    }
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
        # Said on the form it was typed into, so in the language of the form.
        raise HTTPException(status_code=409, detail=f"«{body.username}» нэртэй хэрэглэгч аль хэдийн бий.")
    salt, digest = hash_password(body.password)
    user = User(username=body.username, pw_salt=salt, pw_hash=digest, role=body.role)
    db.add(user)
    db.commit()
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/users/{user_id}/password")
def reset_password(
    user_id: str,
    body: ResetPasswordIn,
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Хэрэглэгч олдсонгүй.")
    salt, digest = hash_password(body.new_password)
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
    # The rule the line above serves, stated on its own. Today it cannot fire
    # over HTTP — the caller is an active admin and may not close their own
    # account — but it is the invariant, and it must survive the day a second
    # path changes a role or an account is closed some other way.
    if not active and user.role == "admin" and user.active and not _other_active_admins(db, user.id):
        raise HTTPException(status_code=400, detail="Сүүлчийн идэвхтэй админыг хаах боломжгүй.")
    user.active = active
    db.commit()
    return {"active": user.active}


def _other_active_admins(db: Session, user_id: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(User)
        .where(User.role == "admin", User.active.is_(True), User.id != user_id)
    ) or 0


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
    from app import provider_settings, providers, secretbox, worker_report
    from app.config import get_settings
    from app.stt import factory

    # Stored overrides included: a page that reports the environment while
    # the jobs use something else answers the wrong question. A stored key
    # that cannot be opened is reported below (`secret_storage`) rather than
    # failing the page the operator fixes it from.
    try:
        settings = provider_settings.effective(db)
    except secretbox.SecretKeyError:
        settings = get_settings()

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
                # Summarised, never echoed: duudlaga's /me answers with the
                # key itself beside the balance ({"key": "dk_live_…"}).
                stt["balance"] = balance_summary(await client.account_info())
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
        # The dub's separation last: the worker, not a key, decides it, and
        # the worker says what it can do (app.worker_report).
        "capabilities": [
            c.to_dict()
            for c in [*providers.describe(settings), providers.separation(worker_report.read(db))]
        ],
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
        # Whether the keys stored from this page are encrypted at rest, and
        # which variable would make them so. Names only.
        "secret_storage": provider_settings.storage_status(db),
    }


# What a balance is called. An ALLOW-list, not a deny-list: neither provider
# documents the whole answer, and a field nobody wrote down — an email, a key
# prefix, a second key — must stay off the page by default rather than leak
# until someone notices it.
_BALANCE_NAME = re.compile(
    r"balance|credit|remain|limit|used|usage|count|spend|quota|tier|plan|reset|currency", re.I
)
_PRIVATE_NAME = re.compile(r"key|token|secret|password|email|name|(^|_)id$", re.I)
BALANCE_MAX_FIELDS = 12
BALANCE_MAX_TEXT = 40


def balance_summary(account: object) -> list[dict]:
    """The provider's balance and limits as `{name, value}` rows.

    Fetched on every visit to the settings page and then thrown away — the
    one number that answers "will the next transcription run" never reached
    the screen. Scalars only, one level of nesting (`limits.daily`), capped,
    and only under a name that says it is an amount.
    """
    if not isinstance(account, dict):
        return []
    rows: list[dict] = []

    def visit(prefix: str, value: object, depth: int) -> None:
        if len(rows) >= BALANCE_MAX_FIELDS:
            return
        if isinstance(value, dict):
            if depth == 0:
                for name, inner in value.items():
                    visit(f"{prefix}.{name}", inner, depth + 1)
            return
        # The name is checked whole, so a private word anywhere in the path
        # (`key.limit`) keeps the row off the page.
        if not _BALANCE_NAME.search(prefix) or any(_PRIVATE_NAME.search(p) for p in prefix.split(".")):
            return
        if isinstance(value, bool) or isinstance(value, int | float):
            rows.append({"name": prefix, "value": value})
        elif isinstance(value, str) and value and len(value) <= BALANCE_MAX_TEXT:
            rows.append({"name": prefix, "value": value})

    for name, value in account.items():
        visit(str(name), value, 0)
    return rows


@router.get("/tts/voices")
async def tts_voices(db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """The voices the voice-over can use, and whether ElevenLabs lists
    Mongolian for the chosen model.

    Free — nothing is synthesised — and never raises: a provider that cannot
    be reached is itself the answer, as on the providers page.

    `mongolian` is ASKED, not assumed. It is True or False when ElevenLabs'
    model list says so, and None when that list could not be read or does
    not mention the model: unknown is not the same as no, and a picker that
    printed "not supported" for a model it could not ask about would be
    guessing in the operator's direction.
    """
    from app import provider_settings
    from app.tts import elevenlabs

    settings = provider_settings.effective(db)
    out: dict = {
        "model": settings.elevenlabs_tts_model,
        "voice_id": settings.elevenlabs_tts_voice_id or None,
        "voices": [],
        "mongolian": None,
        "error": None,
    }
    if not settings.elevenlabs_api_key:
        out["error"] = "ElevenLabs-ийн API түлхүүр тавигдаагүй байна."
        return out

    client = elevenlabs.build_client(settings)
    try:
        out["voices"] = await client.voices()
        try:
            languages = await client.model_languages(settings.elevenlabs_tts_model)
            out["mongolian"] = elevenlabs.is_mongolian(languages)
        except Exception as e:  # noqa: BLE001 - the voices are still worth showing
            logger.warning("Could not read ElevenLabs' model list: %s", e)
    except Exception as e:  # noqa: BLE001 - reachability is part of the answer
        out["error"] = str(e) or type(e).__name__
    finally:
        await client.aclose()
    return out


@router.get("/settings")
def provider_settings_read(db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Where each provider value comes from, and a hint at its contents.

    Never the value itself — the response is the same whether or not the
    caller already knows the key.
    """
    from app import provider_settings

    return provider_settings.describe(db)


@router.put("/settings")
async def provider_settings_write(
    body: ProviderSettingsIn,
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
    principal: Principal = Depends(require_admin),  # noqa: B008
) -> dict:
    """Store the fields that were sent. Takes effect on the next job.

    The worker reads these per job from the same table, so no restart and no
    redeploy is needed — and nothing has to be pushed to the worker, which
    could not be reached from here anyway.

    A new model name is checked against OpenRouter's catalogue BEFORE it is
    stored. `stt_provider` beside it has always been a closed set for exactly
    this reason; the model was free text, so a typo saved cleanly and surfaced
    as a 400 on the next paid job instead of under the box it was typed in.
    """
    from app import provider_settings

    fields = body.model_dump(exclude_unset=True)
    model_check = await _check_new_model(db, fields)

    try:
        changed = provider_settings.apply(
            db, fields, actor=principal.username, ip=client_ip(request)
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Names only. The audit middleware records the request without its body,
    # and this response is the one other place a value could escape.
    return {
        "changed": changed,
        "settings": provider_settings.describe(db),
        "model_check": model_check,
    }


async def _check_new_model(db: Session, fields: dict) -> dict | None:
    """Refuse a model OpenRouter does not serve; never refuse on an outage.

    Returns None when no model was sent, so "not checked" and "checked and
    fine" stay apart in the response.
    """
    from app import provider_settings
    from app.ai import openrouter_client

    model = (fields.get("openrouter_model") or "").strip()
    if not model:
        # Empty clears the override and falls back to the environment, which
        # this route has no business vetoing.
        return None

    settings = provider_settings.effective(db)
    # A key sent in the SAME request is the one to authenticate with — an
    # operator entering a key and a model together must not be told the
    # catalogue is unreachable because the old key was empty.
    key = (fields.get("openrouter_api_key") or "").strip() or settings.openrouter_api_key
    if not key:
        # With no key the app cannot call OpenRouter for a job either, so
        # there is nothing this check could protect — and asking anyway buys
        # a 401 after a timeout the operator waits through.
        return {"verified": False, "reason": "OpenRouter key is not configured"}

    config = openrouter_client.OpenRouterConfig(
        api_key=key, model=model, base_url=settings.openrouter_base_url
    )

    try:
        check = await openrouter_client.check_model(config, model)
    except openrouter_client.ModelCatalogUnavailable as e:
        # Saving anyway is the lesser harm: refusing would make an OpenRouter
        # outage a lockout. Naming it keeps that from being silent.
        logger.warning("Could not verify model %r: %s", model, e)
        return {"verified": False, "reason": str(e)}

    if not check.known:
        detail = f"OpenRouter «{model}» нэртэй загвар үйлчилдэггүй."
        if check.suggestions:
            detail += " Ойролцоо нэрс: " + ", ".join(check.suggestions) + "."
        raise HTTPException(status_code=400, detail=detail)
    return {"verified": True}


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


JOB_STATES = ("queued", "running", "done", "failed", "canceled")
# Only work that stopped short. A finished job re-run by hand is a second
# render or, for the metered kinds, a second bill for a result that exists.
RETRYABLE = ("failed", "canceled")
JOBS_PAGE_MAX = 200


@router.get("/jobs")
def list_jobs(
    state: str | None = None,
    kind: str | None = None,
    limit: int = Query(50, ge=1, le=JOBS_PAGE_MAX),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Every account's jobs, newest first, a page at a time.

    `total` is the count under the same filters, so the page can say how many
    there are rather than implying that one page is all of them. The project
    and its owner are named: a job id tells an operator nothing.
    """
    from app.jobs import queue
    from app.jobs.kinds import KINDS

    if state is not None and state not in JOB_STATES:
        raise HTTPException(status_code=422, detail=f"Тодорхойгүй төлөв: {state}")
    if kind is not None and kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"Тодорхойгүй ажлын төрөл: {kind}")
    filters = []
    if state is not None:
        filters.append(Job.state == state)
    if kind is not None:
        filters.append(Job.kind == kind)

    total = db.scalar(select(func.count()).select_from(Job).where(*filters)) or 0
    rows = db.execute(
        select(Job, Project.name, User.username)
        .outerjoin(Project, Project.id == Job.project_id)
        .outerjoin(User, User.id == Project.owner_id)
        .where(*filters)
        # The id breaks ties so that paging cannot show a row twice or skip
        # one when two jobs were queued in the same instant.
        .order_by(Job.created_at.desc(), Job.id)
        .limit(limit)
        .offset(offset)
    ).all()
    jobs = []
    for job, project_name, owner in rows:
        payload = queue.payload_of(job)
        jobs.append({
            **queue.to_dict(job),
            "project_name": project_name,
            "owner": owner,
            "lane": job.lane,
            "no_retry": job.no_retry,
            "claimed_by": job.claimed_by,
            "requeued_at": _number(payload.get("requeued_at")),
            "retryable": job.state in RETRYABLE,
            # Every attempt that ended, oldest first (app.jobs.queue): `error`
            # above is only the latest, and an earlier failure is often the
            # one that explains it.
            "started_at": job.started_at,
            "available_at": job.available_at,
            "history": job.history or [],
        })
    return {"jobs": jobs, "total": total, "limit": limit, "offset": offset}


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _json_number(element):
    """A JSONB number as a float, or NULL for anything else — a stray string
    must cost one sample, not 500 the whole page on a failed cast."""
    return case((func.jsonb_typeof(element) == "number", cast(element.astext, Float)))


@router.get("/jobs/stats")
def job_stats(
    days: int = Query(7, ge=1, le=365),
    db: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """How the queue has been doing: success rate, run time, time waiting.

    Measured from the rows themselves, so the window cannot be longer than
    the rows are kept (`job_keep_days`); the answer says which window it
    actually used.

    - Success is done / (done + failed). A cancel is a person's decision,
      not a failure, so it is counted beside the rate and never inside it.
    - Run time is the successful attempt's own `elapsed_sec`, as the worker
      measured it around the handler.
    - Waiting is from the request — or an admin's requeue — to the start of
      the run that finished it. Only jobs that succeeded on their FIRST
      attempt count: for the rest, the gap also holds an earlier failed run
      and the stale-heartbeat timeout, which is not time spent in a queue.
    """
    from app.config import get_settings
    from app.jobs import queue
    from app.jobs.kinds import KINDS

    keep_days = get_settings().job_keep_days
    window = min(days, keep_days)
    now = time.time()
    since = now - window * 86400

    elapsed = _json_number(Job.result["output"]["elapsed_sec"])
    queued_at = func.greatest(Job.created_at, _json_number(Job.result["payload"]["requeued_at"]))
    wait = func.greatest(Job.finished_at - queued_at - elapsed, 0.0)
    is_done = Job.state == "done"
    first_try = is_done & (Job.attempts == 1) & elapsed.is_not(None)

    def p(fraction: float, when, value):
        # percentile_cont ignores NULLs, so a CASE without ELSE is a filter.
        return func.percentile_cont(fraction).within_group(case((when, value)))

    columns = [
        func.count().filter(is_done).label("done"),
        func.count().filter(Job.state == "failed").label("failed"),
        func.count().filter(Job.state == "canceled").label("canceled"),
        p(0.5, is_done, elapsed).label("run_p50"),
        p(0.95, is_done, elapsed).label("run_p95"),
        p(0.5, first_try, wait).label("wait_p50"),
        p(0.95, first_try, wait).label("wait_p95"),
        func.count().filter(first_try).label("wait_samples"),
    ]
    settled = (Job.state.in_(("done", "failed", "canceled")), Job.finished_at >= since)

    overall = db.execute(select(*columns).where(*settled)).one()
    per_kind = db.execute(select(Job.kind, *columns).where(*settled).group_by(Job.kind)).all()
    order = {k: i for i, k in enumerate(KINDS)}
    per_kind = sorted(per_kind, key=lambda row: (order.get(row.kind, len(order)), row.kind))

    overview = queue.queue_overview(db=db)
    oldest = db.scalar(select(func.min(queued_at)).where(Job.state == "queued"))
    return {
        "days": window,
        "keep_days": keep_days,
        "overall": _stats_row(overall),
        "kinds": [{"kind": row.kind, **_stats_row(row)} for row in per_kind],
        "queue": {
            "waiting": overview["waiting"],
            "running": overview["counts"].get("running", 0),
            "live_workers": overview["live_workers"],
            "stalled": overview["stalled"],
            "oldest_waiting_sec": round(max(0.0, now - oldest), 1) if oldest is not None else None,
        },
    }


def _stats_row(row) -> dict:
    def sec(value: float | None) -> float | None:
        return round(float(value), 1) if value is not None else None

    decided = row.done + row.failed
    return {
        "done": row.done,
        "failed": row.failed,
        "canceled": row.canceled,
        # Null, not 0 or 100, when nothing finished: a rate from no samples
        # is not a rate.
        "success_pct": round(100.0 * row.done / decided, 1) if decided else None,
        "run_p50_sec": sec(row.run_p50),
        "run_p95_sec": sec(row.run_p95),
        "wait_p50_sec": sec(row.wait_p50),
        "wait_p95_sec": sec(row.wait_p95),
        "wait_samples": row.wait_samples,
    }


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, db: Session = Depends(get_db)) -> dict:  # noqa: B008
    """Return a failed or canceled job to the queue by hand.

    Automatic recovery runs inside the worker's own loop, so when a worker
    dies the recovery dies with it. This is the escape hatch that does not
    depend on the thing that broke — and the human decision the metered
    kinds (`no_retry`) were failed to wait for.
    """
    from app.jobs import queue

    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Ажил олдсонгүй.")
    if job.state not in RETRYABLE:
        raise HTTPException(
            status_code=400,
            detail="Зөвхөн амжилтгүй болсон эсвэл цуцлагдсан ажлыг дахин эхлүүлнэ.",
        )
    # A settled job gave up its dedupe key, so nothing else stops this from
    # running beside the same work started again since — two transcriptions
    # of one project, writing the same transcript. The live one wins.
    if job.project_id is not None:
        twin = db.scalar(
            select(Job.id).where(
                Job.project_id == job.project_id,
                Job.kind == job.kind,
                Job.state.in_(queue.ACTIVE_STATES),
            )
        )
        if twin is not None:
            raise HTTPException(
                status_code=409,
                detail="Энэ төслийн ижил ажил аль хэдийн дараалалд эсвэл явж байна.",
            )
    now = time.time()
    job.state = "queued"
    job.attempts = 0
    job.error = None
    job.progress = 0.0
    job.stage = ""
    job.message = "Админ дахин эхлүүлсэн"
    job.claimed_by = None
    job.heartbeat_at = None
    job.finished_at = None
    # A person asked for it now: no backoff left over from the failed run.
    # Its history stays — the attempts before this one are why it is here.
    job.available_at = None
    # A canceled job keeps its flag otherwise, and the handle of the very run
    # this starts reads it and stops on the first progress tick.
    job.cancel_requested = False
    job.updated_at = now
    # In the payload because that is the part `finish` keeps (handlers read
    # only the keys they know): the statistics measure this run's wait from
    # here, not from a request days old. The failed run's output goes — it
    # would read as this run's while the job waits.
    job.result = {"payload": {**queue.payload_of(job), "requeued_at": now}}
    db.commit()
    return {"requeued": True}
