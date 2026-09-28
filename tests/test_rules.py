"""The self-evolution registry's grammar, its pure evaluator, and the seed rows.

Three properties are load-bearing here and each has its own block below.

1. **Every problem at once.** `parse_condition` is written for a person pasting a
   condition into `jarvis rules` and for a model proposing one from an improvement
   order. Both re-submit per error message, so a parser that stops at the first problem
   costs a round trip per line it could have reported together.
2. **Absent is not false.** A leaf whose field the order does not record is FALSE for
   every operator except `absent`, and the evaluator says WHICH field was missing. A
   condition that matches only because a field is missing is the commonest way a rule
   over-fires (spec §3.2), and these tests are where that stays caught.
3. **The grammar's acceptance criterion**: all five seed conditions of §3.5 are
   expressible without extending it.

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT, so nobody reads it as forgotten: that the
seed rules' primitives exist in `remedies.REMEDIES`, that their parameters satisfy a
primitive's schema, or that `rules.resolve` returns a `Resolution` for any of the five.
Six of those primitives (`update_branch`, `carry_verdict`, `lower_attention`,
`drop_hold`, `retry_neo_question`, `raise_attention`) land with §4, which this section
has no dependency on — the two are built in parallel on purpose. **§5.3 owns all three
of those assertions and the firing proof**, because it is the one section that depends
on both. (Neo, question 878.)
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from jarvis import automerge, invariants, probes, remedies, rules


def facts(**values):
    """One order's recorded facts. Only what is passed is RECORDED — that is the point."""
    return rules.Facts(project="acme", order_id="wo-1", order_kind="worker",
                       values=values, now=1000.0)


# -- the grammar ----------------------------------------------------------------------


@pytest.mark.parametrize("node", [
    {"field": "status", "op": "eq", "value": "running"},
    {"field": "status", "op": "ne", "value": "running"},
    {"field": "status", "op": "in", "value": ["running", "pending"]},
    {"field": "status", "op": "not_in", "value": ["running"]},
    {"field": "seconds_in_status", "op": "gte", "value": 60},
    {"field": "seconds_in_status", "op": "lte", "value": 60},
    {"field": "hold_cause", "op": "exists"},
    {"field": "hold_cause", "op": "absent"},
    {"field": "event_counts", "op": "count_gte", "value": {"kind": "turn", "value": 2}},
])
def test_parse_condition_accepts_every_operator(node):
    assert rules.parse_condition(node) == rules.parse_condition(json.dumps(node))


def test_parse_condition_accepts_every_combinator():
    leaf = {"field": "status", "op": "eq", "value": "running"}
    for doc in ({"all": [leaf]}, {"any": [leaf, leaf]}, {"not": leaf}):
        assert rules.parse_condition(doc)


def test_parse_condition_reports_every_problem_at_once():
    """One document, three independent faults, one exception naming all three."""
    doc = {"all": [
        {"field": "no_such_field", "op": "eq", "value": "x"},
        {"field": "status", "op": "matches", "value": "x"},
        {"field": "status", "op": "gte", "value": 3},
    ]}
    with pytest.raises(rules.RulesError) as e:
        rules.parse_condition(doc)
    assert len(e.value.problems) == 3
    text = str(e.value)
    assert "all[0]" in text and "all[1]" in text and "all[2]" in text


@pytest.mark.parametrize("doc, needle", [
    ({"field": "no_such_field", "op": "eq", "value": "x"}, "no_such_field"),
    ({"field": "status", "op": "matches", "value": "x"}, "matches"),
    ({"field": "status", "op": "gte", "value": 3}, "gte"),
    ({"field": "status", "op": "count_gte", "value": {"kind": "a", "value": 1}},
     "`count_gte` cannot be applied to `status`"),
    ({"all": []}, "empty"),
    ({"field": "status", "op": "in", "value": "running"}, "list"),
    ({"field": "status", "op": "eq", "value": 3}, "str"),
    ({"all": [{"any": [{"all": [{"any": [
        {"field": "status", "op": "eq", "value": "x"}]}]}]}]}, "deeper"),
    ({"all": [{"field": "status", "op": "eq", "value": str(i)} for i in range(33)]},
     "nodes"),
    ("{not json", "JSON"),
    ([{"field": "status", "op": "eq", "value": "x"}], "object"),
])
def test_parse_condition_refuses(doc, needle):
    with pytest.raises(rules.RulesError) as e:
        rules.parse_condition(doc)
    assert needle in str(e.value)


def test_sources_and_fields_used():
    cond = rules.parse_condition({"all": [
        {"field": "status", "op": "eq", "value": "running"},
        {"not": {"field": "hold_cause", "op": "exists"}},
    ]})
    assert rules.fields_used(cond) == frozenset({"status", "hold_cause"})
    assert rules.sources_used(cond) == frozenset({"work_order", "holds"})


# -- absent is not false --------------------------------------------------------------


@pytest.mark.parametrize("op, value, expected", [
    ("eq", "panel_gave_up", False),
    ("ne", "panel_gave_up", False),
    ("in", ["panel_gave_up"], False),
    ("not_in", ["panel_gave_up"], False),
    ("exists", None, False),
    ("absent", None, True),
])
def test_an_unrecorded_field_is_false_for_everything_but_absent(op, value, expected):
    node = {"field": "hold_cause", "op": op}
    if value is not None:
        node["value"] = value
    cond = rules.parse_condition(node)
    assert rules.matches(cond, facts(status="running")) is expected


def test_an_unrecorded_numeric_field_is_false_for_gte_and_lte():
    for op in ("gte", "lte"):
        cond = rules.parse_condition({"field": "hold_seconds", "op": op, "value": 1})
        assert rules.matches(cond, facts()) is False


def test_evaluation_names_the_absent_fields_and_explain_says_so():
    cond = rules.parse_condition({"all": [
        {"field": "status", "op": "eq", "value": "running"},
        {"field": "hold_cause", "op": "eq", "value": "panel_gave_up"},
    ]})
    ev = rules.evaluate(cond, facts(status="running"))
    assert ev.matched is False
    assert ev.absent == ("hold_cause",)
    assert "`hold_cause` is not recorded on this order" in rules.explain(cond, ev)
    assert [leaf.present for leaf in ev.leaves] == [True, False]


# -- combinators, nesting, purity ------------------------------------------------------


def test_explain_a_recorded_field_that_simply_did_not_match():
    """The other arm: the field IS recorded and the comparison failed. It must NOT
    borrow the absence wording — that would blame a missing fact for a real mismatch."""
    cond = rules.parse_condition({"field": "status", "op": "eq", "value": "needs_review"})
    ev = rules.evaluate(cond, facts(status="running"))
    said = rules.explain(cond, ev)
    assert "not recorded" not in said
    assert "`status`" in said and "needs_review" in said
    assert "matched: " in rules.explain(cond, rules.evaluate(
        cond, facts(status="needs_review")))


def test_not_and_nesting():
    cond = rules.parse_condition({"all": [
        {"field": "status", "op": "eq", "value": "waiting_pr_merge"},
        {"not": {"any": [
            {"field": "automerge_code", "op": "eq", "value": "base_red"},
            {"field": "hold_cause", "op": "exists"},
        ]}},
    ]})
    assert rules.matches(cond, facts(status="waiting_pr_merge",
                                     automerge_code="sha_moved")) is True
    assert rules.matches(cond, facts(status="waiting_pr_merge",
                                     automerge_code="base_red")) is False


def test_matches_is_pure(tmp_path):
    cond = rules.parse_condition({"field": "seconds_in_status", "op": "gte",
                                  "value": 3600})
    f = facts(seconds_in_status=7200)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert rules.matches(cond, f) is rules.matches(cond, f) is True
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert rules.evaluate(cond, f) == rules.evaluate(cond, f)


def test_render_condition_reads_as_prose():
    cond = rules.parse_condition({"all": [
        {"field": "status", "op": "eq", "value": "needs_review"},
        {"field": "seconds_in_status", "op": "gte", "value": 3600},
    ]})
    rendered = rules.render_condition(cond)
    assert "`status`" in rendered and "3600" in rendered


def test_facts_is_declared_here_and_owned_by_the_evaluation_pass():
    with pytest.raises(NotImplementedError) as e:
        rules.facts(object(), {"id": "wo-1"}, now=0.0)
    assert "evaluation" in str(e.value)


# -- resolve ---------------------------------------------------------------------------


MATCHING = {"id": "dt-match", "condition": json.dumps(
    {"field": "status", "op": "eq", "value": "running"})}
OTHER = {"id": "dt-other", "condition": json.dumps(
    {"field": "status", "op": "eq", "value": "pending"})}
BROKEN = {"id": "dt-broken", "condition": json.dumps(
    {"field": "field_a_later_release_removed", "op": "eq", "value": "x"})}


def test_resolve_yields_one_resolution_per_live_remedy_row():
    out = rules.resolve([MATCHING, OTHER], {"dt-match": [
        {"id": "rm-1", "primitive": "nudge", "params": {}, "argument": "ask it"},
        {"id": "rm-2", "primitive": "unblock", "params": {}, "argument": "cut them"},
    ], "dt-other": [
        {"id": "rm-3", "primitive": "nudge", "params": {}, "argument": "never"},
    ]}, facts(status="running"))
    assert [r.primitive for r in out] == ["nudge", "unblock"]
    assert out[0].argument == "ask it"
    assert out[0].detector["id"] == "dt-match"


def test_resolve_skips_a_retired_remedy_row():
    out = rules.resolve([MATCHING], {"dt-match": [
        {"id": "rm-1", "primitive": "nudge", "params": {}, "retired_at": 12.0},
        {"id": "rm-2", "primitive": "unblock", "params": {}, "status": "retracted"},
        {"id": "rm-3", "primitive": "file_work_order", "params": {}},
    ]}, facts(status="running"))
    assert [r.primitive for r in out] == ["file_work_order"]


def test_an_unparseable_stored_condition_is_skipped_and_reported():
    readable, unreadable = rules.readable_detectors([MATCHING, BROKEN])
    assert [row["id"] for row, _cond in readable] == ["dt-match"]
    assert [det_id for det_id, _err in unreadable] == ["dt-broken"]
    assert isinstance(unreadable[0][1], rules.RulesError)
    out = rules.resolve([MATCHING, BROKEN], {"dt-match": [
        {"id": "rm-1", "primitive": "nudge", "params": {}}]}, facts(status="running"))
    assert [r.detector["id"] for r in out] == ["dt-match"]


def test_can_apply_is_none_without_a_precondition_and_the_sentence_with_one():
    rows = {"dt-match": [{"id": "rm-1", "primitive": "nudge", "params": {}}]}
    assert rules.resolve([MATCHING], rows, facts(status="running"))[0].can_apply is None
    out = rules.resolve([MATCHING], rows, facts(status="running"),
                        precondition=lambda primitive, params: "no session is running")
    assert out[0].can_apply == "no session is running"


def test_resolve_accepts_remedy_rows_as_a_flat_iterable():
    out = rules.resolve([MATCHING], [
        {"id": "rm-1", "detector_id": "dt-match", "primitive": "nudge", "params": {}}],
        facts(status="running"))
    assert [r.primitive for r in out] == ["nudge"]


# -- parameter validation ---------------------------------------------------------------


def test_validate_params_refuses_a_primitive_that_is_not_in_the_registry():
    problems = rules.validate_params("rm_minus_rf", {})
    assert problems and "rm_minus_rf" in problems[0]


def test_validate_params_accepts_a_shipped_primitive_while_no_schema_exists():
    assert getattr(remedies.REMEDIES["nudge"], "params", None) is None
    assert rules.validate_params("nudge", {}) == []


def test_validate_params_reads_a_tuple_of_param_objects_as_a_schema():
    """The primitives section spells `Remedy.params` as `tuple[Param, ...]`, not a dict.

    That section is on a parallel branch and its exact spelling is not settled, so the
    schema branch accepts either shape. A schema check that raises `AttributeError` the
    day the other shape lands is worse than one that adapts.
    """

    class _Param:
        def __init__(self, name, type_, required):
            self.name, self.type, self.required = name, type_, required

    class _Stub:
        params = (_Param("wo_id", "str", True), _Param("count", "num", False))

    with mock.patch.dict(remedies.REMEDIES, {"stub": _Stub()}, clear=False):
        assert rules.validate_params("stub", {"wo_id": "wo-1"}) == []
        problems = rules.validate_params("stub", {"count": "seven", "nope": 1})
        assert any("requires the 'wo_id' parameter" in p for p in problems)
        assert any("'count' must be num" in p for p in problems)
        assert any("unknown parameter 'nope'" in p for p in problems)


# -- the seed rows -----------------------------------------------------------------------


def test_seed_rows_are_five_builtin_fleet_wide_detectors_in_dry_run():
    rows = rules.seed_rows()
    assert len(rows) == 5
    for entry in rows:
        det = entry["detector"]
        assert det["status"] == rules.DRY_RUN
        assert det["source"] == "builtin"
        assert det["project"] == ""
        assert det["subjects"] == rules.DEFAULT_SUBJECT
        assert det["seed_version"] == rules.SEED_VERSION
        assert probes.ID_PATTERN.match(det["gap_class"]), det["gap_class"]
        assert det["summary"] and det["issue_url"]
        assert entry["remedies"]
        for rem in entry["remedies"]:
            assert rem["status"] == rules.DRY_RUN
            assert rem["detector_id"] == det["id"]
            assert rem["argument"]


def test_every_seed_condition_parses():
    """§3.2's acceptance criterion: all five seed conditions are expressible in the
    grammar without extending it. Checked here because this is where the grammar is."""
    for entry in rules.seed_rows():
        assert rules.parse_condition(entry["detector"]["condition"])


def test_seed_ids_are_stable_across_calls():
    first = [e["detector"]["id"] for e in rules.seed_rows()]
    second = [e["detector"]["id"] for e in rules.seed_rows()]
    assert first == second and len(set(first)) == 5
    assert all(i.startswith("dt-") for i in first)


def test_the_five_gap_classes_are_the_ones_the_spec_names():
    assert [e["detector"]["gap_class"] for e in rules.seed_rows()] == [
        "stale-panel-hold", "unreachable-neo-question", "catch-up-round-burn",
        "red-base-inherited", "overtaken-release-order"]


# -- the drift pins ------------------------------------------------------------------------


def test_the_inlined_blocker_sentence_tracks_invariants():
    """`rules` is a LEAF and may not import `invariants`, so the literal is inlined
    there. This is the pin that keeps the copy honest — the TEST may import both."""
    assert rules.VALIDATION_STUCK_BLOCKER == invariants.VALIDATION_STUCK_BLOCKER
    seed = json.loads(rules.seed_rows()[0]["detector"]["condition"])
    reasons = [n["value"] for n in seed["all"] if n.get("field") == "attention_reason"]
    assert reasons == [invariants.VALIDATION_STUCK_BLOCKER]


def test_the_quoted_automerge_codes_are_the_real_ones():
    codes = [json.loads(e["detector"]["condition"]) for e in rules.seed_rows()]
    quoted = {n["value"] for doc in codes for n in doc["all"]
              if n.get("field") == "automerge_code"}
    assert quoted == {automerge.HELD_SHA_MOVED, automerge.HELD_BASE_RED}


def test_bound_records_the_cap_it_applied():
    assert rules.bound("short") == "short"
    long = "x" * (rules.FACTS_CHARS + 50)
    out = rules.bound(long)
    assert out.startswith("x" * rules.FACTS_CHARS)
    assert str(rules.FACTS_CHARS) in out[rules.FACTS_CHARS:]
