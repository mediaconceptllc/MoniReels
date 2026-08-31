"""Which font families this image can actually render subtitles in.

libass resolves a font by NAME through fontconfig. Ask for one that is not
installed and it does not fail — it quietly substitutes whatever fontconfig
offers instead, so the operator picks a font, the export uses a different
one, and nothing anywhere says so. SubtitleStyle has shipped with a default
of "Arial" since the beginning, and there is no Arial in this image.

So the list of choices comes from the image itself. `fc-list` is already here
(fontconfig is what libass resolves through), and `:lang=mn` filters to faces
with the Cyrillic coverage Mongolian needs — a family that renders the UI
beautifully and the subtitles as empty boxes is not a choice worth offering.
"""
from __future__ import annotations

import functools
import subprocess

from app.utils.logging import get_logger

logger = get_logger(__name__)

#: Used when fc-list cannot be run at all (a dev machine without fontconfig).
#: Deliberately the families the Dockerfile installs, so the fallback names
#: fonts that are really there rather than inventing a safe-sounding one.
FALLBACK_FAMILIES = ("Inter", "DejaVu Sans", "Noto Sans")

#: What a new project gets. Must be installed by the Dockerfile — a default
#: that is not present is the bug this module exists to stop.
DEFAULT_FAMILY = "Inter"


@functools.lru_cache(maxsize=1)
def available() -> tuple[str, ...]:
    """Installed families with Mongolian Cyrillic coverage, sorted.

    Cached for the life of the process: the font set of a container cannot
    change while it runs, and `fc-list` costs tens of milliseconds that would
    otherwise be paid on every settings page load.
    """
    try:
        result = subprocess.run(
            ["fc-list", ":lang=mn", "family"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("fc-list is not available; offering the fonts the image installs")
        return FALLBACK_FAMILIES

    if result.returncode != 0:
        logger.warning("fc-list failed (%s); offering the fonts the image installs", result.returncode)
        return FALLBACK_FAMILIES

    families: set[str] = set()
    for line in result.stdout.splitlines():
        # fc-list prints comma-separated aliases per face, e.g.
        # "DejaVu Sans,DejaVu Sans Condensed". The first is the family a
        # human recognises and the one libass matches most reliably.
        name = line.split(",")[0].strip()
        if name:
            families.add(name)

    return tuple(sorted(families)) or FALLBACK_FAMILIES


def resolve(family: str) -> str:
    """The family to hand libass, falling back with a WARNING when needed.

    Never raises. A project stored before this existed carries "Arial", and
    failing its export over a font would be worse than rendering it in
    something legible — but the substitution has to be visible in the log,
    which is exactly what libass never does.
    """
    if family in available():
        return family
    fallback = DEFAULT_FAMILY if DEFAULT_FAMILY in available() else available()[0]
    logger.warning(
        "Font %r is not installed in this image; rendering subtitles in %r instead",
        family,
        fallback,
    )
    return fallback
