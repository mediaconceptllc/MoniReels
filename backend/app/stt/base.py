"""Abstract STT provider contract. Chimege is the only implementation — see HARD RULES."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.models import Transcript


class SttProvider(ABC):
    @abstractmethod
    async def transcribe(self, wav_path: Path) -> Transcript:
        """wav_path must already be 16 kHz mono 16-bit PCM WAV."""
        raise NotImplementedError
