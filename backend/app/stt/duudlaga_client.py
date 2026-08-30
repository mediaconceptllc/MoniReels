"""duudlaga.dev — Mongolian speech-to-text. The only STT provider.

Replaces Chimege. Everything that was NOT Chimege-specific already moved to
app.stt.chunking; what remains here is transport only.

Written against the published API:

    POST {base}/stt/transcriptions   multipart file= -> {"id","text",...}
    GET  {base}/me                   key info, balance, limits

The error contract is the part that shapes this file. Every failure comes
back as JSON with a `code`, and the codes are NOT interchangeable — three of
them need behaviour that a plain status-code check gets wrong:

  * 422 `no_speech` is not a failure at all. VAD picks the windows we send,
    and it does mis-fire on a music transient or a cough. Treating that as an
    error would let one bad two-second window kill an hour-long
    transcription; the chunk simply contributes nothing.
  * 402 must never be retried. Retrying a spent balance produces three
    identical failures and hides the one thing the operator needs to read.
  * 429 `daily_spend_cap_exceeded` must never be retried either, even though
    the other two 429s should be. A daily cap does not clear within the
    lifetime of a job — only the clock resolves it.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from app.models import Transcript
from app.stt.base import SttProvider
from app.stt.chunking import (
    TARGET_CHUNK_MIN_SEC,
    compute_pause_boundaries,
    detect_silences,
    extract_chunk,
    merge_transcripts,
    text_to_transcript,
    wav_duration_sec,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# Overridable without a redeploy, in case the route moves.
TRANSCRIBE_PATH = os.environ.get("DUUDLAGA_TRANSCRIBE_PATH", "/stt/transcriptions")
ACCOUNT_PATH = os.environ.get("DUUDLAGA_ACCOUNT_PATH", "/me")

MAX_ATTEMPTS = 3
BACKOFF_BASE_SEC = 1.0
BACKOFF_FACTOR = 2.0
# A server-sent Retry-After is honoured, but not without a ceiling: a job
# holding its worker slot for ten minutes on one chunk is worse than failing
# and being requeued.
MAX_RETRY_AFTER_SEC = 60.0
REQUEST_TIMEOUT_SEC = 180.0

# Retry only helps when the condition is transient.
RETRYABLE_CODES = frozenset({"rate_limit_exceeded", "concurrency_limit_exceeded", "internal_error"})
# A spent balance or a daily cap does not resolve on its own within a job.
FATAL_CODES = frozenset({"insufficient_credits", "payment_required", "daily_spend_cap_exceeded"})

# How far a chunk the server refused may be halved before the span is given
# up on. Each level doubles the spans and every span costs MAX_ATTEMPTS
# requests, so depth is expensive: 2 is 21 requests worst case, 4 would be
# 93. Two halvings take the 30s ceiling to 7.5s, well under the 21s that
# production transcribed without complaint.
MAX_SPLIT_DEPTH = 2
# Never split below this: sub-second chunks cut words in half, and a span
# this short contributes almost nothing to the transcript anyway.
MIN_SPLIT_SEC = 3.0

# Mongolian, because this reaches the producer through the job's error field.
CODE_MESSAGES = {
    "invalid_request": "Хүсэлт буруу бүрдсэн байна.",
    "insufficient_credits": "duudlaga.dev дээрх кредит дууссан байна. Цэнэглээд дахин оролдоно уу.",
    "payment_required": "duudlaga.dev-ийн автомат төлбөр амжилтгүй болж хандалт түр зогссон байна.",
    "rate_limit_exceeded": "duudlaga.dev-ийн хүсэлтийн хязгаарт хүрлээ.",
    "daily_spend_cap_exceeded": "Энэ түлхүүрийн өдрийн зарлагын хязгаарт хүрлээ.",
    "concurrency_limit_exceeded": "duudlaga.dev рүү зэрэг явуулах хүсэлтийн хязгаарт хүрлээ.",
    "internal_error": "duudlaga.dev дээр серверийн алдаа гарлаа.",
}


class DuudlagaError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        if self.code in FATAL_CODES:
            return False
        if self.code in RETRYABLE_CODES:
            return True
        # An unrecognised code is retried only when the status itself says
        # "transient". A new code the API adds later must not be assumed
        # billable-and-safe to repeat.
        return self.status in (408, 425, 500, 502, 503, 504)

    @property
    def blames_the_payload(self) -> bool:
        """Whether a smaller chunk is worth trying.

        413 says so outright. A 500 does not, but the API documents no size
        limit at all and in production returned one, three times over, for
        the same 59s chunk while every chunk under 22s in the same job
        succeeded — so an oversized payload is the reading that fits. Rate,
        concurrency and spend limits are deliberately excluded: splitting
        makes *more* requests, which is the opposite of what they ask for.
        """
        return self.status == 413 or self.code == "internal_error" or self.status == 500


@dataclass
class DuudlagaConfig:
    base_url: str
    api_key: str
    max_audio_sec: float
    model: str = ""
    language: str = "mn"


class DuudlagaClient(SttProvider):
    def __init__(self, config: DuudlagaConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def max_audio_sec(self) -> float:
        return self._config.max_audio_sec

    # -- public API --------------------------------------------------------

    async def transcribe_chunk_text(self, chunk_wav_path: Path) -> str:
        """One already-cut chunk in, raw text out — no timing attached.

        Returns "" when the API reports `no_speech`. This is the single
        network primitive both this class's own pause chunking and the VAD
        pipeline in app.stt.pipeline build on, so neither has to know the
        wire format or the error contract.
        """
        return await self._with_retry(self._post_chunk, chunk_wav_path)

    async def transcribe(self, wav_path: Path) -> Transcript:
        """Whole file in, Transcript out.

        `wav_path` must already be 16 kHz mono 16-bit PCM. The file is ALWAYS
        pause-split first, even when it is shorter than max_audio_sec: a whole
        file is never sent as one request, and compute_pause_boundaries
        degrades to a single [0, duration] chunk on its own for short clips.
        """
        self._require_config()

        ffmpeg = _require_ffmpeg()
        total_duration = wav_duration_sec(wav_path)
        silences = await detect_silences(ffmpeg, wav_path)
        boundaries = compute_pause_boundaries(
            total_duration, silences, self._config.max_audio_sec, min_chunk_sec=TARGET_CHUNK_MIN_SEC
        )
        logger.info(
            "Splitting %.1fs of audio into %d pause-based chunk(s): %s",
            total_duration,
            len(boundaries),
            ", ".join(f"[{s:.1f}-{e:.1f}]" for s, e in boundaries),
        )

        workdir = wav_path.parent / f"{wav_path.stem}_chunks"
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            results: list[tuple[float, Transcript]] = []
            silent = 0
            for i, (start, end) in enumerate(boundaries):
                logger.info("Sending chunk %d/%d (%.1f-%.1fs)", i + 1, len(boundaries), start, end)
                spans = await self._transcribe_span(
                    ffmpeg, wav_path, workdir, start, end, f"{i:03d}"
                )
                if not spans:
                    silent += 1
                    continue
                results.extend(spans)
            if silent:
                logger.info("%d/%d chunk(s) contained no speech", silent, len(boundaries))
            return merge_transcripts(results)
        finally:
            for f in workdir.glob("*.wav"):
                f.unlink(missing_ok=True)
            workdir.rmdir()

    async def _transcribe_span(
        self,
        ffmpeg: str,
        wav_path: Path,
        workdir: Path,
        start: float,
        end: float,
        tag: str,
        depth: int = 0,
    ) -> list[tuple[float, Transcript]]:
        """One span in, positioned transcripts out — halved if it is refused.

        The API documents no maximum chunk length and answers an oversized
        one with a bare 500, which `_with_retry` then repeats identically
        twice more before the whole job dies. In production that threw away
        six chunks already transcribed and paid for, six minutes in, because
        chunk seven happened to be 59s long while every chunk under 22s in
        the same job had succeeded.

        Halving turns that into a transcript without having to know the
        limit the API declines to state, and terminates on its own: each
        level doubles the request count, so MAX_SPLIT_DEPTH and
        MIN_SPLIT_SEC bound what one bad span can cost.
        """
        chunk_path = workdir / f"chunk_{tag}.wav"
        await extract_chunk(ffmpeg, wav_path, chunk_path, start, end)
        try:
            text = await self.transcribe_chunk_text(chunk_path)
        except DuudlagaError as e:
            if not e.blames_the_payload or depth >= MAX_SPLIT_DEPTH or end - start <= MIN_SPLIT_SEC:
                raise
            mid = (start + end) / 2
            logger.warning(
                "Chunk %s (%.1f-%.1fs) was refused; halving it and sending both parts: %s",
                tag, start, end, e,
            )
            return [
                *await self._transcribe_span(
                    ffmpeg, wav_path, workdir, start, mid, f"{tag}a", depth + 1
                ),
                *await self._transcribe_span(
                    ffmpeg, wav_path, workdir, mid, end, f"{tag}b", depth + 1
                ),
            ]
        finally:
            chunk_path.unlink(missing_ok=True)

        if not text:
            return []
        return [(start, text_to_transcript(text, end - start, self._config.language))]

    async def account_info(self) -> dict:
        """Key info, balance and limits from GET /me.

        Exists so "the transcription failed" can be answered BEFORE the job
        runs. `insufficient_credits` mid-render costs a worker slot and a
        download for a job that could never have finished.
        """
        self._require_config()
        response = await self._client.get(
            f"{self._config.base_url.rstrip('/')}{ACCOUNT_PATH}", headers=self._headers()
        )
        if response.status_code >= 400:
            raise _error_from_response(response)
        return response.json()

    # -- wire format -------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.api_key}"}

    async def _post_chunk(self, wav_path: Path) -> str:
        url = f"{self._config.base_url.rstrip('/')}{TRANSCRIBE_PATH}"
        files = {"file": (wav_path.name, wav_path.read_bytes(), "audio/wav")}
        # The documented request carries only the file. `model` is sent only
        # when explicitly configured — an unexpected field risks a 400
        # invalid_request for no gain.
        data = {"model": self._config.model} if self._config.model else None

        response = await self._client.post(url, headers=self._headers(), files=files, data=data)

        if response.status_code >= 400:
            error = _error_from_response(response)
            # Not a failure: VAD chose this window, and it does mis-fire on a
            # transient. One empty two-second window must not end an
            # hour-long transcription.
            if error.code == "no_speech":
                logger.info("No speech in %s; skipping the chunk", wav_path.name)
                return ""
            raise error

        return self._parse_response(response.json())

    @staticmethod
    def _parse_response(payload: object) -> str:
        """Pull the transcript text out of the response.

        `text` is what the API documents. The other keys are accepted because
        an unrecognised shape must not silently become "" — which would be
        indistinguishable from "this audio had no speech", the one outcome
        that already has its own explicit signal.
        """
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, list):
            return " ".join(DuudlagaClient._parse_response(item) for item in payload).strip()
        if isinstance(payload, dict):
            for key in ("text", "transcription", "transcript", "result"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value.strip()
                if isinstance(value, list | dict):
                    return DuudlagaClient._parse_response(value)
            segments = payload.get("segments")
            if isinstance(segments, list):
                return " ".join(s.get("text", "") for s in segments if isinstance(s, dict)).strip()
        raise DuudlagaError(f"Unrecognised duudlaga.dev response shape: {str(payload)[:300]}")

    # -- retry -------------------------------------------------------------

    def _require_config(self) -> None:
        if not self._config.base_url or not self._config.api_key:
            raise DuudlagaError("DUUDLAGA_BASE_URL / DUUDLAGA_API_KEY are not configured")

    async def _with_retry(self, call: Callable[..., Awaitable[T]], *args: object) -> T:
        self._require_config()
        last: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return await call(*args)
            except httpx.TimeoutException as e:
                last = e
                delay = _backoff(attempt)
            except httpx.HTTPError as e:
                # A transport failure (DNS, reset) is transient by nature.
                last = e
                delay = _backoff(attempt)
            except DuudlagaError as e:
                if not e.retryable:
                    raise
                last = e
                # Honour the server's own Retry-After when it sends one; it
                # knows when the window reopens and we do not.
                delay = min(e.retry_after, MAX_RETRY_AFTER_SEC) if e.retry_after else _backoff(attempt)

            if attempt < MAX_ATTEMPTS:
                logger.warning(
                    "duudlaga.dev attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt, MAX_ATTEMPTS, type(last).__name__, delay,
                )
                await asyncio.sleep(delay)

        message = f"duudlaga.dev request failed after {MAX_ATTEMPTS} attempts: {last}"
        if isinstance(last, DuudlagaError):
            # Carry the classification forward. Rebuilding a bare error here
            # erased the status and code, so every caller that decides on
            # them — retry policy, the chunk-halving above — saw an
            # unclassified failure and could only give up.
            raise DuudlagaError(
                message, status=last.status, code=last.code, retry_after=last.retry_after
            ) from last
        raise DuudlagaError(message) from last


def build_client(settings) -> DuudlagaClient:
    return DuudlagaClient(
        DuudlagaConfig(
            base_url=settings.duudlaga_base_url,
            api_key=settings.duudlaga_api_key,
            max_audio_sec=settings.duudlaga_max_audio_sec,
            model=settings.duudlaga_model,
        )
    )


def _backoff(attempt: int) -> float:
    return BACKOFF_BASE_SEC * (BACKOFF_FACTOR ** (attempt - 1))


def _require_ffmpeg() -> Path:
    from app.video.ffmpeg import discover_ffmpeg

    binaries = discover_ffmpeg()
    if binaries.ffmpeg is None:
        raise DuudlagaError("FFmpeg is required for audio chunking but was not found")
    return binaries.ffmpeg


def _error_from_response(response: httpx.Response) -> DuudlagaError:
    """Build a typed error from the documented `{code, message}` body.

    The code, not the status, decides what happens next: two 429s are
    retryable and a third (a daily cap) is not, and a 422 is not an error at
    all. Branching on the status alone gets all three wrong.
    """
    code: str | None = None
    detail: str | None = None
    try:
        body = response.json()
        if isinstance(body, dict):
            error = body.get("error") if isinstance(body.get("error"), dict) else body
            code = error.get("code") if isinstance(error, dict) else None
            detail = error.get("message") if isinstance(error, dict) else None
    except ValueError:
        pass

    retry_after = _retry_after_seconds(response)
    message = CODE_MESSAGES.get(code or "", "")

    if not message:
        if response.status_code in (401, 403):
            message = "duudlaga.dev API түлхүүрийг татгалзлаа. DUUDLAGA_API_KEY-г шалгана уу."
        elif response.status_code == 404:
            message = (
                f"duudlaga.dev {response.request.url} хаягт 404 өглөө. "
                "DUUDLAGA_TRANSCRIBE_PATH-ыг шалгана уу."
            )
        elif response.status_code == 413:
            message = "Аудионы хэсэг хэт том байна. DUUDLAGA_MAX_AUDIO_SEC-ийг багасгана уу."
        else:
            message = f"duudlaga.dev хүсэлт амжилтгүй ({response.status_code})"

    # The server's own message is appended, not substituted: it carries
    # detail no local table can (which field was invalid, how low the
    # balance is).
    if detail and detail not in message:
        message = f"{message} ({detail})"

    return DuudlagaError(message, status=response.status_code, code=code, retry_after=retry_after)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        # The header also permits an HTTP-date. Falling back to normal
        # backoff is better than parsing a date format wrong and sleeping
        # for hours.
        return None
