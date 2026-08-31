"""Which recogniser answers.

The choice is a NAME in the settings, not a guess from which key happens to
be filled in. Two keys can be present at once — they are for different
things elsewhere in this system — and inferring the provider from that would
switch recognisers the moment somebody pasted a key for another feature.

An unknown name is refused loudly at construction. Falling back to a default
would mean the operator selects one provider, the log says another, and the
bill arrives from the second.
"""
from __future__ import annotations

from app.config import Settings
from app.stt.base import SttProvider
from app.utils.logging import get_logger

logger = get_logger(__name__)

DUUDLAGA = "duudlaga"
ELEVENLABS = "elevenlabs"
PROVIDERS = (DUUDLAGA, ELEVENLABS)


class UnknownSttProvider(ValueError):
    pass


def build_client(settings: Settings) -> SttProvider:
    name = (settings.stt_provider or DUUDLAGA).strip().lower()
    if name == DUUDLAGA:
        from app.stt.duudlaga_client import build_client as build

        logger.info("STT provider: duudlaga.dev")
        return build(settings)
    if name == ELEVENLABS:
        from app.stt.elevenlabs_client import build_client as build

        logger.info(
            "STT provider: ElevenLabs %s (diarize=%s)",
            settings.elevenlabs_stt_model, settings.elevenlabs_diarize,
        )
        return build(settings)
    raise UnknownSttProvider(
        f"Тодорхойгүй яриа таних систем: {settings.stt_provider!r}. "
        f"Боломжтой: {', '.join(PROVIDERS)}"
    )


def api_key_for(settings: Settings) -> str:
    """The key the SELECTED provider needs, for the readiness check.

    Reading the wrong one is how a provider reports ready because a different
    vendor's key is set.
    """
    name = (settings.stt_provider or DUUDLAGA).strip().lower()
    return settings.elevenlabs_api_key if name == ELEVENLABS else settings.duudlaga_api_key
