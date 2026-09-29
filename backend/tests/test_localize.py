"""The suggestion cards in Mongolian, whatever the model wrote them in.

What these hold: a card that is already Mongolian costs no call; only the
texts with no Mongolian in them are sent, each labelled with what it is; the
quote the hook is checked against is never sent; the answers land where they
were asked from, and a hashtag comes back as one hashtag.
"""
from __future__ import annotations

import asyncio

import pytest

from app.ai.localize import localize_suggestions, needs_mongolian
from app.models import Cut, KeepRange, ShortIdea, Suggestions, YoutubePlan


def _ideas(**short) -> Suggestions:
    fields = {
        "id": "sh1",
        "title": "Нийлмэл хүүгийн нууц",
        "hook_text": "30 жилд юу болох вэ?",
        "hook_quote": "Most people never learn how compound interest works.",
        "caption": "Мөнгө өсгөх энгийн жишээ",
        "hashtags": ["#санхүү"],
        "on_screen_texts": ["Нийлмэл хүү"],
        "why_it_works": "hook 9, b-roll 7, relevance 8",
        "cuts": [Cut(start=0.0, end=5.0, role="hook", reason="the claim")],
        **short,
    }
    return Suggestions(
        shorts=[ShortIdea(**fields)],
        youtube=[YoutubePlan(
            title="Мөнгө хэрхэн өсдөг вэ", throughline="Гурван зуршил",
            ranges=[KeepRange(start=0.0, end=600.0)], total_duration=600.0,
        )],
    )


class FakeLlm:
    def __init__(self, answer: dict | None = None) -> None:
        self.answer = answer or {"texts": []}
        self.prompts: list[str] = []

    async def complete_json(self, system, user, json_schema, schema_name, temperature=0.4,
                            max_tokens=None):
        self.prompts.append(user)
        return self.answer


def _run(client, ideas):
    return asyncio.run(localize_suggestions(client, ideas))


@pytest.mark.parametrize("text, needs", [
    ("Why Most People Never Get Rich", True),
    ("#finance", True),
    ("Нийлмэл хүүгийн нууц", False),
    # A brand inside a Mongolian title is still a Mongolian title.
    ("iPhone-ийн нууц", False),
    # Nothing to translate in a number.
    ("2024", False),
    ("", False),
])
def test_a_text_with_no_mongolian_in_it_is_the_one_that_needs_it(text, needs):
    assert needs_mongolian(text) is needs


def test_a_card_already_in_mongolian_costs_no_call():
    client = FakeLlm()
    ideas = _ideas()
    out, changed = _run(client, ideas)
    assert client.prompts == [] and changed == 0 and out is ideas


def test_only_the_texts_with_no_mongolian_are_sent_each_saying_what_it_is():
    client = FakeLlm()
    ideas = _ideas(hashtags=["#санхүү", "#finance"])
    ideas.youtube[0].title = "Why Most People Never Get Rich"
    _run(client, ideas)

    (prompt,) = client.prompts
    assert "Translate these 2 texts" in prompt
    assert "(hashtag) #finance" in prompt
    assert "(video title) Why Most People Never Get Rich" in prompt
    assert "Нийлмэл хүүгийн нууц" not in prompt


def test_the_quote_the_hook_is_checked_against_is_never_sent():
    """It is verbatim transcript; translated, it could never match again."""
    client = FakeLlm()
    _run(client, _ideas(title="The secret of compound interest"))
    (prompt,) = client.prompts
    assert "Most people never learn" not in prompt
    assert "hook 9" not in prompt  # working notes stay as written


def test_the_answers_land_where_they_were_asked_from():
    ideas = _ideas(title="The secret", hashtags=["#finance", "#санхүү", "#money tips"])
    ideas.youtube[0].throughline = "Three habits"
    client = FakeLlm({"texts": [
        {"i": 0, "text": "Нууц"},
        {"i": 1, "text": "санхүүгийн мэдлэг"},  # a hashtag with no # and a space
        {"i": 2, "text": "#мөнгөний зөвлөгөө"},
        {"i": 3, "text": "Гурван зуршил"},
    ]})

    out, changed = _run(client, ideas)

    assert out.shorts[0].title == "Нууц"
    assert out.shorts[0].hashtags == ["#санхүүгийнмэдлэг", "#санхүү", "#мөнгөнийзөвлөгөө"]
    assert out.youtube[0].throughline == "Гурван зуршил"
    assert out.shorts[0].hook_quote == ideas.shorts[0].hook_quote
    assert changed == 4
    # A copy: the ideas the caller holds are what the model wrote.
    assert ideas.shorts[0].title == "The secret"


def test_an_answer_that_says_nothing_leaves_the_text_as_it_was():
    ideas = _ideas(title="The secret", caption="A simple example")
    client = FakeLlm({"texts": [
        {"i": 0, "text": "  "},  # empty
        {"i": 7, "text": "Хаанаас ч юм"},  # never asked
        {"i": True, "text": "Үнэн"},  # not an index
    ]})
    out, changed = _run(client, ideas)
    assert out.shorts[0].title == "The secret" and out.shorts[0].caption == "A simple example"
    assert changed == 0
