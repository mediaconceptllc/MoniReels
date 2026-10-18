"""What each job bought from a paid provider — written down however it ended.

The only record of spend used to be `jobs.result.output.llm`, and it was
written on the SUCCESS path alone. A transcription that billed forty minutes
of audio and then failed on its last chunk, or a job canceled after its
model calls, left no trace anywhere; and `queue.purge_old` deletes even the
successful rows after `job_keep_days`. So the producer's question "what did
this month cost, and for whom?" had no answer the system could give.

This meter is filled at the same single gates the LLM meter is — one per
provider: `OpenRouterClient._record_usage`, the speech-to-text clients'
successful responses, the TTS client's successful synthesis — and
`worker._run_job` flushes it into `usage_events` from its `finally`. A
failed, canceled or abandoned attempt therefore records exactly what it
bought, and the ledger is a table of its own that no purge touches.

Prices are never invented. OpenRouter states the cost of each call it
answers; the speech providers state none, so theirs comes from a rate the
operator configured (app.config) or stays NULL. NULL reads "unknown" in the
report, where a zero would read "free".

Not captured, by construction: a process killed outright (OOM, SIGKILL)
never reaches `finally`, and paid calls made outside a job have no meter to
count into — there are none today, and an API request must not make one
(heavy work is always a job).
"""
from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.dbmodels import Project, UsageEvent
from app.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Line:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    audio_seconds: float = 0.0
    characters: int = 0
    cost_usd: float = 0.0


# (provider, kind, model, priced). Priced and unpriced calls are separate
# lines, so a row's cost is either the whole truth or NULL — never a known
# part quietly standing in for a total.
Key = tuple[str, str, str, bool]


@dataclass
class Meter:
    lines: dict[Key, Line] = field(default_factory=dict)
    # Calls arrive from concurrent STT chunks, and a context copied into a
    # thread shares this same object; the lock keeps two adds from racing.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(
        self,
        *,
        provider: str,
        kind: str,
        model: str,
        cost: float | None,
        prompt: int = 0,
        completion: int = 0,
        seconds: float = 0.0,
        characters: int = 0,
    ) -> None:
        key = (provider, kind, model or "", cost is not None)
        with self._lock:
            line = self.lines.setdefault(key, Line())
            line.requests += 1
            line.prompt_tokens += prompt
            line.completion_tokens += completion
            line.audio_seconds += seconds
            line.characters += characters
            line.cost_usd += cost or 0.0


_current: ContextVar[Meter | None] = ContextVar("usage_meter", default=None)


def start() -> Meter:
    meter = Meter()
    _current.set(meter)
    return meter


def current() -> Meter | None:
    return _current.get()


def record_llm(*, model: str, prompt: int, completion: int, cost: float | None) -> None:
    """One answered OpenRouter call. `cost` is what OpenRouter said it cost,
    or None when the response carried no price."""
    meter = _current.get()
    if meter is not None:
        meter.add(
            provider="openrouter", kind="llm", model=model, cost=cost,
            prompt=prompt, completion=completion,
        )


def record_stt(*, provider: str, model: str, seconds: float) -> None:
    """Audio a speech-to-text provider accepted and answered."""
    meter = _current.get()
    if meter is not None:
        meter.add(
            provider=provider, kind="stt", model=model,
            cost=_rated(stt_rate(provider), seconds / 60), seconds=seconds,
        )


def record_tts(*, provider: str, model: str, characters: int) -> None:
    """Text a TTS provider turned into speech."""
    meter = _current.get()
    if meter is not None:
        meter.add(
            provider=provider, kind="tts", model=model,
            cost=_rated(tts_rate(provider), characters / 1000), characters=characters,
        )


def stt_rate(provider: str) -> float | None:
    """USD per minute of audio, or None when nobody configured one."""
    s = get_settings()
    return {
        "duudlaga": s.duudlaga_usd_per_minute,
        "elevenlabs": s.elevenlabs_stt_usd_per_minute,
    }.get(provider)


def tts_rate(provider: str) -> float | None:
    """USD per thousand characters, or None when nobody configured one."""
    s = get_settings()
    return {"elevenlabs": s.elevenlabs_tts_usd_per_1k_chars}.get(provider)


def _rated(rate: float | None, units: float) -> float | None:
    return None if rate is None else rate * units


def flush(
    meter: Meter | None, *, job_id: str | None, job_kind: str, project_id: str | None
) -> int:
    """Append one ledger row per line. Returns how many were written.

    The user is the project's owner, read now. A job carries no record of
    who pressed the button, and an editor can only start work on their own
    project — so the owner is who it was, except when an admin ran it for
    them. A project deleted mid-job leaves the user empty: the spend is
    still recorded, under its project id.
    """
    if meter is None:
        return 0
    with meter._lock:
        lines = list(meter.lines.items())
    if not lines:
        return 0
    with session_scope() as db:
        user_id = (
            db.scalar(select(Project.owner_id).where(Project.id == project_id))
            if project_id
            else None
        )
        db.add_all(
            UsageEvent(
                job_id=job_id,
                job_kind=job_kind,
                project_id=project_id,
                user_id=user_id,
                provider=provider,
                kind=kind,
                model=model[:120],
                requests=line.requests,
                prompt_tokens=line.prompt_tokens,
                completion_tokens=line.completion_tokens,
                audio_seconds=round(line.audio_seconds, 3),
                characters=line.characters,
                # Six places for the same reason as the job result: a call on
                # a cheap model lands far below a cent.
                cost_usd=round(line.cost_usd, 6) if priced else None,
            )
            for (provider, kind, model, priced), line in lines
        )
    return len(lines)


def describe(meter: Meter | None) -> str:
    """The meter in one log line — what is left when the ledger write itself
    fails, so the spend is at least somewhere an operator can find it."""
    if meter is None:
        return "nothing"
    with meter._lock:
        lines = list(meter.lines.items())
    return "; ".join(
        f"{provider}/{kind} {model or '-'}: {line.requests} req"
        + (f", {line.prompt_tokens}+{line.completion_tokens} tok" if kind == "llm" else "")
        + (f", {line.audio_seconds:.1f}s" if kind == "stt" else "")
        + (f", {line.characters} chars" if kind == "tts" else "")
        + (f", ${line.cost_usd:.6f}" if priced else ", price unknown")
        for (provider, kind, model, priced), line in lines
    ) or "nothing"
