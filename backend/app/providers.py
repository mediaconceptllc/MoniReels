"""Which outside service serves which capability, and whether it can run now.

Two questions this answers that nothing answered before:

**Which provider is doing what.** The keys sit in one list on the settings
page with no indication of what each one powers, and one of them — ElevenLabs
— powers nothing at all yet. A stored key that nothing reads looks exactly
like a working feature until someone depends on it.

**Whether a job can succeed before it is started.** A transcribe with no key,
or against an empty balance, used to queue happily, claim a worker slot,
download the source video and only then fail. Production hit exactly that: 62
chunks, 62 rejections, and the operator's first notice was a dead job.

Reasons are written in Mongolian because the frontend shows an API `detail`
verbatim to a Mongolian-speaking operator — see lib/api.ts `detailOf`.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.config import Settings

STT = "stt"
LLM = "llm"
TTS = "tts"


@dataclass(frozen=True)
class Capability:
    name: str
    label: str
    provider: str
    #: What it powers, in the operator's terms — not the vendor's.
    powers: str
    configured: bool
    #: False when the code to use this provider does not exist yet. Kept
    #: separate from `configured` on purpose: a key can be stored for a
    #: feature nothing reads, and collapsing the two would report that as
    #: ready.
    implemented: bool
    #: Why it cannot run, or None. Shown to the operator as written.
    blocked: str | None

    @property
    def ready(self) -> bool:
        return self.implemented and self.configured and self.blocked is None

    def to_dict(self) -> dict:
        return {**asdict(self), "ready": self.ready}


def describe(settings: Settings) -> list[Capability]:
    """Every capability, in the order an export needs them.

    No network. Reachability and balance are a separate, slower question —
    see the admin providers route — and this has to answer instantly because
    it gates every paid button.
    """
    stt_key = bool(settings.duudlaga_api_key)
    llm_key = bool(settings.openrouter_api_key)
    tts_key = bool(settings.elevenlabs_api_key)

    return [
        Capability(
            name=STT,
            label="Яриа таних",
            provider="duudlaga.dev",
            powers="Видеоны яриаг текст болгож, хадмал үүсгэнэ.",
            configured=stt_key,
            implemented=True,
            blocked=None if stt_key else "duudlaga.dev API түлхүүр тавигдаагүй байна.",
        ),
        Capability(
            name=LLM,
            label="Санал боловсруулах",
            provider=f"OpenRouter · {settings.openrouter_model}",
            powers="Транскриптээс short-ын огтлолуудыг сонгоно.",
            configured=llm_key,
            implemented=True,
            blocked=None if llm_key else "OpenRouter API түлхүүр тавигдаагүй байна.",
        ),
        Capability(
            name=TTS,
            label="Хиймэл дуу",
            provider="ElevenLabs",
            powers="Одоогоор юуг ч ажиллуулахгүй.",
            configured=tts_key,
            implemented=False,
            # Said plainly. A key stored for a feature nothing reads must not
            # look like a working feature, or the first attempt to use it
            # becomes a bug report.
            blocked="Хиймэл дуу оруулах хэсэг хараахан хэрэгжээгүй. Түлхүүр хадгалагдана, "
            "гэхдээ одоогоор ямар ч ажил үүгээр явахгүй.",
        ),
    ]


def blocker(settings: Settings, capability: str) -> str | None:
    """Why `capability` cannot run right now, or None.

    Called before a paid job is queued. Returning a reason here costs
    nothing; discovering the same thing inside a worker costs a slot, a
    download, and the operator's afternoon.
    """
    for item in describe(settings):
        if item.name == capability:
            return item.blocked if not item.ready else None
    return f"Тодорхойгүй чадвар: {capability}"
