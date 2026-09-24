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


def for_language(settings: Settings, language: str | None) -> Settings:
    """The recogniser settings for a video spoken in `language`.

    A Mongolian video gets whatever the operator selected. Any other language
    goes to ElevenLabs Scribe with that language's code, because duudlaga.dev
    is a Mongolian recogniser whose API takes no language at all: sent English,
    it returns Mongolian-shaped nonsense and bills for it.

    That used to be the fate of EVERY non-Mongolian video: both recognisers
    were pinned to Mongolian (duudlaga by design, Scribe by `language_code =
    "mon"` in one global setting), so English speech was decoded as Mongolian
    with no error anywhere.

    Returns a copy; the operator's stored settings are never touched.
    """
    from app.languages import MONGOLIAN, SCRIBE_CODES

    language = language or MONGOLIAN
    if language == MONGOLIAN:
        return settings
    return settings.model_copy(
        update={"stt_provider": ELEVENLABS, "elevenlabs_stt_language": SCRIBE_CODES[language]}
    )


def api_key_for(settings: Settings) -> str:
    """The key the SELECTED provider needs, for the readiness check.

    Reading the wrong one is how a provider reports ready because a different
    vendor's key is set.
    """
    name = (settings.stt_provider or DUUDLAGA).strip().lower()
    return settings.elevenlabs_api_key if name == ELEVENLABS else settings.duudlaga_api_key
