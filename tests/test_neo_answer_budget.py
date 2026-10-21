"""Neo's answers are capped in length, because the user reads them"""

from __future__ import annotations

import pytest

from jarvis import neo as neo_mod
from jarvis.bootstrap import ASSETS
from jarvis.gates import REVIEWER_PERSONA

# The two panel seats that write something a human reads: the chair always
SPEAKING_SEATS = ("premise", "chair")


@pytest.fixture()
def persona() -> str:
    return neo_mod.PERSONA


def seat_mandate(seat: str) -> str:
    return (ASSETS / "neo-seats" / f"{seat}.md").read_text()


def test_agreeing_answers_get_one_line(persona: str) -> None:
    body = persona.lower()
    assert "one line" in body
    assert "agree" in body


def test_overriding_answers_get_fifty_words(persona: str) -> None:
    assert "50 WORDS" in persona
    assert "override" in persona.lower()


def test_the_budget_caps_explanation_not_the_decision(persona: str) -> None:
    """The failure mode worth guarding: a shorter answer that answers less"""
    assert "Cut the justification, never the answer." in persona


def test_the_worker_is_told_it_may_ask_again(persona: str) -> None:
    """The escape hatch is what makes the cap safe"""
    assert "ask again" in persona.lower()


def test_mandated_verbatim_wording_is_exempt(persona: str) -> None:
    """The user's constraint: the 50-word budget must never squeeze out wording a learning requires stated..."""
    assert "EXEMPT" in persona
    assert "VERBATIM" in persona
    assert "do not count it against" in persona


def test_escalation_reasons_are_one_line(persona: str) -> None:
    """Escalation reasons land in the user's inbox"""
    assert "`reason` is always one line" in persona


@pytest.mark.parametrize("seat", SPEAKING_SEATS)
def test_a_panel_seat_that_speaks_to_a_human_carries_the_same_budget(seat: str) -> None:
    """More agents must not mean more words"""
    body = seat_mandate(seat)

    assert "50 WORDS" in body
    assert "ONE LINE" in body.upper()
    assert "EXEMPT" in body and "VERBATIM" in body
    assert "Cut the justification, never the answer." in body


def test_gate_reviewer_is_untouched_by_the_budget() -> None:
    """Deliberate scope line: REVIEWER_PERSONA already demands one-line reasons and emits no prose answer"""
    assert "50 WORDS" not in REVIEWER_PERSONA
    shapes = [ln for ln in REVIEWER_PERSONA.splitlines() if '"reason":' in ln]
    assert shapes, "the reviewer's output contract has moved; this test needs rewriting"
    assert all("<one line" in ln for ln in shapes), shapes
