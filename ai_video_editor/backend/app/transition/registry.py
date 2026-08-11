"""UI transition name -> xfade filter name, with fallback when unsupported.

Only transitions the installed FFmpeg actually supports (per capabilities
probing) are exposed to the frontend; see resolve()/available_transitions().
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_TRANSITION_DURATION = 0.25
MAX_TRANSITION_DURATION = 2.00
TRANSITION_DURATION_STEP = 0.05


@dataclass(frozen=True)
class TransitionDef:
    ui_name: str
    xfade_name: str
    fallback_xfade_name: str | None  # used if xfade_name isn't supported


REGISTRY: list[TransitionDef] = [
    TransitionDef("Fade", "fadeblack", "fade"),
    TransitionDef("Cross Fade", "fade", None),
    TransitionDef("Dissolve", "dissolve", "fade"),
    TransitionDef("Wipe Left", "wipeleft", "fade"),
    TransitionDef("Wipe Right", "wiperight", "fade"),
    TransitionDef("Slide Left", "slideleft", "wipeleft"),
    TransitionDef("Slide Right", "slideright", "wiperight"),
    TransitionDef("Zoom", "zoomin", "fade"),
    TransitionDef("Circle Open", "circleopen", "fade"),
    TransitionDef("Circle Close", "circleclose", "fade"),
    TransitionDef("Blur", "hblur", "dissolve"),
    TransitionDef("Pixelize", "pixelize", "dissolve"),
]

_BY_UI_NAME = {t.ui_name: t for t in REGISTRY}


def resolve(ui_name: str, supported_xfade: list[str]) -> str:
    """Return the xfade filter name to actually use for a UI transition name.

    Falls back per the table, then finally to plain "fade" (always supported
    by any FFmpeg build with xfade) if even the fallback isn't available.
    """
    definition = _BY_UI_NAME.get(ui_name)
    if definition is None:
        raise ValueError(f"Unknown transition: {ui_name!r}")

    supported = set(supported_xfade)
    if definition.xfade_name in supported:
        return definition.xfade_name
    if definition.fallback_xfade_name and definition.fallback_xfade_name in supported:
        return definition.fallback_xfade_name
    return "fade"


def available_transitions(supported_xfade: list[str]) -> list[dict]:
    """UI-facing list: every registry entry, flagged with whether it (or its
    fallback) is actually usable on this machine's FFmpeg build.
    """
    supported = set(supported_xfade)
    result = []
    for t in REGISTRY:
        usable = t.xfade_name in supported or (
            t.fallback_xfade_name in supported if t.fallback_xfade_name else False
        )
        result.append({
            "ui_name": t.ui_name,
            "xfade_name": t.xfade_name,
            "supported": usable,
        })
    return result
