"""The mechanical gate a fix order passes before the validation panel is paid.

§7.2 of docs/superpowers/specs/2026-09-27-self-evolution.md. `rules.precheck` is PURE —
the rows, the order's row and the triggering order's facts all arrive as arguments — so
every outcome below is a unit test with no store, no clock and no network.

WHAT IS BEING PROTECTED HERE. The refusal's words are the feature's only forcing
function: the pre-check never registers a detector or a remedy for the submitter (§7.2,
"it refuses, it does not create"), so a refusal that does not say WHICH thing is missing
and HOW to supply it is the dead end kn-85265170 is about. Each test therefore asserts
the sentence's load-bearing nouns, not merely that something was refused.

THE TWO STATES THAT ARE DELIBERATELY NOT ONE: `trigger_facts=None` with nothing named is
a REFUSAL (a detector that has never been shown to fire is unproven), and a trigger order
that was named and could not be READ is a PASS carrying `trigger_unreadable`. Collapsing
them would either refuse an order for the OS's own lost record or let an unproven rule
through. Both are asserted below.
"""

from __future__ import annotations

import json

from jarvis import rules

MATCHING = {"id": "dt-match", "gap_class": "stale-hold", "condition": json.dumps(
    {"field": "status", "op": "eq", "value": "running"})}
UNPARSEABLE = {"id": "dt-broken", "gap_class": "stale-hold", "condition": json.dumps(
    {"field": "field_a_later_release_removed", "op": "eq", "value": "x"})}

FIX_ORDER = {"id": "wo-fix", "gap_class": "stale-hold", "detector_id": "dt-match",
             "remedy_rule_id": "", "trigger_wo_id": "wo-trigger"}


def facts(**values):
    """The TRIGGERING order's recorded facts. Only what is passed is recorded."""
    return rules.Facts(project="acme", order_id="wo-trigger", order_kind="worker",
                       values=values, now=1000.0)


def rows(**kwargs):
    """One live remedy row under `dt-match`, overridable field by field."""
    return {"dt-match": [{"id": "rr-1", "primitive": "nudge", "params": {},
                          "argument": "ask it", **kwargs}]}


# -- the ordinary order, which is almost every order ------------------------------------


def test_an_empty_gap_class_is_not_pre_checked_at_all():
    """§7.1: an empty `gap_class` is the ordinary case and skips every gate."""
    assert rules.precheck([], {}, {"id": "wo-1", "gap_class": ""}, None) is None
    assert rules.precheck([], {}, {"id": "wo-1"}, None) is None


# -- the five outcomes ------------------------------------------------------------------


def test_a_gap_class_with_no_live_detector_is_refused_by_name():
    out = rules.precheck([], rows(), FIX_ORDER, facts(status="running"))
    assert out and "stale-hold" in out and "detector" in out
    assert "--detector-id" in out            # kn-85265170: say HOW, not just "name it"


def test_a_retired_detector_does_not_count_as_a_live_one():
    retired = [{**MATCHING, "status": "retracted"},
               {**MATCHING, "id": "dt-2", "retired_at": 12.0}]
    out = rules.precheck(retired, rows(), FIX_ORDER, facts(status="running"))
    assert out and "stale-hold" in out


def test_a_detector_with_no_live_remedy_row_is_refused():
    out = rules.precheck([MATCHING], {"dt-match": [
        {"id": "rr-1", "primitive": "nudge", "retired_at": 12.0}]},
        FIX_ORDER, facts(status="running"))
    assert out and "dt-match" in out and "remedy" in out


def test_a_remedy_naming_a_primitive_that_does_not_exist_is_refused_by_primitive():
    out = rules.precheck([MATCHING], rows(primitive="rm_minus_rf"), FIX_ORDER,
                         facts(status="running"))
    assert out and "rm_minus_rf" in out and "nudge" in out   # the closed registry, named


def test_only_the_named_remedy_rule_counts_when_the_order_names_one():
    wo = {**FIX_ORDER, "remedy_rule_id": "rr-9"}
    out = rules.precheck([MATCHING], rows(), wo, facts(status="running"))
    assert out and "rr-9" in out


def test_a_condition_that_does_not_match_is_refused_with_the_leaf_that_failed():
    """A REAL EVALUATION, never a syntax check — and `explain`'s absence clause is the
    part that tells a rule author their rule is aimed at the wrong orders."""
    out = rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="pending"))
    assert out and "dt-match" in out and "wo-trigger" in out
    assert "did not match because" in out and "status" in out

    absent = rules.precheck([MATCHING], rows(), FIX_ORDER, facts())
    assert absent and "`status` is not recorded on this order" in absent


def test_a_stored_condition_this_release_cannot_parse_is_refused_and_says_so():
    out = rules.precheck([UNPARSEABLE], {"dt-broken": [
        {"id": "rr-1", "primitive": "nudge"}]}, {**FIX_ORDER, "detector_id": "dt-broken"},
        facts(status="running"))
    assert out and "dt-broken" in out and "parse" in out


def test_a_precondition_sentence_is_the_refusal_verbatim():
    def refuses(primitive, params):
        return "the pull request has no head commit recorded, so nothing can be compared"

    out = rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                         precondition=refuses)
    assert out == ("the pull request has no head commit recorded, so nothing can be "
                   "compared")


def test_a_live_detector_a_live_remedy_and_a_matching_condition_pass():
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                          precondition=lambda primitive, params: "") is None


def test_an_undeterminable_precondition_is_not_a_refusal():
    """kn-b7deec8f: `can_apply` with nothing able to answer is a THIRD state. It is not
    collapsed into "yes" — the pass is on the other three checks and this one is left
    unanswered for the packet to state — and it is never a refusal."""
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running")) is None
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                          precondition=lambda primitive, params: None) is None


# -- the triggering order, and the two states that are not one --------------------------


def test_no_triggering_order_at_all_is_refused_in_the_specs_own_words():
    out = rules.precheck([MATCHING], rows(), {**FIX_ORDER, "trigger_wo_id": ""}, None)
    assert out == (
        "gap_class 'stale-hold' has no triggering order to prove its detector against: "
        "trigger_wo_id is empty and the improvement-order refs resolve to no work order "
        "in this project. Re-file with `jarvis wo create --trigger-wo-id <wo-id>`, "
        "naming the order the investigation was filed from.")


def test_a_trigger_order_that_could_not_be_read_lets_the_submission_through():
    """Named but unreadable — deleted, or in another project. The pinned ruling: never
    fabricate an answer from a failure. UNREADABLE is recorded and the panel judges."""
    assert rules.precheck(
        [MATCHING], rows(), FIX_ORDER, None,
        trigger_unreadable="wo-trigger is not in this project's database") is None


def test_an_unreadable_trigger_does_not_excuse_a_missing_detector():
    """The pass-through is about the CONDITION, which cannot be evaluated. Existence is
    still decided here — §7.2's step 1 needs no facts at all."""
    out = rules.precheck([], rows(), FIX_ORDER, None,
                         trigger_unreadable="deleted")
    assert out and "stale-hold" in out


# -- purity -----------------------------------------------------------------------------


def test_precheck_reaches_no_store(monkeypatch):
    """rules.py is a LEAF (§3): stdlib plus db, catalog, remedies. A pre-check that
    opened a store would make the gate untestable in one step and would drag a database
    into `Daemon._validate_work_order`'s hot band."""
    import jarvis.rules as module

    assert "central_store" not in module.__dict__
    assert "ProjectStore" not in module.__dict__
