"""The STT provider contract.

app.stt.duudlaga_client is the only implementation. The abstraction still
earns its place: app.stt.pipeline is written against it, so the provider
that answers is a construction detail rather than something every stage of
the pipeline has to know about — which is what made replacing Chimege a
transport change instead of a rewrite.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.models import Transcript


class SttProvider(ABC):
    @abstractmethod
    async def transcribe(self, wav_path: Path) -> Transcript:
        """wav_path must already be 16 kHz mono 16-bit PCM WAV."""
        raise NotImplementedError
