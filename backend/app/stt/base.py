"""The STT provider contract.

Two implementations: app.stt.duudlaga_client and app.stt.elevenlabs_client.
app.stt.pipeline, the worker and the admin page are written against this
class, so the provider that answers is a construction detail rather than
something every caller has to know about — which is what made replacing
Chimege a transport change instead of a rewrite.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from app.models import Transcript

if TYPE_CHECKING:
    from app.stt.chunk_cache import ChunkCache


class SttProvider(ABC):
    #: Set by the worker for a provider that sends audio chunk by chunk and
    #: names its recogniser (a `variant` property): chunks an earlier,
    #: unfinished run already paid for are read back instead of bought again
    #: (app.stt.chunk_cache). A provider that answers in one call never reads
    #: it.
    chunk_cache: ChunkCache | None = None

    @abstractmethod
    async def transcribe(self, wav_path: Path) -> Transcript:
        """wav_path must already be 16 kHz mono 16-bit PCM WAV."""
        raise NotImplementedError

    @abstractmethod
    async def account_info(self) -> dict:
        """The provider's own account answer (balance, plan). It can carry the
        key itself, so callers summarise it and never echo it."""
        raise NotImplementedError

    @abstractmethod
    async def aclose(self) -> None:
        """Release the HTTP client. Every caller closes in a `finally`."""
        raise NotImplementedError
