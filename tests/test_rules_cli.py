"""`ops.rules_*` and the `jarvis rules` CLI family.

The two properties these tests exist to pin:

1. **Absent is never zero.** An empty registry returns a SENTENCE, not a table of zeros,
   and a detector that has never fired has no hit rate rather than a hit rate of 0.0. A
   fabricated zero reads as a measurement.
2. **A thing that could not be read decides nothing.** `rules_dry_run` on a detector
   whose stored condition no longer parses reports it UNREADABLE and evaluates nothing;
   with an order id and no fact snapshot to build it says so and returns no verdict. It
   never reports "no match", which would be an answer nobody computed.

The CLI derives no number: it prints what `ops` returned.

Spec: docs/superpowers/specs/2026-09-27-self-evolution.md §3.4.
"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops, rules
from jarvis.central_store import CentralStore

COND = {"all": [{"field": "status", "op": "eq", "value": "needs_review"},
                {"field": "seconds_in_status", "op": "gte", "value": 3600}]}


@pytest.fixture()
def store(jarvis_home):
    s = CentralStore()
    # THE FIVE BUILTIN SEED ROWS ARE CLEARED. `CentralStore` seeds them on every open
    # (docs/superpowers/specs/2026-09-27-self-evolution.md §5.3), and this file is about
    # the TABLE MECHANICS — a row round-tripping, an insert being refused, a retraction never
    # deleting — every assertion of which is written as "and the table stays empty". The
    # seeds themselves are proved in `tests/test_rules_tick.py`, which is the section that
    # owns their call site; asserting them here too would be two files disagreeing about
    # what a count means the first time a sixth rule is seeded.
    s.conn.execute("DELETE FROM remedy_rules")
    s.conn.execute("DELETE FROM detectors")
    s.conn.commit()          # another connection (the CLI opens its own) must see it
    yield s
    s.close()


@pytest.fixture()
def registered(store):
    det = store.add_detector("stale-panel-hold", COND, project="proj_a",
                             summary="a panel hold nobody clears", source="io",
                             io_id="io-1", fix_wo_id="wo-9",
                             issue_url="https://example/786",
                             pr_url="https://example/pr/1", seed_version=1)
    rule = store.add_remedy_rule(det["id"], "nudge", params={},
                                 argument="ask the session where it is")
    return det, rule


# -- rules_list -------------------------------------------------------------------------


def test_an_empty_registry_returns_the_sentence_and_no_fabricated_zero(store):
    data = ops.rules_list()
    assert data["rules"] == []
    assert data["counts"]["total"] == 0
    assert isinstance(data["note"], str) and data["note"]
    assert "0.0" not in data["note"] and "0%" not in data["note"]
    # `catalog.RulesConfig` ships OFF, and with no catalog registered at all the answer
    # is the same False — never None, and never True on a failure to read a file.
    assert data["enabled"] is False


def test_a_detector_that_never_fired_has_no_hit_rate(store, registered):
    det, rule = registered
    entry = ops.rules_list(project="proj_a")["rules"][0]
    assert entry["id"] == det["id"]
    assert entry["hit_rate"] is None
    assert "never fired" in entry["hit_rate_note"]
    assert entry["condition_prose"] == rules.render_condition(rules.parse_condition(COND))
    assert [r["id"] for r in entry["remedies"]] == [rule["id"]]
    assert entry["fires"]["total"] == 0


def test_a_detector_that_fired_carries_the_rate_ops_computed(store, registered):
    det, _rule = registered
    common = dict(detector_id=det["id"], project="proj_a", order_id="wo-1",
                  order_kind="work_order", fingerprint="fp", mode=rules.DRY_RUN)
    store.record_rule_fire(outcome=rules.RECORDED, **common)
    store.record_rule_fire(outcome=rules.UNREADABLE, **common)
    entry = ops.rules_list()["rules"][0]
    assert entry["fires"]["total"] == 2
    assert entry["fires"]["by_outcome"][rules.UNREADABLE] == 1
    assert entry["hit_rate"] == pytest.approx(0.5)
    assert entry["hit_rate_note"] is None


def test_the_counts_lead_and_include_the_retracted(store, registered):
    det, _rule = registered
    store.add_detector("red-base-inherited", COND)
    store.retract_detector(det["id"], "fixed in code")
    counts = ops.rules_list()["counts"]
    assert counts == {"total": 2, "armed": 0, "dry_run": 1, "retracted": 1}


# -- rules_show -------------------------------------------------------------------------


def test_show_carries_the_whole_provenance_chain(store, registered):
    det, rule = registered
    store.record_rule_fire(detector_id=det["id"], project="proj_a", order_id="wo-1",
                           order_kind="work_order", fingerprint="fp",
                           mode=rules.DRY_RUN, outcome=rules.RECORDED)
    data = ops.rules_show(det["id"])
    assert data["detector"]["id"] == det["id"]
    assert data["condition_prose"]
    assert data["provenance"] == {"source": "io", "io_id": "io-1", "fix_wo_id": "wo-9",
                                  "issue_url": "https://example/786",
                                  "pr_url": "https://example/pr/1", "seed_version": 1}
    assert data["remedies"][0]["primitive"] == "nudge"
    assert data["remedies"][0]["params"] == {}
    assert data["remedies"][0]["id"] == rule["id"]
    assert [f["outcome"] for f in data["fires"]] == [rules.RECORDED]


def test_show_refuses_an_unknown_id(store):
    with pytest.raises(ops.OpsError):
        ops.rules_show("dt-nope")


# -- rules_retract ----------------------------------------------------------------------


def test_retract_dispatches_on_the_prefix_and_never_deletes(store, registered):
    det, rule = registered
    out = ops.rules_retract(rule["id"], "the primitive changed shape")
    assert out["rule"]["id"] == rule["id"] and out["rule"]["retired_at"]
    assert store.remedy_rules_for(det["id"], include_retired=True)

    out = ops.rules_retract(det["id"], "the gap was fixed in code")
    assert out["rule"]["status"] == rules.RETRACTED
    assert store.get_detector(det["id"]) is not None


def test_retract_refuses_an_empty_reason_and_an_unknown_id(store, registered):
    det, _rule = registered
    with pytest.raises(ops.OpsError):
        ops.rules_retract(det["id"], "   ")
    with pytest.raises(ops.OpsError):
        ops.rules_retract("dt-nope", "because")
    with pytest.raises(ops.OpsError):
        ops.rules_retract("xx-1", "because")
    assert store.get_detector(det["id"])["retired_at"] is None


# -- rules_dry_run ----------------------------------------------------------------------


def test_dry_run_without_an_order_lists_what_the_condition_reads(store, registered):
    det, _rule = registered
    data = ops.rules_dry_run(det["id"])
    assert data["readable"] is True
    assert data["evaluated"] is False
    assert set(data["fields"]) == {"status", "seconds_in_status"}
    assert set(data["sources"]) == {"work_order", "state_durations"}
    assert data["condition_prose"]


def test_the_active_basis_field_reads_the_same_source(store):
    """Spec §6 of
    docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md
    """
    det = store.add_detector(
        "held-too-long",
        {"all": [{"field": "seconds_in_status_active", "op": "gte", "value": 3600}]},
        project="proj_a", summary="really working an hour", source="io")

    data = ops.rules_dry_run(det["id"])

    assert data["readable"] is True
    assert set(data["fields"]) == {"seconds_in_status_active"}
    assert set(data["sources"]) == {"state_durations"}


def test_dry_run_on_an_order_whose_snapshot_cannot_be_built_decides_nothing(
        store, registered, tmp_path, monkeypatch):
    """A snapshot that could not be built is the unreadable case, one level down.

    `matched` stays None — never "no match", which would report a verdict nobody
    computed. The reader is a store on a path with no project database behind it.
    """
    det, _rule = registered
    monkeypatch.setattr(ops, "find_work_order",
                        lambda oid, project_name=None: ("proj_a", tmp_path,
                                                        {"id": oid, "kind": "code"}))

    def refuse(*a, **kw):
        raise RuntimeError("the timeline could not be read")

    monkeypatch.setattr(ops, "rule_facts", refuse)
    data = ops.rules_dry_run(det["id"], "wo-1")
    assert data["evaluated"] is False
    assert data["matched"] is None            # never a verdict nobody computed
    assert "could not be built" in data["note"]
    assert store.list_rule_fires() == []      # it writes NOTHING


def test_dry_run_evaluates_the_fact_snapshot(store, registered, tmp_path,
                                             monkeypatch):
    det, _rule = registered

    def fake_facts(store_, wo, *, now, sources=None):
        return rules.Facts(project="proj_a", order_id="wo-1", order_kind="work_order",
                           values={"status": "needs_review", "seconds_in_status": 7200},
                           now=now)

    monkeypatch.setattr(rules, "facts", fake_facts)
    monkeypatch.setattr(ops, "find_work_order",
                        lambda oid, project_name=None: ("proj_a", tmp_path,
                                                        {"id": oid, "kind": "code"}))
    data = ops.rules_dry_run(det["id"], "wo-1")
    assert data["evaluated"] is True
    assert data["matched"] is True
    assert data["explanation"].startswith("matched:")
    assert data["absent"] == []
    assert store.list_rule_fires() == []


def test_dry_run_on_a_condition_that_no_longer_parses_reports_it_unreadable(store,
                                                                           registered):
    det, _rule = registered
    # A row written by an older release naming a field this one removed. Written raw on
    # purpose: `add_detector` cannot produce it, which is the point.
    store.conn.execute("UPDATE detectors SET condition=? WHERE id=?",
                       (json.dumps({"all": [{"field": "moon_phase", "op": "eq",
                                             "value": "full"}]}), det["id"]))
    data = ops.rules_dry_run(det["id"], "wo-1")
    assert data["readable"] is False
    assert data["evaluated"] is False
    assert data["matched"] is None
    assert any("moon_phase" in p for p in data["problems"])
    assert store.list_rule_fires() == []


# -- the CLI ----------------------------------------------------------------------------


def test_the_family_is_exactly_five_verbs_and_names_the_other_registry():
    """`arm` is NOT in this section, and this assertion is what keeps it out.

    The description has to open by saying which registry this is and name `jarvis gate
    rules`: one verb meaning two registries is how that family stops being readable.
    """
    parser = cli.build_parser()
    rules_parser = parser._subparsers._group_actions[0].choices["rules"]
    verbs = set(rules_parser._subparsers._group_actions[0].choices)
    # `summary` joined in §9 (the evolution report). `arm` still has not.
    assert verbs == {"list", "show", "retract", "dry-run", "summary"}
    assert "gate rules" in rules_parser.description
    assert rules_parser.description.strip().startswith("the self-healing")


def test_the_cli_drives_the_whole_surface(store, registered, capsys):
    det, rule = registered
    assert cli.main(["rules", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counts"]["total"] == 1

    assert cli.main(["rules", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "1 rule: 0 armed, 1 in dry run, 0 retracted"
    assert det["id"] in out

    assert cli.main(["rules", "show", det["id"]]) == 0
    out = capsys.readouterr().out
    assert "nudge" in out and "io-1" in out and "https://example/786" in out

    assert cli.main(["rules", "dry-run", det["id"]]) == 0
    assert "seconds_in_status" in capsys.readouterr().out

    assert cli.main(["rules", "retract", rule["id"], "--reason", "shape changed"]) == 0
    assert "retracted" in capsys.readouterr().out
    assert store.remedy_rules_for(det["id"]) == []


def test_the_counts_line_is_plural_aware_and_leads_an_empty_registry(store, capsys):
    assert cli.main(["rules", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "0 rules: 0 armed, 0 in dry run, 0 retracted"


def test_retract_without_a_reason_fails(store, registered):
    det, _rule = registered
    with pytest.raises(SystemExit):
        cli.main(["rules", "retract", det["id"]])


def test_an_unknown_detector_is_an_error_not_a_traceback(store, capsys):
    assert cli.main(["rules", "show", "dt-nope"]) == 1
    assert "error:" in capsys.readouterr().err


def test_show_clips_a_long_detail_to_one_line(store, registered, capsys):
    det, _rule = registered
    store.record_rule_fire(detector_id=det["id"], project="proj_a", order_id="wo-1",
                           order_kind="work_order", fingerprint="fp",
                           mode=rules.DRY_RUN, outcome=rules.RECORDED,
                           detail="x" * 4000)
    assert cli.main(["rules", "show", det["id"]]) == 0
    out = capsys.readouterr().out
    fire_line = [ln for ln in out.splitlines() if ln.strip().startswith("fire ")][0]
    assert len(fire_line) < 200 and fire_line.endswith("…")
    # The stored value is untouched: only the terminal line is clipped.
    assert ops.rules_show(det["id"])["fires"][0]["detail"] == "x" * 4000


def test_list_is_one_line_per_detector_and_leaves_the_prose_to_show(store, registered,
                                                                    capsys):
    det, _rule = registered
    prose = rules.render_condition(rules.parse_condition(COND))
    assert cli.main(["rules", "list"]) == 0
    assert prose not in capsys.readouterr().out
    assert cli.main(["rules", "show", det["id"]]) == 0
    assert prose in capsys.readouterr().out


def test_the_hit_rate_line_agrees_in_number(store, registered, capsys):
    det, _rule = registered
    common = dict(detector_id=det["id"], project="proj_a", order_id="wo-1",
                  order_kind="work_order", fingerprint="fp", mode=rules.DRY_RUN)
    store.record_rule_fire(outcome=rules.RECORDED, **common)
    assert cli.main(["rules", "list"]) == 0
    assert "1 hit of 1 fire" in capsys.readouterr().out

    store.record_rule_fire(outcome=rules.RECORDED, **common)
    store.record_rule_fire(outcome=rules.UNREADABLE, **common)
    assert cli.main(["rules", "list"]) == 0
    assert "2 hits of 3 fires" in capsys.readouterr().out


def test_the_absent_case_keeps_its_sentence_verbatim(store, registered, capsys):
    det, _rule = registered
    note = ops.rules_list()["rules"][0]["hit_rate_note"]
    assert cli.main(["rules", "list"]) == 0
    assert note in capsys.readouterr().out
    assert "not a hit rate of zero" in note
