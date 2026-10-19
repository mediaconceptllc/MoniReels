"""What one account may put on the shared queue — refused at the button, not
discovered on a provider's invoice.

The audit (SEC-4) measured what stood in the way before: nothing. An ordinary
editor queued 25 exports at once and created 40 projects in 0.4 s, and a
stolen password could run an unbounded STT/LLM/TTS bill while holding the one
paid lane every other producer waits on. app.spend only REPORTS; this refuses.

Race-safe by a per-account advisory lock taken inside the request's own
transaction. Two requests that each count "2 of 3" would otherwise both
insert; with the lock the second one counts after the first one's job exists.
The lock is released by the commit that writes the job (queue.enqueue commits
the caller's session) or by the request's rollback — so it must be taken
right before that commit, after every other check has passed.

Admins are exempt: they are the people who free a stuck account's work, and a
limit on them would be a limit on the remedy.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.dbmodels import Job, Project, UsageEvent
from app.jobs import queue
from app.jobs.kinds import LANE_METERED, LANES
from app.security import Principal

# The first half of the two-key advisory lock: a namespace of its own, so a
# lock taken here can never be confused with one taken anywhere else.
LOCK_CLASS = 0x51_54  # "QT"

# Imports follow an upload and are free; the project limits bound them.
UNCAPPED_KINDS = frozenset({"import_video"})

PAID_KINDS = frozenset(kind for kind, lane in LANES.items() if lane == LANE_METERED)


def _lock(db: Session, user_id: str) -> None:
    db.execute(select(func.pg_advisory_xact_lock(LOCK_CLASS, func.hashtext(user_id))))


def _too_many(detail: str) -> HTTPException:
    return HTTPException(status_code=429, detail=detail)


def month_start(now: float) -> float:
    """Midnight UTC on the 1st — the month the providers bill by."""
    moment = datetime.fromtimestamp(now, UTC)
    return datetime(moment.year, moment.month, 1, tzinfo=UTC).timestamp()


def month_spend(db: Session, user_id: str, now: float | None = None) -> tuple[float, int]:
    """(known USD, requests with no known price) this UTC month.

    The unknown part stays a COUNT, never a zero: speech without a configured
    rate is recorded unpriced, and adding it in as $0 would let the cap read
    a month of unpriced minutes as a cheap one.
    """
    since = month_start(time.time() if now is None else now)
    known, unpriced = db.execute(
        select(
            func.coalesce(func.sum(UsageEvent.cost_usd), 0.0),
            func.coalesce(
                func.sum(case((UsageEvent.cost_usd.is_(None), UsageEvent.requests), else_=0)), 0
            ),
        ).where(UsageEvent.user_id == user_id, UsageEvent.created_at >= since)
    ).one()
    return float(known), int(unpriced)


def admit_job(
    db: Session,
    principal: Principal,
    kind: str,
    *,
    paid: bool | None = None,
    dedupe_key: str | None = None,
) -> None:
    """Refuse (429) a job this account may not queue now.

    `paid` overrides the kind's own answer for work that bills only sometimes
    — an export with a Mongolian voice-over buys speech, one without does not.

    A job that would dedupe onto one already queued adds no load, so it is
    admitted whatever the counts say: the second click on a running
    transcription must get that transcription back, not a refusal.
    """
    if principal.is_admin or kind in UNCAPPED_KINDS:
        return
    settings = get_settings()
    paid = kind in PAID_KINDS if paid is None else paid
    _lock(db, principal.id)
    if dedupe_key and queue.active_with_key(db, dedupe_key) is not None:
        return

    active, active_paid = db.execute(
        select(
            func.count(),
            func.count().filter(Job.kind.in_(PAID_KINDS)),
        )
        .select_from(Job)
        .join(Project, Project.id == Job.project_id)
        .where(
            Project.owner_id == principal.id,
            Job.state.in_(queue.ACTIVE_STATES),
            Job.kind.not_in(UNCAPPED_KINDS),
        )
    ).one()

    cap = settings.quota_active_jobs
    if cap and active >= cap:
        raise _too_many(
            f"Танд дараалалд эсвэл ажиллаж буй {active} ажил байна — нэг дор дээд тал нь {cap}. "
            "Аль нэг нь дуусах эсвэл цуцлагдсаны дараа дахин оролдоно уу."
        )
    cap = settings.quota_active_paid_jobs
    if kind in PAID_KINDS and cap and active_paid >= cap:
        raise _too_many(
            f"Төлбөртэй {active_paid} ажил (яриа таних, санал, орчуулга) дараалалд эсвэл "
            f"ажиллаж байна — нэг дор дээд тал нь {cap}. Аль нэг нь дуусахыг хүлээнэ үү."
        )
    budget = settings.quota_monthly_usd
    if paid and budget is not None:
        spent, unpriced = month_spend(db, principal.id)
        if spent >= budget:
            unknown = (
                f" Үүнд үнэ нь тодорхойгүй {unpriced} хүсэлт ороогүй." if unpriced else ""
            )
            raise _too_many(
                f"Энэ сарын төлбөртэй үйлчилгээний зардал ${spent:.2f} болж, нэг хэрэглэгчийн "
                f"сарын хязгаар ${budget:.2f}-д хүрсэн.{unknown} Админд хандана уу."
            )


def admit_project(db: Session, principal: Principal) -> None:
    """Refuse a new project past the hourly rate (429) or the total (409)."""
    if principal.is_admin:
        return
    settings = get_settings()
    _lock(db, principal.id)
    total, last_hour = db.execute(
        select(
            func.count(),
            func.count().filter(Project.created_at >= time.time() - 3600),
        ).where(Project.owner_id == principal.id)
    ).one()

    cap = settings.quota_projects_per_hour
    if cap and last_hour >= cap:
        raise _too_many(
            f"Сүүлийн нэг цагт {last_hour} төсөл үүсгэсэн — цагт дээд тал нь {cap}. "
            "Түр хүлээгээд дахин оролдоно уу."
        )
    cap = settings.quota_projects_total
    if cap and total >= cap:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Танд {total} төсөл байна — дээд хэмжээ {cap}. "
                "Хэрэггүй болсон төслөө устгаад дахин оролдоно уу."
            ),
        )
