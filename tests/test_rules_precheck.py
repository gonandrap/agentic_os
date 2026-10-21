"""The mechanical gate a fix order passes before the validation panel is paid (docs/superpowers/specs/2026-09-27-self-evolution.md)"""

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
    """The TRIGGERING order's recorded facts"""
    return rules.Facts(project="acme", order_id="wo-trigger", order_kind="worker",
                       values=values, now=1000.0)


def rows(**kwargs):
    """One live remedy row under `dt-match`, overridable field by field"""
    return {"dt-match": [{"id": "rr-1", "primitive": "nudge", "params": {},
                          "argument": "ask it", **kwargs}]}


# -- the ordinary order, which is almost every order


def test_an_empty_gap_class_is_not_pre_checked_at_all():
    """§7.1: an empty `gap_class` is the ordinary case and skips every gate"""
    assert rules.precheck([], {}, {"id": "wo-1", "gap_class": ""}, None) is None
    assert rules.precheck([], {}, {"id": "wo-1"}, None) is None


# -- the five outcomes


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
    """A REAL EVALUATION, never a syntax check"""
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
    """kn-b7deec8f: `can_apply` with nothing able to answer is a THIRD state"""
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running")) is None
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                          precondition=lambda primitive, params: None) is None


# -- the triggering order, and the two states that are not one


def test_no_triggering_order_at_all_is_refused_in_the_specs_own_words():
    out = rules.precheck([MATCHING], rows(), {**FIX_ORDER, "trigger_wo_id": ""}, None)
    assert out == (
        "gap_class 'stale-hold' has no triggering order to prove its detector against: "
        "trigger_wo_id is empty and the improvement-order refs resolve to no work order "
        "in this project. Re-file with `jarvis wo create --trigger-wo-id <wo-id>`, "
        "naming the order the investigation was filed from.")


def test_a_trigger_order_that_could_not_be_read_lets_the_submission_through():
    """Named but unreadable — deleted, or in another project"""
    assert rules.precheck(
        [MATCHING], rows(), FIX_ORDER, None,
        trigger_unreadable="wo-trigger is not in this project's database") is None


def test_an_unreadable_trigger_does_not_excuse_a_missing_detector():
    """The pass-through is about the CONDITION, which cannot be evaluated"""
    out = rules.precheck([], rows(), FIX_ORDER, None,
                         trigger_unreadable="deleted")
    assert out and "stale-hold" in out


# -- a NAMED trigger the records do not have: the submitter's own input


def test_a_named_trigger_the_records_do_not_have_is_refused_in_the_constants_own_words():
    """The round-2 defect: a made-up `--trigger-wo-id` used to pass checks 3 and 4 unevaluated"""
    out = rules.precheck([MATCHING], rows(), FIX_ORDER, None,
                         trigger_missing="wo-does-not-exist")
    assert out == (
        "gap_class 'stale-hold' names triggering order 'wo-does-not-exist' through "
        "`--trigger-wo-id`, and this project's records do not have it. That id is the "
        "submitter's own input, not a record the OS lost, so this is a REFUSAL and not "
        "an unreadable record: nothing has shown the detector fires, because there is no "
        "order to fire it against. Re-file with `jarvis wo create --trigger-wo-id "
        "<wo-id>`, naming an order this project has.")


def test_a_missing_detector_still_beats_a_missing_trigger():
    """Deepest-stage precedence is unchanged: check 1 refuses before check 3 is reached"""
    out = rules.precheck([], rows(), FIX_ORDER, None,
                         trigger_missing="wo-does-not-exist")
    assert out and "stale-hold" in out and "--detector-id" in out
    assert "wo-does-not-exist" not in out


def test_a_missing_trigger_leaves_no_unproven_note():
    """A candidate that REFUSES may not leave a note: the notes describe checks that passed unevaluated"""
    notes: list[str] = []
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, None,
                          trigger_missing="wo-gone", unproven=notes) is not None
    assert notes == []


# -- deepest-stage precedence (recorded assumption 2)


def test_the_deepest_refusal_wins_when_detectors_fail_at_different_stages():
    no_remedy = {**MATCHING, "id": "dt-no-remedy"}
    out = rules.precheck([no_remedy, MATCHING], rows(), FIX_ORDER,
                         facts(status="pending"))
    assert out and "dt-match" in out and "did not match because" in out
    assert "dt-no-remedy" not in out, "a stage-3 refusal beat a stage-5 one"


def test_an_unparseable_detector_loses_to_a_specific_no_match():
    out = rules.precheck([UNPARSEABLE, MATCHING],
                         {**rows(), "dt-broken": [{"id": "rr-2", "primitive": "nudge"}]},
                         FIX_ORDER, facts(status="pending"))
    assert out and "dt-match" in out and "did not match because" in out
    assert "dt-broken" not in out


# -- the notes: a check that PASSED UNEVALUATED is recorded


def test_an_unreadable_trigger_leaves_a_note_naming_check_3():
    notes: list[str] = []
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, None,
                          trigger_unreadable="its recorded state could not be read",
                          unproven=notes) is None
    [note] = notes
    assert note.startswith("check 3")
    assert "dt-match" in note and "its recorded state could not be read" in note


def test_an_undeterminable_precondition_leaves_a_note_naming_check_4():
    """(iv) no precondition injected at all, and (iii) the primitive declares none"""
    notes: list[str] = []
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                          unproven=notes) is None
    [note] = notes
    assert note.startswith("check 4") and "nudge" in note

    declares_none: list[str] = []
    import dataclasses

    from jarvis import remedies
    patched = dict(remedies.REMEDIES)
    patched["nudge"] = dataclasses.replace(patched["nudge"], can_apply=None)
    original, remedies.REMEDIES = remedies.REMEDIES, patched
    try:
        assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                              precondition=lambda primitive, params: "",
                              unproven=declares_none) is None
    finally:
        remedies.REMEDIES = original
    [note] = declares_none
    assert note.startswith("check 4") and "can_apply" in note


def test_a_passing_precondition_leaves_no_note():
    notes: list[str] = []
    assert rules.precheck([MATCHING], rows(), FIX_ORDER, facts(status="running"),
                          precondition=lambda primitive, params: "",
                          unproven=notes) is None
    assert notes == []


# -- purity


def test_precheck_reaches_no_store(monkeypatch):
    """rules.py is a LEAF (§3): stdlib plus db, catalog, remedies"""
    import jarvis.rules as module

    assert "central_store" not in module.__dict__
    assert "ProjectStore" not in module.__dict__
