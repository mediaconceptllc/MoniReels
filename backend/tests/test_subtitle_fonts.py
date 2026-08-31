"""Fonts that actually exist in the image.

libass resolves a family by NAME through fontconfig and, when it is missing,
substitutes another WITHOUT FAILING. So an operator picks a font, the export
uses a different one, and nothing anywhere says so. SubtitleStyle shipped
with a default of "Arial" — a font in no container this project has ever
built — for exactly that reason: nothing could tell.
"""
from __future__ import annotations

import pytest

from app.models import Segment, SubtitleStyle
from app.subtitle import fonts
from app.subtitle.ass import build_ass_document


@pytest.fixture(autouse=True)
def _fresh_cache():
    fonts.available.cache_clear()
    yield
    fonts.available.cache_clear()


def test_the_image_offers_at_least_one_cyrillic_family():
    # `fc-list :lang=mn` is the same question CI asks of the built image.
    assert len(fonts.available()) >= 1


def test_the_default_family_is_one_of_the_offered_ones(monkeypatch):
    # The bug this module exists to stop: a default nothing has installed.
    monkeypatch.setattr(fonts, "available", lambda: ("Inter", "DejaVu Sans"))
    assert SubtitleStyle().font_family in fonts.available()


def test_a_missing_family_falls_back_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(fonts, "available", lambda: ("Inter", "DejaVu Sans"))

    with caplog.at_level("WARNING"):
        resolved = fonts.resolve("Arial")

    assert resolved == "Inter"
    assert "Arial" in caplog.text  # the substitution libass would never mention


def test_an_installed_family_is_passed_through_untouched(monkeypatch):
    monkeypatch.setattr(fonts, "available", lambda: ("Inter", "DejaVu Sans"))
    assert fonts.resolve("DejaVu Sans") == "DejaVu Sans"


def test_fc_list_being_absent_is_not_fatal(monkeypatch):
    # A dev machine without fontconfig must still be able to run the app.
    def boom(*_a, **_k):
        raise FileNotFoundError("fc-list")

    monkeypatch.setattr(fonts.subprocess, "run", boom)
    assert fonts.available() == fonts.FALLBACK_FAMILIES


def test_a_failing_fc_list_is_not_fatal_either(monkeypatch):
    class _Result:
        returncode = 127
        stdout = ""

    monkeypatch.setattr(fonts.subprocess, "run", lambda *_a, **_k: _Result())
    assert fonts.available() == fonts.FALLBACK_FAMILIES


def test_only_the_first_alias_of_a_face_is_offered(monkeypatch):
    # fc-list prints "DejaVu Sans,DejaVu Sans Condensed"; the first name is
    # the one a human recognises and the one libass matches most reliably.
    class _Result:
        returncode = 0
        stdout = "DejaVu Sans,DejaVu Sans Condensed\nInter,Inter Display\n"

    monkeypatch.setattr(fonts.subprocess, "run", lambda *_a, **_k: _Result())
    assert fonts.available() == ("DejaVu Sans", "Inter")


def test_the_ass_document_carries_the_resolved_family(monkeypatch):
    # The whole chain: a stored font nothing installed must not reach libass.
    monkeypatch.setattr(fonts, "available", lambda: ("Inter",))
    style = SubtitleStyle(font_family="Arial")
    segments = [Segment(id="s", start=0.0, end=2.0, text="сайн уу")]

    document = build_ass_document(segments, style)

    assert "Style: Default,Inter," in document
    assert "Arial" not in document
