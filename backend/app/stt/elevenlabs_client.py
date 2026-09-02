"""ElevenLabs Scribe — speech to text with real word timings and speakers.

Different in kind from duudlaga.dev, not just a second vendor:

**Word-level timestamps.** duudlaga returns text and nothing else, so every
segment boundary in this system is an estimate — a chunk's duration split
across its words by character count (app.stt.chunking). Scribe returns a
start and an end per word, so `timings_estimated` is finally False and a cut
lands where the word actually is.

**Diarization.** Each word carries the speaker who said it, which is the
measurement app.ai.punctuate was inferring from the text with a paid LLM
call. Measured beats inferred.

**Whole file, no chunking.** duudlaga refused anything much over 30 seconds,
which is why there is a pause-splitter, an Opus encoder and a concurrency
limiter in that client. None of that is needed here.

WARNING ON THE CONTRACT. The network this was written on blocks
elevenlabs.io, so the request and response shapes below could not be checked
against the live documentation. They are written from knowledge, defensively:
the parser accepts a response with no `words` at all (falling back to the
plain text), and anything it cannot understand raises with the keys it
actually received rather than a KeyError. Use the reachability check on the
settings page before spending a long file on it.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.models import Segment, Transcript
from app.stt.base import SttProvider
from app.stt.chunking import text_to_transcript
from app.utils import provider_errors
from app.utils.logging import get_logger

logger = get_logger(__name__)

TRANSCRIBE_PATH = "/speech-to-text"
ACCOUNT_PATH = "/user/subscription"

#: Longest a built segment may run before it is closed regardless of
#: punctuation. Matches the cut planner's own cap (app.ai.prompts) so a
#: segment never arrives too coarse to cut inside.
MAX_SEGMENT_SEC = 15.0

#: A whole file goes out in one request, so the read has to outlast the
#: transcription of the entire video, not of a 30-second chunk.
TIMEOUT_S = 900.0


class ElevenLabsError(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def ends_the_run(self) -> bool:
        """Whether the ACCOUNT, not this request, is the problem.

        Same rule the duudlaga client learned the hard way: a quota that is
        spent is spent, and retrying spends nothing but time.
        """
        return self.status in (401, 402, 403)


@dataclass(frozen=True)
class ElevenLabsSttConfig:
    api_key: str
    base_url: str = "https://api.elevenlabs.io/v1"
    model: str = "scribe_v1"
    language: str = "mon"
    diarize: bool = True


class ElevenLabsSttClient(SttProvider):
    def __init__(self, config: ElevenLabsSttConfig, http_client: httpx.AsyncClient | None = None):
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=TIMEOUT_S)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self._config.api_key}

    async def transcribe(self, wav_path: Path) -> Transcript:
        if not self._config.api_key:
            raise ElevenLabsError("ElevenLabs API түлхүүр тавигдаагүй байна.")

        url = f"{self._config.base_url.rstrip('/')}{TRANSCRIBE_PATH}"
        data = {
            "model_id": self._config.model,
            "diarize": "true" if self._config.diarize else "false",
            # Word timings are the whole reason to prefer this provider.
            "timestamps_granularity": "word",
        }
        if self._config.language:
            data["language_code"] = self._config.language

        with wav_path.open("rb") as fh:
            response = await self._client.post(
                url,
                headers=self._headers(),
                data=data,
                files={"file": (wav_path.name, fh, "audio/wav")},
            )

        if response.status_code >= 400:
            # Same split as everywhere else: the body is the operator's, the
            # message is the user's.
            logger.warning(
                "ElevenLabs answered %s: %s", response.status_code, response.text[:600]
            )
            detail = provider_errors.read_error(response).message
            raise ElevenLabsError(
                f"ElevenLabs хүсэлт амжилтгүй ({response.status_code})"
                + (f": {detail}" if detail else ""),
                status=response.status_code,
            )

        return build_transcript(response.json(), wav_duration(wav_path))

    async def account_info(self) -> dict:
        """Subscription and quota, so "will this run" is answerable before a
        long file is uploaded. Same purpose as the duudlaga client's /me."""
        url = f"{self._config.base_url.rstrip('/')}{ACCOUNT_PATH}"
        response = await self._client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise ElevenLabsError(
                f"ElevenLabs хүсэлт амжилтгүй ({response.status_code})", status=response.status_code
            )
        return response.json()


def wav_duration(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, wave.Error):
        return 0.0


def build_transcript(payload: object, fallback_duration: float) -> Transcript:
    """Turns the API's answer into a Transcript.

    Tolerant on purpose — see the contract warning at the top of this module.
    A response with words becomes properly timed segments; one with only text
    falls back to the same character-proportional split duudlaga needs, which
    is worse but is exactly what this pipeline ran on before.
    """
    if not isinstance(payload, dict):
        raise ElevenLabsError(f"ElevenLabs хариу таарсангүй: {type(payload).__name__}")

    text = (payload.get("text") or "").strip()
    language = payload.get("language_code") or "mn"
    words = payload.get("words")

    if not isinstance(words, list) or not words:
        if not text:
            raise ElevenLabsError(
                f"ElevenLabs хариунд текст алга. Талбарууд: {sorted(payload)[:10]}"
            )
        logger.warning("No word timings in the response; falling back to estimated segment times")
        return text_to_transcript(text, fallback_duration, language)

    segments = segments_from_words(words)
    if not segments:
        return text_to_transcript(text, fallback_duration, language)

    return Transcript(
        language=language,
        segments=segments,
        full_text=text or " ".join(s.text for s in segments),
        timings_estimated=False,
        speakers=len({s.speaker for s in segments if s.speaker}),
    )


def segments_from_words(words: list, max_sec: float = MAX_SEGMENT_SEC) -> list[Segment]:
    """Groups words into segments at the places a segment should end.

    A speaker change closes a segment before anything else does: two people's
    words in one subtitle is the failure a viewer notices first, and a cut
    built on such a segment carries half of somebody else's sentence.

    Then sentence-ending punctuation, which Scribe supplies — the thing the
    Mongolian ASR this project started with never did.

    Then the duration cap, so a speaker who never pauses still produces
    segments the cut planner can work inside.
    """
    import uuid as uuid_lib

    out: list[Segment] = []
    buf: list[str] = []
    start: float | None = None
    end = 0.0
    speaker: str | None = None

    def flush() -> None:
        nonlocal buf, start, speaker
        if buf and start is not None:
            text = " ".join(buf).strip()
            if text:
                out.append(
                    Segment(
                        id=uuid_lib.uuid4().hex, start=start, end=end, text=text, speaker=speaker
                    )
                )
        buf, start, speaker = [], None, None

    for word in words:
        if not isinstance(word, dict):
            continue
        # "spacing" and "audio_event" entries are not speech. Keeping them
        # would put "(laughter)" in a subtitle and count it as dialogue.
        if word.get("type") not in (None, "word"):
            continue
        token = (word.get("text") or "").strip()
        if not token:
            continue

        who = word.get("speaker_id")
        who = str(who) if who is not None else None
        if buf and who != speaker:
            flush()

        if start is None:
            start = _as_float(word.get("start"), end)
            speaker = who
        end = _as_float(word.get("end"), start)
        buf.append(token)

        if token[-1] in ".!?…" or (end - start) >= max_sec:
            flush()

    flush()
    return out


def _as_float(value: object, fallback: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def build_client(settings) -> ElevenLabsSttClient:
    return ElevenLabsSttClient(
        ElevenLabsSttConfig(
            api_key=settings.elevenlabs_api_key,
            base_url=settings.elevenlabs_base_url,
            model=settings.elevenlabs_stt_model,
            language=settings.elevenlabs_stt_language,
            diarize=settings.elevenlabs_diarize,
        )
    )
