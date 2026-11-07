"""Owner-mode question text and how its answer is read (docs/superpowers/specs/2026-11-07-neo-owns-os-origin-assumption-review.md §3)"""

from __future__ import annotations

import pytest

from jarvis import autoreview
from tests.test_requestor import (GOLD_A, GOLD_S, GOLD_WO, GOLDEN_CONFIRM, GOLDEN_EARLY,
                                  GOLDEN_RULING)

SIB = [GOLD_A, GOLD_S]
OWNER_HEADING = "# YOU OWN THIS DECISION"


def ruling_q(**kw):
    return autoreview._ruling_question("p", GOLD_WO, GOLD_A, SIB, **kw)  # noqa: SLF001


def confirm_q(**kw):
    return autoreview._confirm_question("p", GOLD_WO, GOLD_A, SIB, **kw)  # noqa: SLF001


def test_owner_off_is_byte_identical_on_both_builders():
    """Goldens captured before §2 and §3 edited these builders"""
    assert ruling_q(owner=False) == GOLDEN_RULING
    assert ruling_q(early=True, owner=False) == GOLDEN_EARLY
    assert confirm_q(owner=False) == GOLDEN_CONFIRM


@pytest.mark.parametrize("build", [lambda: ruling_q(owner=True, requestor_label="schedule"),
                                   lambda: confirm_q(owner=True, requestor_label="schedule")],
                         ids=["ruling", "confirm"])
def test_owner_delivered_text_makes_a_deny_a_rework(build):
    out = build()
    assert OWNER_HEADING in out
    assert out.index("# Who asked for this work order") < out.index(OWNER_HEADING)
    assert "`deny` is a REWORK" in out
    assert "ONLY with `cause: conflicting-authority`" in out
    assert "sends it to the user" not in out and "send it to the user" not in out
    assert "`high` goes to the user whatever you rule" in out
    assert "restate the assumption self-contained" in out
    # §3.1: the owner block never repeats the label
    assert out.count("schedule") == 1


def test_owner_early_keeps_todays_answers_and_adds_only_the_block():
    out = ruling_q(early=True, owner=True)
    answers = GOLDEN_EARLY.rsplit("\n\n", 1)[1]
    assert out.endswith(answers)
    assert OWNER_HEADING in out and "your objection as owner" in out
    assert "REWORK" not in out


def test_no_owner_text_names_an_overridden_cause():
    from jarvis.neo_store import ESCALATION_CAUSES_OVERRIDDEN

    texts = [ruling_q(owner=True), ruling_q(owner=True, early=True), confirm_q(owner=True),
             autoreview.ASSUMPTION_REVIEWER_PERSONA]
    for text in texts:
        assert "rework-cap" not in text
        for cause in ESCALATION_CAUSES_OVERRIDDEN:
            assert cause not in text


def test_the_persona_grants_owner_mode_through_the_question():
    assert f"`{OWNER_HEADING}` block" in autoreview.ASSUMPTION_REVIEWER_PERSONA


def test_shared_constants():
    assert autoreview.MAX_REWORKS == 2
    assert autoreview.OWNER_ESCALATION_CAUSES == ("conflicting-authority",)


# -- reading the answer (§3.2)


def reply(**over) -> dict:
    return {"escalate": False, "verdict": "approved", "stakes": "routine",
            "reason": "rename it", "model": "m", "cause": "", **over}


DENY = {"verdict": "denied"}


@pytest.mark.parametrize("verdict, accept, rework, overridden", [
    pytest.param(reply(), True, False, False, id="routine-approve"),
    pytest.param(reply(stakes="high"), False, False, True, id="approve-high-net"),
    pytest.param(reply(stakes=""), False, False, True, id="approve-unclassified-net"),
    pytest.param(reply(escalate=True, cause="conflicting-authority", **DENY),
                 False, False, False, id="escalate-conflicting-authority"),
    pytest.param(reply(**DENY), False, True, False, id="deny"),
    pytest.param(reply(escalate=True, cause="no-learning-applies", **DENY),
                 False, True, False, id="escalate-other-cause"),
    pytest.param(reply(escalate=True, **DENY), False, True, False, id="escalate-no-cause"),
    pytest.param(reply(refine=True, **DENY), False, True, False, id="refine"),
    # Neo Q3768: the high-stakes net is not owner-overridable on a non-accept either
    pytest.param(reply(stakes="high", **DENY), False, False, False, id="deny-high"),
    pytest.param(reply(escalate=True, stakes="high", cause="high-stakes", **DENY),
                 False, False, False, id="escalate-high"),
    pytest.param(reply(escalate=True, stakes="", **DENY), False, False, False,
                 id="escalate-unclassified"),
    # kn-40db1828: a failed call is never read as a rework
    pytest.param({"escalate": True, "answer": "", "reason": "neo call failed: boom",
                  "failed": True}, False, False, False, id="failed"),
    pytest.param(reply(escalate=True, failed=True, **DENY), False, False, False,
                 id="failed-routine"),
])
def test_owner_read_ruling_table(verdict, accept, rework, overridden):
    r = autoreview.read_ruling(verdict, owner=True)
    assert (r.accept, r.rework, r.overridden) == (accept, rework, overridden)
    if rework:
        assert r.reason == "rename it"
        assert autoreview.escalation_cause(r) == "neo-denied"


@pytest.mark.parametrize("verdict", [
    reply(), reply(stakes="high"), reply(**DENY), reply(escalate=True, **DENY),
    reply(escalate=True, cause="conflicting-authority", **DENY), {}])
def test_owner_off_read_ruling_is_unchanged_and_never_a_rework(verdict):
    off = autoreview.read_ruling(verdict)
    assert off == autoreview.read_ruling(verdict, owner=False)
    assert not off.rework
