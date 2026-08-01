"""Neo's answers are capped in length, because the user reads them.

The cap lives in the persona, not in code: an over-long answer is a model miss, and
truncating it would cut mid-sentence exactly on the override case — the one carrying
the reasoning the worker needs. So what is testable here is that the instruction is
present and unambiguous; whether real answers obey it is measured by the opt-in LLM
eval in evals/llm/test_neo_judgment.py.
"""

from __future__ import annotations

import pytest

from jarvis import neo as neo_mod
from jarvis.gates import REVIEWER_PERSONA


@pytest.fixture()
def persona() -> str:
    return neo_mod.PERSONA


def test_agreeing_answers_get_one_line(persona: str) -> None:
    body = persona.lower()
    assert "one line" in body
    assert "agree" in body


def test_overriding_answers_get_fifty_words(persona: str) -> None:
    assert "50 WORDS" in persona
    assert "override" in persona.lower()


def test_the_budget_caps_explanation_not_the_decision(persona: str) -> None:
    """The failure mode worth guarding: a shorter answer that answers less.

    A budget read as "say less" would drop the second half of a two-part question,
    which costs the worker a whole round trip to recover — the opposite of the point.
    """
    assert "Cut the justification, never the answer." in persona


def test_the_worker_is_told_it_may_ask_again(persona: str) -> None:
    """The escape hatch is what makes the cap safe. Without it, a short answer that
    omitted something leaves the worker guessing instead of asking."""
    assert "ask again" in persona.lower()


def test_mandated_verbatim_wording_is_exempt(persona: str) -> None:
    """The user's constraint: the 50-word budget must never squeeze out wording a
    learning requires stated verbatim — the mandatory gate false-positive reason being
    the live example. Budgets that silently truncate a compliance phrase are worse than
    no budget."""
    assert "EXEMPT" in persona
    assert "VERBATIM" in persona
    assert "do not count it against" in persona


def test_escalation_reasons_are_one_line(persona: str) -> None:
    """Escalation reasons land in the user's inbox, so they are the one field where
    length costs the user directly."""
    assert "`reason` is always one line" in persona


def test_gate_reviewer_is_untouched_by_the_budget() -> None:
    """Deliberate scope line: REVIEWER_PERSONA already demands one-line reasons and
    emits no prose answer, so the budget has nothing to add there. This test exists so
    that a later 'apply it everywhere' change is a decision, not a drift.

    It counts nothing — the number of verdict shapes is the gate reviewer's business and
    has already changed once (the `dismiss` verdict). What it holds is that EVERY shape
    it emits asks for one line, and that none of them carries a word budget.
    """
    assert "50 WORDS" not in REVIEWER_PERSONA
    shapes = [ln for ln in REVIEWER_PERSONA.splitlines() if '"reason":' in ln]
    assert shapes, "the reviewer's output contract has moved; this test needs rewriting"
    assert all("<one line" in ln for ln in shapes), shapes
