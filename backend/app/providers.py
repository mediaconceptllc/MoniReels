"""Which outside service serves which capability, and whether it can run now.

Two questions this answers that nothing answered before:

**Which provider is doing what.** The keys sit in one list on the settings
page with no indication of what each one powers. A stored key that nothing
reads looks exactly like a working feature until someone depends on it —
which is why the voice-over reported itself unbuilt until it was built.

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
from app.worker_report import WorkerReport

STT = "stt"
LLM = "llm"
TTS = "tts"
SEPARATION = "separation"


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
    from app.stt import factory

    # The SELECTED provider's key, never "any key is set": two keys can be
    # present at once and reading the wrong one reports ready because a
    # different vendor is configured.
    stt_name = (settings.stt_provider or factory.DUUDLAGA).strip().lower()
    stt_known = stt_name in factory.PROVIDERS
    stt_key = bool(factory.api_key_for(settings)) if stt_known else False
    stt_label = {
        factory.DUUDLAGA: "duudlaga.dev",
        factory.ELEVENLABS: f"ElevenLabs {settings.elevenlabs_stt_model}",
    }.get(stt_name, settings.stt_provider)

    llm_key = bool(settings.openrouter_api_key)
    tts_key = bool(settings.elevenlabs_api_key)
    tts_voice = bool(settings.elevenlabs_tts_voice_id)
    if not tts_key:
        tts_blocked = "Монгол дуу үүсгэх ElevenLabs-ийн API түлхүүр тавигдаагүй байна."
    elif not tts_voice:
        # A key alone is not a voice-over: with no voice there is nothing to
        # send, and the first export to find that out would be the first
        # one somebody asked for.
        tts_blocked = "Монгол дууны хоолой сонгогдоогүй байна — Тохиргоо хуудаснаас сонгоно уу."
    else:
        tts_blocked = None

    if not stt_known:
        stt_blocked = (
            f"Тодорхойгүй яриа таних систем: {settings.stt_provider!r}. "
            f"Боломжтой: {', '.join(factory.PROVIDERS)}"
        )
    elif not stt_key:
        stt_blocked = f"{stt_label}-ийн API түлхүүр тавигдаагүй байна."
    else:
        stt_blocked = None

    return [
        Capability(
            name=STT,
            label="Яриа таних",
            provider=stt_label,
            powers="Видеоны яриаг текст болгож, хадмал үүсгэнэ.",
            configured=stt_key,
            implemented=stt_known,
            blocked=stt_blocked,
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
            label="Монгол дуу",
            provider=f"ElevenLabs {settings.elevenlabs_tts_model}",
            powers="Монголоос бусад хэлтэй видеоны экспортод орчуулгыг монгол дуугаар уншуулна.",
            configured=tts_key and tts_voice,
            implemented=True,
            blocked=tts_blocked,
        ),
    ]


def separation(report: WorkerReport | None) -> Capability:
    """Whether an export can remove the source speech under the Mongolian
    voice (export.source_speech "remove").

    Answered from what the WORKER said about itself (app.worker_report): the
    API image never has Demucs, so looking for it here would always say no.
    No report yet — no worker running this code has started — is unknown,
    and unknown does not start a job that cannot finish.
    """
    if report is None:
        blocked = (
            "Worker энэ боломжийн талаар хараахан мэдээлээгүй байна — ажиллаж байгаа эсэхийг "
            "шалгана уу."
        )
    elif not report.separation:
        blocked = "Worker-т Demucs суугаагүй байна — worker сервисийг INSTALL_DUB=1-ээр дахин build хийнэ."
    else:
        blocked = None
    return Capability(
        name=SEPARATION,
        label="Эх яриа арилгах",
        provider=f"Demucs {report.model} (worker)" if report is not None else "Demucs (worker)",
        powers="Монгол дуу оруулахад эх яриаг хөгжим, орчны чимээнээс салгаж бүрэн хасна.",
        configured=report is not None and report.separation,
        implemented=True,
        blocked=blocked,
    )


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
