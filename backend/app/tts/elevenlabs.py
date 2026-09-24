"""ElevenLabs text to speech — the voice of a Mongolian voice-over.

One line of text in, one clip of speech out. WHERE a clip goes and how fast
it plays belong to app.tts.voiceover; this module only speaks.

**Billed per character, so a request that may have been served is never
sent again.** A 429 is the one retry: ElevenLabs answers it before any
synthesis (too many concurrent requests, or the system is busy), so asking
again costs nothing. A 5xx may have done the work and lost the answer on the
way back — the export fails and says so rather than paying twice.

**The contract was written from knowledge, not checked against the live
documentation** — the network this was written on blocks elevenlabs.io, the
same warning app.stt.elevenlabs_client carries. So the answers are read
defensively: a voice list with fields missing still lists its voices, and a
model list that does not mention the model says "unknown", never "no".
Whether the model speaks Mongolian at all is ASKED of ElevenLabs (the admin
voice picker shows the answer) rather than assumed here.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from app.utils import provider_errors
from app.utils.logging import get_logger

logger = get_logger(__name__)

SPEECH_PATH = "/text-to-speech/{voice_id}"
VOICES_PATH = "/voices"
MODELS_PATH = "/models"

#: What every clip comes back as. Part of the cache key: a different format
#: is a different file.
OUTPUT_FORMAT = "mp3_44100_128"

#: One line is one sentence. Two minutes covers a slow provider without
#: letting a hung socket hold the export's slot for good.
TIMEOUT_S = 120.0

#: v3's "Natural". Its stability takes three values (0.0 Creative, 0.5
#: Natural, 1.0 Robust); the middle one reads a line as written instead of
#: inventing an emotion for it. Not a setting yet — nothing here measures
#: which one a Mongolian voice-over wants.
STABILITY = 0.5
SIMILARITY_BOOST = 0.75

#: 429 only — see the module docstring.
BUSY_RETRIES = 3
BUSY_BACKOFF_S = 2.0
#: A Retry-After longer than this is a quota window, not a busy moment; the
#: export fails with the reason rather than sleeping through it.
RETRY_AFTER_MAX_S = 20.0

#: The voice id goes into the request PATH. Anything but an id — a slash, a
#: dot-dot — would address a different endpoint with the account's key.
VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

#: The codes no retry can fix: the account, the key, the model or a voice is
#: wrong, and every other line would fail the same way — so the export stops
#: at the first one. A voice deleted from the account after a speaker was
#: given it is one of them. `not_configured` is ours: no key, or no voice.
NOT_CONFIGURED = "not_configured"
FINAL_CODES = frozenset({
    "quota_exceeded", "invalid_api_key", "payment_required",
    "voice_not_found", "invalid_uid", "model_not_found", NOT_CONFIGURED,
})

#: What a Mongolian operator reads for the codes ElevenLabs documents. Its own
#: message is English and is kept beside it.
REASONS = {
    "invalid_api_key": "API түлхүүр буруу",
    "quota_exceeded": "Тэмдэгтийн үлдэгдэл дууссан",
    "voice_not_found": "Сонгосон хоолой олдсонгүй",
    "invalid_uid": "Сонгосон хоолой олдсонгүй",
    "model_not_found": "Загвар олдсонгүй",
    "too_many_concurrent_requests": "Зэрэг хүсэлт хэт олон — багцын хязгаар",
    "system_busy": "ElevenLabs завгүй байна",
    "max_character_limit_exceeded": "Нэг хүсэлтийн тэмдэгтийн хязгаар хэтэрсэн",
}

MONGOLIAN_CODES = frozenset({"mn", "mon", "khk"})


class TtsError(Exception):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def ends_the_run(self) -> bool:
        """Whether a retry could not help — see FINAL_CODES."""
        return self.status in (401, 402, 403) or self.code in FINAL_CODES


@dataclass(frozen=True)
class VoiceConfig:
    api_key: str
    voice_id: str
    model: str = "eleven_v3"
    base_url: str = "https://api.elevenlabs.io/v1"

    def fingerprint(self, voice_id: str | None = None) -> str:
        """Everything that decides how a clip sounds except its text — for
        `voice_id`, or the default voice when none is given.

        Part of the cache key, so a new voice or model is a new clip for
        every line — and the same voice reading the same words is never paid
        for twice. The key stays out of it: rotating it changes nothing that
        was spoken.
        """
        return "|".join((
            self.model, voice_id or self.voice_id, str(STABILITY), str(SIMILARITY_BOOST),
            OUTPUT_FORMAT,
        ))


def is_mongolian(language_ids: list[str] | None) -> bool | None:
    """Whether a model's language list includes Mongolian. None when there is
    no list to read — which is "unknown", never "no"."""
    if language_ids is None:
        return None
    return any(code.lower() in MONGOLIAN_CODES for code in language_ids)


class ElevenLabsTts:
    def __init__(
        self,
        config: VoiceConfig,
        http_client: httpx.AsyncClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.config = config
        self._client = http_client or httpx.AsyncClient(timeout=TIMEOUT_S)
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{path}"

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.config.api_key}

    def _require_key(self) -> None:
        if not self.config.api_key:
            raise TtsError("ElevenLabs API түлхүүр тавигдаагүй байна.", code=NOT_CONFIGURED)

    async def synthesize(self, text: str, voice_id: str | None = None) -> bytes:
        """Speech for one line, as MP3 bytes — in `voice_id`, or the default
        voice when none is given."""
        text = text.strip()
        if not text:
            raise TtsError("Хоосон мөрийг дуу болгох боломжгүй.")
        self._require_key()
        voice = voice_id or self.config.voice_id
        if not VOICE_ID.fullmatch(voice or ""):
            raise TtsError("Монгол дууны хоолой сонгогдоогүй байна.", code=NOT_CONFIGURED)

        url = self._url(SPEECH_PATH.format(voice_id=voice))
        payload = {
            "text": text,
            "model_id": self.config.model,
            "voice_settings": {"stability": STABILITY, "similarity_boost": SIMILARITY_BOOST},
        }
        attempt = 0
        while True:
            response = await self._client.post(
                url,
                params={"output_format": OUTPUT_FORMAT},
                headers=self._headers(),
                json=payload,
            )
            if response.status_code != 429 or attempt >= BUSY_RETRIES:
                break
            wait = _retry_after(response)
            if wait is not None and wait > RETRY_AFTER_MAX_S:
                break
            attempt += 1
            await self._sleep(wait if wait is not None else BUSY_BACKOFF_S * attempt)

        if response.status_code >= 400:
            raise _error(response)
        if not response.content:
            raise TtsError("ElevenLabs хоосон дуу буцаалаа.", status=response.status_code)
        return response.content

    async def voices(self) -> list[dict]:
        """The voices this account can use. Free: nothing is synthesised."""
        self._require_key()
        response = await self._client.get(self._url(VOICES_PATH), headers=self._headers())
        if response.status_code >= 400:
            raise _error(response)
        body = response.json()
        rows = body.get("voices") if isinstance(body, dict) else None
        out = []
        for v in rows if isinstance(rows, list) else []:
            if not isinstance(v, dict) or not isinstance(v.get("voice_id"), str):
                continue
            labels = v.get("labels") if isinstance(v.get("labels"), dict) else {}
            out.append({
                "id": v["voice_id"],
                "name": str(v.get("name") or v["voice_id"]),
                "category": str(v.get("category") or ""),
                "gender": str(labels.get("gender") or ""),
                "accent": str(labels.get("accent") or ""),
                # ElevenLabs' own sample, in English — enough to hear the
                # timbre before a character is spent on it.
                "preview_url": v.get("preview_url") if isinstance(v.get("preview_url"), str) else None,
            })
        return sorted(out, key=lambda v: v["name"].lower())

    async def model_languages(self, model_id: str) -> list[str] | None:
        """The language ids ElevenLabs lists for `model_id`, or None when the
        model is not in its list at all."""
        self._require_key()
        response = await self._client.get(self._url(MODELS_PATH), headers=self._headers())
        if response.status_code >= 400:
            raise _error(response)
        body = response.json()
        rows = body if isinstance(body, list) else (body.get("models") if isinstance(body, dict) else None)
        for m in rows if isinstance(rows, list) else []:
            if not isinstance(m, dict) or (m.get("model_id") or m.get("id")) != model_id:
                continue
            languages = m.get("languages") if isinstance(m.get("languages"), list) else []
            ids = []
            for lang in languages:
                code = lang.get("language_id") if isinstance(lang, dict) else lang
                if isinstance(code, str) and code:
                    ids.append(code)
            return sorted(ids)
        return None


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def _error(response: httpx.Response) -> TtsError:
    # The body is the operator's (logged whole); the user gets its message.
    logger.warning("ElevenLabs TTS answered %s: %s", response.status_code, response.text[:600])
    read = provider_errors.read_error(response)
    reason = REASONS.get(read.code or "")
    detail = " — ".join(part for part in (reason, read.message) if part)
    return TtsError(
        f"ElevenLabs дуу үүсгэж чадсангүй ({response.status_code})" + (f": {detail}" if detail else ""),
        status=response.status_code,
        code=read.code,
    )


def build_client(settings) -> ElevenLabsTts:
    return ElevenLabsTts(
        VoiceConfig(
            api_key=settings.elevenlabs_api_key,
            voice_id=settings.elevenlabs_tts_voice_id,
            model=settings.elevenlabs_tts_model,
            base_url=settings.elevenlabs_base_url,
        )
    )
