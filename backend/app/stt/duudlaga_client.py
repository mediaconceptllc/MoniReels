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
    ChunkingError,
    compute_pause_boundaries,
    detect_silences,
    encode_for_upload,
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
    "no_speech": "Энэ хэсэгт яриа илрээгүй.",
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
        #
        # Any 5xx counts, not a list of them. The API sits behind Cloudflare,
        # which answers for an origin that fails or times out with 520-527 and
        # an empty body — production hit a bodiless 520 on a 50s chunk, one
        # request after a 500 on the same chunk. Enumerating the numbers is
        # what let that through: the condition was identical and only the
        # digits were new.
        return self.status in (408, 425) or (self.status is not None and self.status >= 500)

    @property
    def ends_the_run(self) -> bool:
        """Whether every remaining chunk is doomed too, not just this one.

        An exhausted balance or a tripped spend cap is a property of the
        ACCOUNT, so the chunks still queued behind this one cannot succeed
        either — sending them is 60-odd pointless requests and a log that
        buries the one line an operator has to read.

        Keyed on the status as well as the code: production answered with
        402 and the credits message, and the code-only check would have let
        the whole batch fly. Same lesson as the 422 and the 520 above — the
        field the API is documented to send is not the field it always sends.
        """
        return self.code in FATAL_CODES or self.status == 402

    @property
    def blames_the_payload(self) -> bool:
        """Whether a smaller chunk is worth trying.

        413 says so outright. A 5xx does not, but the API documents no size
        limit at all and in production answered a 59.4s chunk with 500 three
        times over and a 50.5s chunk with a bodiless Cloudflare 520, while
        every chunk under 22s in the same jobs succeeded — so an oversized
        payload is the reading that fits, whichever number the edge picks.
        Rate, concurrency and spend limits are deliberately excluded:
        splitting makes *more* requests, which is the opposite of what they
        ask for.
        """
        return self.status == 413 or (self.status is not None and self.status >= 500)

    @property
    def means_no_speech(self) -> bool:
        """The documented answer for a window with nothing to transcribe.

        Keyed on the status as well as the code, because production returned
        a 422 carrying the message and no `code`, and a code-only check turned
        a silent chunk — an outcome the pipeline already knew how to skip —
        into a dead job at chunk 2 of 68. Same lesson as the 520: the field
        the API is documented to send is not the field it always sends.
        """
        return self.code == "no_speech" or self.status == 422

    @property
    def rejects_the_format(self) -> bool:
        """Whether the API is refusing the container, not the audio.

        The documented request shows a `.wav` and the accepted formats are
        written down nowhere, so a compressed upload is a guess. This is how
        the guess is taken back — without it, one 400 would fail a job that
        the original bytes would have transcribed.
        """
        return self.status in (400, 415) or self.code == "invalid_request"


@dataclass
class DuudlagaConfig:
    base_url: str
    api_key: str
    max_audio_sec: float
    model: str = ""
    language: str = "mn"
    # Empty sends the WAV as-is. Anything else is an Opus bitrate to
    # re-encode each chunk to before uploading.
    upload_bitrate: str = "32k"
    # Chunks in flight at once. The provider's own transcription is ~0.45x
    # realtime and is where essentially all the wall clock goes: 8 seconds of
    # ffmpeg against 8 minutes of waiting, measured on a 17:44 source.
    concurrency: int = 4


class DuudlagaClient(SttProvider):
    def __init__(self, config: DuudlagaConfig, http_client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = http_client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
        self._owns_client = http_client is None
        # Latched, not re-probed: once the API has rejected a compressed
        # upload there is no reason to spend an encode and a round trip on
        # every remaining chunk to be told the same thing 54 more times.
        self._send_compressed = bool(config.upload_bitrate)

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
            limit = asyncio.Semaphore(max(1, self._config.concurrency))
            fatal: DuudlagaError | None = None

            async def one(i: int, start: float, end: float) -> list[tuple[float, Transcript]]:
                nonlocal fatal
                async with limit:
                    # Checked inside the semaphore, so a verdict that lands
                    # while this chunk waits its turn still stops it. Chunks
                    # already past this point are in flight and are left to
                    # finish; cancelling them is what the comment below is
                    # about.
                    if fatal is not None:
                        raise fatal
                    logger.info(
                        "Sending chunk %d/%d (%.1f-%.1fs)", i + 1, len(boundaries), start, end
                    )
                    try:
                        return await self._transcribe_span(
                            ffmpeg, wav_path, workdir, start, end, f"{i:03d}"
                        )
                    except DuudlagaError as e:
                        if e.ends_the_run and fatal is None:
                            fatal = e
                        raise

            # gather, and never as_completed: merge_transcripts joins the text
            # in list order and does not sort, so the order of these results IS
            # the order of the transcript. gather returns them in argument
            # order however they finish; taking them as they complete would
            # shuffle the interview into nonsense one slow chunk at a time.
            #
            # return_exceptions so a fatal chunk does not leave the rest
            # running unawaited into a workdir this function is about to
            # delete. They are already in flight and already billed; the first
            # failure in chunk order is raised once they have all settled.
            settled = await asyncio.gather(
                *(one(i, s, e) for i, (s, e) in enumerate(boundaries)),
                return_exceptions=True,
            )

            if fatal is not None:
                # Raise the verdict itself, not whichever chunk happens to be
                # first in order - the ones that merely stopped early carry
                # the same exception object but say nothing about why. The
                # count goes in the message because "the run died" and "the
                # run died after 41 of 62 chunks you have already paid for"
                # call for different next steps.
                done = sum(1 for o in settled if not isinstance(o, BaseException))
                raise DuudlagaError(
                    f"{fatal} ({done}/{len(boundaries)} чанхаа амжсан)",
                    status=fatal.status,
                    code=fatal.code,
                ) from fatal

            results: list[tuple[float, Transcript]] = []
            silent = 0
            for outcome in settled:
                if isinstance(outcome, BaseException):
                    raise outcome
                if not outcome:
                    silent += 1
                    continue
                results.extend(outcome)
            if silent:
                logger.info("%d/%d chunk(s) contained no speech", silent, len(boundaries))
            merged = merge_transcripts(results)
            # These were measured to decide where to chunk and were then
            # dropped. They are the cheapest evidence this pipeline has about
            # where a sentence ended — see app.ai.boundaries.
            return merged.model_copy(update={"pauses": [start for start, _ in silences]})
        finally:
            for f in workdir.glob("*"):
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
        """Send one chunk, compressed if the API will take it.

        16 kHz mono PCM is 32 kB per second on the wire, and the API refused
        every chunk that got large while accepting every small one. Opus at
        32 kbps is roughly a ninth of that for speech, which puts even a
        60-second chunk well inside the range that has always worked.

        The compression is a guess — the documented request shows a `.wav`
        and the accepted formats are written down nowhere — so a rejection
        falls back to the original bytes and stops guessing for the rest of
        the job, rather than failing a chunk that WAV would have transcribed.
        """
        if self._send_compressed:
            compressed = wav_path.with_suffix(".opus")
            try:
                await encode_for_upload(
                    _require_ffmpeg(), wav_path, compressed, self._config.upload_bitrate
                )
                # `skip_no_speech=False` so an empty verdict on the compressed
                # bytes comes back as an error to check rather than as "".
                return await self._post_file(compressed, "audio/ogg", skip_no_speech=False)
            except DuudlagaError as e:
                if e.means_no_speech:
                    # Do not take this one on trust. The first span the API
                    # called silent had transcribed fine the run before, as
                    # uncompressed audio — so "no speech" here could equally
                    # be this encode having destroyed it. The original bytes
                    # settle which, and say so in the log.
                    logger.info(
                        "Compressed %s came back as no speech; re-checking the original WAV",
                        wav_path.name,
                    )
                elif e.rejects_the_format:
                    logger.warning(
                        "duudlaga.dev rejected a compressed upload (%s); sending WAV from here on",
                        e,
                    )
                    self._send_compressed = False
                else:
                    raise
            except ChunkingError:
                logger.exception("Opus encode failed; sending WAV from here on")
                self._send_compressed = False
            finally:
                compressed.unlink(missing_ok=True)

        return await self._post_file(wav_path, "audio/wav")

    async def _post_file(self, path: Path, content_type: str, skip_no_speech: bool = True) -> str:
        url = f"{self._config.base_url.rstrip('/')}{TRANSCRIBE_PATH}"
        files = {"file": (path.name, path.read_bytes(), content_type)}
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
            if error.means_no_speech and skip_no_speech:
                logger.info("No speech in %s; skipping the chunk", path.name)
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
            upload_bitrate=settings.duudlaga_upload_bitrate,
            concurrency=settings.duudlaga_concurrency,
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
