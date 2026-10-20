"""The validation veto table, one test per row"""

from __future__ import annotations

import ast
import inspect
import json

from jarvis.project_store import (
    VALIDATION_OPINION_STATUSES,
    VALIDATION_VERDICTS,
    VALIDATOR_SEATS,
)
from jarvis.validation import VETO_SEATS, arbitrate

#: Every verdict word the store accepts, plus the plausible wrong ones a model might write
VERDICTS = (*VALIDATION_VERDICTS, "passed", "rejected", "approve", "ok")

#: `ok | abstained | failed` are what a seat is actually recorded as
STATUSES = (*VALIDATION_OPINION_STATUSES, "pass")


def op(seat: str, status: str = "ok", **reply) -> dict:
    """One seat's opinion, in the shape `validation_opinions` stores it"""
    return {"seat": seat, "status": status, "reply": json.dumps(reply)}


def rejected(result) -> bool:
    return result is not None and result["outcome"] == "rejected"


# -- the two seats that can block


def test_security_raising_blocking_forces_a_rejection():
    result = arbitrate([op("security", verdict="reject", blocking=True,
                           reason="this logs the token")])

    assert rejected(result)
    assert "this logs the token" in result["reason"]


def test_tester_raising_blocking_forces_a_rejection():
    result = arbitrate([op("tester", verdict="reject", blocking=True,
                           reason="no test names the new branch")])

    assert rejected(result)
    assert "no test names the new branch" in result["reason"]


def test_the_asks_travel_with_the_reason():
    """The reason is delivered to the submitter verbatim"""
    result = arbitrate([op("security", blocking=True, reason="this leaks",
                           asks=["stop logging the token", "add a redaction test"])])

    assert "stop logging the token" in result["reason"]
    assert "add a redaction test" in result["reason"]


def test_both_veto_seats_blocking_prefers_securitys_words():
    """Only one message reaches the submitter"""
    result = arbitrate([op("tester", blocking=True, reason="the tester line"),
                        op("security", blocking=True, reason="the security line")])

    assert result["reason"].startswith("the security line")


def test_a_veto_seat_that_blocks_without_saying_why_still_rejects():
    """Silence about the reason is not silence about the objection"""
    result = arbitrate([op("security", blocking=True, reason="")])

    assert rejected(result)
    assert result["reason"].strip()


# -- the two seats that cannot, each paired with a seat that can


def test_architect_blocking_forces_nothing_and_the_same_reply_from_security_forces():
    """PAIRED IN ONE TEST, deliberately"""
    reply = {"verdict": "reject", "blocking": True, "reason": "the very same words",
             "asks": ["the very same ask"]}

    assert arbitrate([op("architect", **reply)]) is None
    assert rejected(arbitrate([op("security", **reply)]))


def test_maintainer_blocking_forces_nothing_and_the_same_reply_from_tester_forces():
    reply = {"verdict": "reject", "blocking": True, "reason": "the very same words",
             "asks": ["the very same ask"]}

    assert arbitrate([op("maintainer", **reply)]) is None
    assert rejected(arbitrate([op("tester", **reply)]))


def test_a_whole_panel_of_non_veto_objections_forces_nothing():
    """Their failure mode is a rejection loop that spends exactly the time this feature exists to save"""
    assert arbitrate([op("architect", blocking=True, reason="a"),
                      op("maintainer", blocking=True, reason="b"),
                      op("chair", blocking=True, reason="c")]) is None


# -- how `blocking` is read


def test_the_string_false_still_blocks():
    """Read with `bool()`, never `is True`"""
    assert rejected(arbitrate([op("security", blocking="false", reason="r")]))


def test_a_falsey_blocking_forces_nothing_but_the_seat_is_still_read():
    """The negative control for the row above: `bool()` is permissive, not indiscriminate"""
    assert arbitrate([op("security", blocking=False, reason="r")]) is None
    assert arbitrate([op("security", blocking=0, reason="r")]) is None
    assert rejected(arbitrate([op("security", blocking=1, reason="r")]))


def test_a_veto_seat_that_rejects_without_blocking_forces_nothing():
    """`verdict` is the seat's opinion and `blocking` is its veto"""
    assert arbitrate([op("security", verdict="reject", reason="r")]) is None
    assert rejected(arbitrate([op("security", verdict="reject", blocking=True,
                                  reason="r")]))


# -- silence


def test_an_abstaining_veto_seat_is_neither_veto_nor_consent():
    """Paired with the identical reply recorded `ok`"""
    reply = {"blocking": True, "reason": "the identical words"}

    assert arbitrate([op("security", "abstained", **reply)]) is None
    assert rejected(arbitrate([op("security", "ok", **reply)]))


def test_a_failed_veto_seat_is_silence_too():
    reply = {"blocking": True, "reason": "the identical words"}

    assert arbitrate([op("tester", "failed", **reply)]) is None
    assert rejected(arbitrate([op("tester", "ok", **reply)]))


def test_an_unrecognised_status_is_silence():
    """A status field confused with a verdict must not become a signal"""
    assert arbitrate([op("security", "reject", blocking=True, reason="r")]) is None


def test_output_that_will_not_parse_is_silence():
    assert arbitrate([{"seat": "security", "status": "ok",
                       "reply": "I think this is, on balance, blocking"}]) is None
    assert arbitrate([{"seat": "security", "status": "ok", "reply": ""}]) is None
    assert arbitrate([{"seat": "security", "status": "ok", "reply": "[1, 2, 3]"}]) is None


def test_no_opinions_at_all_forces_nothing():
    assert arbitrate([]) is None


# -- who is read at all


def test_a_seat_outside_the_table_forces_nothing():
    """Whatever it calls itself"""
    for name in ("", "reviewer", "SECURITY", "security ", "premise", "blast"):
        assert arbitrate([op(name, blocking=True, reason="r")]) is None


def test_the_chairs_own_reply_is_never_arbitrated():
    """The chair is not a fifth objector: it synthesises, and it runs only when this returned None"""
    assert arbitrate([op("chair", blocking=True, verdict="reject",
                         outcome="rejected", reason="r")]) is None


def test_the_veto_seats_are_exactly_security_and_tester():
    """The table, stated once"""
    assert VETO_SEATS == ("security", "tester")
    assert set(VETO_SEATS) < set(VALIDATOR_SEATS)


# -- a clean panel, and the negative control


def test_a_clean_panel_returns_none_and_one_security_reject_forces():
    """PAIRED"""
    clean = [op("tester", verdict="pass", blocking=False, reason="covered"),
             op("security", verdict="pass", blocking=False, reason="nothing exposed"),
             op("architect", verdict="pass", blocking=False, reason="fits"),
             op("maintainer", verdict="pass", blocking=False, reason="readable")]

    assert arbitrate(clean) is None

    blocked = [op("security", verdict="reject", blocking=True, reason="this leaks")
               if o["seat"] == "security" else o for o in clean]
    assert rejected(arbitrate(blocked))


# -- nothing can force a pass


def test_no_combination_of_verdict_and_status_ever_returns_a_pass():
    """The exhaustive sweep: every verdict word by every status across all five seats"""
    for seat in VALIDATOR_SEATS:
        for verdict in VERDICTS:
            for status in STATUSES:
                for blocking in (True, False, "false", "", None, 1, 0):
                    result = arbitrate([op(seat, status, verdict=verdict,
                                           blocking=blocking, reason="r",
                                           outcome="passed", pass_=True)])
                    assert result is None or result["outcome"] == "rejected"


def test_arbitrate_has_exactly_one_non_none_return_and_it_is_a_rejection():
    """STRUCTURAL, not behavioural"""
    tree = ast.parse(inspect.getsource(arbitrate))
    returns = [n for n in ast.walk(tree) if isinstance(n, ast.Return)]
    values = [n.value for n in returns
              if not (n.value is None
                      or (isinstance(n.value, ast.Constant) and n.value.value is None))]

    assert len(values) == 1, "more than one way out is more than one rule"
    node = values[0]
    assert isinstance(node, ast.Dict)
    outcomes = [v.value for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and k.value == "outcome"]
    assert outcomes == ["rejected"]
