"""The suggestion cards in Mongolian, whatever the model wrote them in.

The prompt asks for them in Mongolian (app.ai.prompts) and a model mostly
complies. Mostly: on an English video the text it copies from is English, and
a card the producer reads in English — or a file an export names in English —
is exactly what a Mongolian studio's tool must not hand them. So once the
ideas are back, every text a producer reads or an export writes is looked at,
and the ones with no Mongolian in them are translated in ONE small call.
When the model complied, which is the usual case, nothing is sent at all.

`hook_quote` is never touched: it is a verbatim quote of the transcript and is
checked against it (app.ai.prompts). `role`, `reason`, `b_roll` and
`why_it_works` are working notes the page does not show, and stay as written.

A failure here costs nothing but the translation: the caller keeps the ideas
it has already paid for, as the model wrote them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai.llm_client import LLMClient
from app.models import Suggestions

SYSTEM_PROMPT = """You translate the texts of short-video suggestions into Mongolian for
a Mongolian audience: titles, on-screen hooks, captions, hashtags and one-line
summaries.

Rules:
- Write natural Mongolian in Cyrillic script, the way a Mongolian social-media
  editor would write it — not word for word.
- Keep each text what it is: a title stays a short title, a question stays a
  question, a hashtag stays ONE hashtag that starts with # and has no spaces.
- Keep names, brands, numbers and acronyms recognisable.
- Return exactly one translation for EVERY text given, under the same index.

Return JSON: {"texts": [{"i": <text index>, "text": "<Mongolian>"}]}"""

SCHEMA = {
    "name": "translated_texts",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["texts"],
        "properties": {
            "texts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["i", "text"],
                    "properties": {"i": {"type": "integer"}, "text": {"type": "string"}},
                },
            },
        },
    },
}

_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")

#: What each text is, in the model's words: a hashtag translated as a sentence
#: is not a hashtag, and a title is not a caption.
_WHAT = {
    ("shorts", "title"): "short title",
    ("shorts", "hook_text"): "on-screen hook",
    ("shorts", "caption"): "post caption",
    ("shorts", "on_screen_texts"): "on-screen text",
    ("shorts", "hashtags"): "hashtag",
    ("youtube", "title"): "video title",
    ("youtube", "throughline"): "one-line summary",
}


def needs_mongolian(text: str) -> bool:
    """Written in another script, with no Mongolian in it.

    A Mongolian title that names a brand ("iPhone-ийн нууц") has Latin letters
    and is still Mongolian; a number alone has neither and needs nothing.
    """
    return bool(_LATIN.search(text or "")) and not _CYRILLIC.search(text or "")


@dataclass(frozen=True)
class Slot:
    """Where one text lives in a set of suggestions."""

    kind: str  # "shorts" | "youtube"
    index: int  # which idea
    field: str
    item: int | None = None  # the position in a list field; None for a plain one


def texts(suggestions: Suggestions) -> list[tuple[Slot, str]]:
    """Every text a producer reads on a card or an export writes out."""
    out: list[tuple[Slot, str]] = []
    for i, short in enumerate(suggestions.shorts):
        for field in ("title", "hook_text", "caption"):
            out.append((Slot("shorts", i, field), getattr(short, field)))
        for field in ("on_screen_texts", "hashtags"):
            for j, text in enumerate(getattr(short, field)):
                out.append((Slot("shorts", i, field, j), text))
    for i, plan in enumerate(suggestions.youtube):
        for field in ("title", "throughline"):
            out.append((Slot("youtube", i, field), getattr(plan, field)))
    return out


def build_prompt(asked: list[tuple[Slot, str]]) -> str:
    lines = "\n".join(
        f"[{i}] ({_WHAT[(slot.kind, slot.field)]}) {text}" for i, (slot, text) in enumerate(asked)
    )
    return f"Translate these {len(asked)} texts:\n\n{lines}"


def _hashtag(text: str) -> str:
    return "#" + re.sub(r"\s+", "", text.strip().lstrip("#"))


def apply(suggestions: Suggestions, asked: list[Slot], answer: dict) -> tuple[Suggestions, int]:
    """Folds an answer onto a copy of the suggestions; returns it and how many
    texts changed. Only indices that were asked are taken, and an empty answer
    is no answer — that text stays as the model first wrote it."""
    by_index: dict[int, str] = {}
    for line in answer.get("texts", []) if isinstance(answer, dict) else []:
        i = line.get("i") if isinstance(line, dict) else None
        text = (line.get("text") or "").strip() if isinstance(line, dict) else ""
        if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(asked) and text:
            by_index[i] = text

    out = suggestions.model_copy(deep=True)
    changed = 0
    for i, text in by_index.items():
        slot = asked[i]
        if slot.field == "hashtags":
            text = _hashtag(text)
            if text == "#":
                continue
        target = (out.shorts if slot.kind == "shorts" else out.youtube)[slot.index]
        if slot.item is None:
            before = getattr(target, slot.field)
            setattr(target, slot.field, text)
        else:
            items = getattr(target, slot.field)
            before = items[slot.item]
            items[slot.item] = text
        changed += before != text
    return out, changed


async def localize_suggestions(
    client: LLMClient, suggestions: Suggestions
) -> tuple[Suggestions, int]:
    """The suggestions with every text a producer reads in Mongolian, and how
    many had to be translated to get there. No call when none did."""
    asked = [(slot, text) for slot, text in texts(suggestions) if needs_mongolian(text)]
    if not asked:
        return suggestions, 0
    answer = await client.complete_json(
        SYSTEM_PROMPT,
        build_prompt(asked),
        SCHEMA["schema"],
        SCHEMA["name"],
        # Mongolian runs about two characters to a token; the JSON around each
        # answer costs a dozen more. Half again on top, so a long caption is
        # never the one cut off.
        max_tokens=max(1000, int(sum(12 + len(text) / 2 for _, text in asked) * 1.5)),
    )
    return apply(suggestions, [slot for slot, _ in asked], answer)
