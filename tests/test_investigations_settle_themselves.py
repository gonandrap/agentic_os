"""An investigation settles itself, and so does its investigator (io-d2a5dd69, io-b521d17b)"""

from __future__ import annotations

import re
import sqlite3

import pytest
# The investigation fixtures, and the stuck sweep's they build on
from test_health_sweep import stuck_catalog, stuck_os  # noqa: F401
from test_investigation_orders import (WHY, _investigating, _subject,  # noqa: F401
                                       started, store)

from jarvis import cli, hooks, invariants, landing, ops, project_store
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import WO_KIND_CONTRACTS, WO_KINDS
from jarvis.testing import a_report, a_verdict

DIED = "the turn's process ended without writing a result"


def _verdict_in(store, investigator_env: bool, monkeypatch, classification="TRANSIENT"):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    investigator = _investigating(store, inv)
    if investigator_env:
        # The verdict command runs INSIDE the investigator's own turn
        monkeypatch.setenv("JARVIS_WO_ID", investigator)
    out = ops.submit_verdict(inv["id"], a_verdict(classification, subject=subject))
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    return inv["id"], investigator, out


def _dead_turn(store, wo_id: str) -> None:
    turn = store.create_turn(wo_id, "dispatch", "investigate it")
    store.finish_turn(turn["id"], "failed", error=DIED)


def _settle(catalog_file, store, wo_id: str) -> dict:
    daemon = Daemon(load_catalog(catalog_file))
    daemon.settle_work_order(daemon.catalog.project("proj_a"), store,
                             store.get_work_order(wo_id))
    return store.get_work_order(wo_id)


# -- close-out-kills-its-caller


def test_a_verdict_from_inside_the_investigator_settles_it_without_killing_it(
        started, store, monkeypatch):
    inv_id, investigator, out = _verdict_in(store, True, monkeypatch)

    row = store.get_work_order(investigator)
    assert row["status"] == "completed"
    assert not row["needs_attention"]
    kinds = [e["kind"] for e in store.list_events(investigator)]
    assert "verdict_submitted_settled" in kinds
    assert "session_stopped" not in kinds
    assert out["investigator"] == {"wo_id": investigator, "status": "completed",
                                   "session_stopped": False}
    assert "warning" not in out


def test_close_out_refuses_the_work_order_whose_process_is_calling_it(
        started, store, monkeypatch):
    wo = store.create_work_order("ship it")
    store.set_status(wo["id"], "running")
    monkeypatch.setenv("JARVIS_WO_ID", wo["id"])

    with pytest.raises(ops.OpsError, match="inside its own session"):
        ops.close_out(store, store.get_work_order(wo["id"]), "marked_done", why="x")
    assert store.get_work_order(wo["id"])["status"] == "running", "nothing written"

    # From outside — `jarvis wo done`, the merge poller — it closes exactly as before
    monkeypatch.delenv("JARVIS_WO_ID")
    ops.close_out(store, store.get_work_order(wo["id"]), "marked_done", why="x")
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_a_failed_settlement_after_the_verdict_is_stored_is_not_a_rejection(
        started, store, monkeypatch):
    def broken(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ops, "settle_in_session", broken)
    inv_id, investigator, out = _verdict_in(store, True, monkeypatch)

    assert store.get_feature_order(inv_id)["status"] == "completed"
    assert "IS stored" in out["warning"] and "do NOT resubmit" in out["warning"]
    assert investigator in out["warning"] and "investigator" not in out


# -- reaper-has-no-investigator-case


def test_a_turn_that_dies_after_its_verdict_settles_completed_quietly(
        started, store, catalog_file):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    investigator = _investigating(store, inv)
    ops.submit_verdict(inv["id"], a_verdict("TRANSIENT", subject=subject))
    # The pre-fix shape: the settle never landed, and the turn is reaped as dead
    store.set_status(investigator, "running")
    _dead_turn(store, investigator)

    row = _settle(catalog_file, store, investigator)

    assert row["status"] == "completed"
    assert not row["needs_attention"]
    assert [n for n in store.unrouted_notifications() if n["wo_id"] == investigator] == []


def test_an_investigator_that_dies_before_its_verdict_still_fails_loudly(
        started, store, catalog_file):
    inv = ops.create_investigation_order("proj_a", _subject(store), WHY)
    investigator = _investigating(store, inv)
    store.set_status(investigator, "running")
    _dead_turn(store, investigator)

    row = _settle(catalog_file, store, investigator)

    assert row["status"] == "failed"
    assert row["attention_reason"] == invariants.WORKER_FAILED_BLOCKER
    assert [n for n in store.unrouted_notifications() if n["wo_id"] == investigator]


def test_the_reconcile_pass_drains_investigators_already_flagged(
        started, store, catalog_file):
    """The prod backlog: `failed` and flagged, or `cancelled` and flagged, over a stored verdict"""
    daemon = Daemon(load_catalog(catalog_file))
    spec = daemon.catalog.project("proj_a")
    stuck = []
    for status, reason in (("failed", "2 assumptions pending your review"),
                           ("failed", invariants.WORKER_FAILED_BLOCKER),
                           ("cancelled", invariants.WORKER_FAILED_BLOCKER)):
        subject = _subject(store)
        inv = ops.create_investigation_order("proj_a", subject, WHY)
        investigator = _investigating(store, inv)
        store.add_assumption(investigator, "the hold is never re-derived")
        ops.submit_verdict(inv["id"], a_verdict("TRANSIENT", subject=subject))
        store.set_status(investigator, status)
        store.flag_attention(investigator, reason)
        stuck.append((investigator, status))
    # The control: no verdict, so a real failure that keeps its flag
    dead = _investigating(store, ops.create_investigation_order(
        "proj_a", _subject(store), WHY))
    store.set_status(dead, "failed")
    store.flag_attention(dead, invariants.WORKER_FAILED_BLOCKER)

    daemon.settle_landed_investigators(spec, store)

    for investigator, status in stuck:
        row = store.get_work_order(investigator)
        assert not row["needs_attention"], investigator
        assert row["status"] == ("cancelled" if status == "cancelled" else "completed")
    assert store.get_work_order(dead)["needs_attention"]


# -- brief-orders-unclearable-assumptions


def test_an_investigator_assumption_raises_nothing_and_shows_beside_the_verdict(
        started, store):
    inv = ops.create_investigation_order("proj_a", _subject(store), WHY)
    investigator = _investigating(store, inv)
    store.set_status(investigator, "running")

    ops.assume(investigator, "the hold is written once and never re-derived")

    row = store.get_work_order(investigator)
    assert not row["needs_attention"]
    assert invariants.true_blockers(store, row) == []
    shown = ops.show_investigation_order(inv["id"])
    assert [a["content"] for a in shown["assumptions"]] == [
        "the hold is written once and never re-derived"]
    # A failed investigator with no verdict is flagged for the FAILURE, never for its calls
    store.set_status(investigator, "failed")
    blockers = invariants.true_blockers(store, store.get_work_order(investigator))
    assert blockers == [invariants.WORKER_FAILED_BLOCKER]


def test_every_other_kind_still_puts_its_assumptions_to_the_user(started, store):
    wo = store.create_work_order("ship it")
    store.set_status(wo["id"], "needs_review")
    ops.assume(wo["id"], "CSV, not TSV (fixture decision)")
    assert any("assumption" in b for b in
               invariants.true_blockers(store, store.get_work_order(wo["id"])))


# -- listing-reads-the-parent-flag-only / one-attention-line-two-counters


def _flagged_family(store) -> tuple[str, str]:
    """A settled, quiet investigation whose investigator still holds a flag"""
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    investigator = _investigating(store, inv)
    ops.submit_verdict(inv["id"], a_verdict("TRANSIENT", subject=subject))
    store.set_status(investigator, "failed")
    store.flag_attention(investigator, invariants.WORKER_FAILED_BLOCKER)
    return inv["id"], investigator


def test_a_flagged_child_makes_its_family_listable_and_the_strip_agrees(started, store):
    inv_id, investigator = _flagged_family(store)
    assert not store.get_feature_order(inv_id)["needs_attention"]

    listed = [r["id"] for r in ops.list_investigation_orders("proj_a")]
    strip = [a for a in ops.os_status()["attention"] if a.get("fo_id")]

    assert inv_id in listed
    assert [a["fo_id"] for a in strip] == listed


def test_the_line_counts_the_child_it_names_and_decides_with_a_command_that_runs(
        started, store, capsys):
    inv_id, investigator = _flagged_family(store)

    line = next(a for a in ops.os_status()["attention"] if a.get("fo_id") == inv_id)

    assert investigator in line["reason"]
    assert "no children yet" not in line["reason"]
    assert line["decide"] == f"jarvis investigate show {inv_id}"


def test_every_decide_command_is_accepted_by_the_cli(started, store, capsys):
    fo = store.create_feature_order(title="CSV export", description="export")
    io = ops.create_improvement_order("proj_a", "slow dispatch", "it is slow",
                                      refs=["wo-11111111"])
    inv = ops.create_investigation_order("proj_a", _subject(store), WHY)
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    rows = {"feature": fo["id"], "improvement": io["id"],
            "investigation": inv["id"], "decision": do["id"]}
    assert set(rows) == set(project_store.FO_KINDS)
    for kind, fo_id in rows.items():
        argv = project_store.decide_command(kind, fo_id).split()[1:]
        if kind == "improvement":
            # A REVIEW needs a decision to be valid; that it parses and names this kind is the claim
            with pytest.raises(SystemExit) as e:
                cli.build_parser().parse_args([*argv, "--help"])
            assert e.value.code == 0
            continue
        assert cli.main(argv) == 0, kind


# -- finish-is-not-a-submission / a-refusal-outside-the-kind-vocabulary


def test_every_kind_declares_a_settlement_route():
    """The table is the seam: a kind added later cannot ship without choosing one"""
    assert set(WO_KIND_CONTRACTS) == set(WO_KINDS)
    for kind, contract in WO_KIND_CONTRACTS.items():
        assert contract.route in (project_store.SETTLE_BY_FINISH,
                                  project_store.SETTLE_IN_SESSION,
                                  project_store.SETTLE_IDLE), kind
        assert bool(contract.artifact) == bool(contract.submit), kind
        if contract.artifact:
            assert contract.artifact in contract.submit, kind
        if contract.route == project_store.SETTLE_IN_SESSION:
            assert contract.artifact, f"{kind} settles by a submission it must name"
    assert hooks.VERDICT_FILE == WO_KIND_CONTRACTS["investigator"].artifact


def _commands(text: str) -> list[str]:
    return [c for c in re.findall(r"`([^`]+)`", text)
            if c.split()[0] in ("jarvis", "git", "gh")]


@pytest.mark.parametrize("kind", WO_KINDS)
def test_a_refusal_suggests_only_commands_its_kind_may_run(kind, tmp_path):
    contract = WO_KIND_CONTRACTS[kind]
    wo = {"id": "wo-abc12345", "kind": kind, "parent_id": "inv-1234abcd"}
    work = landing.Authored(branch="b", base="origin/main", dirty=("notes.txt",))

    refusal = ops.unlanded_refusal(wo, "", work)

    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_WO_KIND": kind}
    for command in _commands(refusal):
        decision = hooks.investigator_bash_decision(
            {"tool_name": "Bash", "cwd": str(tmp_path),
             "tool_input": {"command": command}}, env)
        assert decision is None or (decision["hookSpecificOutput"]
                                    ["permissionDecision"] != "deny"), (kind, command)
    if not contract.lands_code:
        assert "pull request" not in refusal and "--pr" not in refusal
        assert contract.artifact in refusal
        assert contract.submit.format(fo_id="inv-1234abcd") in refusal


def test_a_report_stored_before_its_analyst_settles_says_so(started, store, monkeypatch):
    io = ops.create_improvement_order("proj_a", "slow dispatch", "it is slow",
                                      refs=["wo-11111111"])
    analyst = store.create_work_order("analyse it", kind="analyst",
                                      parent_id=io["id"])
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"])
    store.set_feature_status(io["id"], "planning")
    store.set_status(analyst["id"], "running")

    def refused(*_a, **_k):
        raise ops.OpsError("wo-x has produced 1 uncommitted file")

    monkeypatch.setattr(ops, "finish", refused)

    out = ops.submit_findings(io["id"], a_report())

    assert store.get_feature_order(io["id"])["status"] == "plan_review"
    assert "IS stored" in out["warning"]


# -- issue 1322: an analyst's evidence, its settled parent, and the panel's stale claim


def test_an_analysts_refusal_gives_it_a_way_out_of_its_own():
    wo = {"id": "wo-abc12345", "kind": "analyst", "parent_id": "io-1234abcd"}
    work = landing.Authored(branch="b", base="origin/main", dirty=("repro.py",))

    refusal = ops.unlanded_refusal(wo, "", work)

    assert ops.NON_LANDING_EVIDENCE_RULE in refusal
    assert "--abandon" in refusal
    assert "user's call" not in refusal


def _analyst_under(store, io_status: str):
    io = ops.create_improvement_order("proj_a", "slow dispatch", "it is slow",
                                      refs=["wo-11111111"])
    analyst = store.create_work_order("analyse it", kind="analyst",
                                      parent_id=io["id"])
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"])
    store.set_feature_status(io["id"], io_status)
    store.set_status(analyst["id"], "validating")
    return io, analyst


def test_a_validating_analyst_under_a_completed_io_is_settled(started, store):
    _io, analyst = _analyst_under(store, "completed")

    found = list(invariants.check_consumed_documents_settle(store))

    assert [v.wo_id for v in found] == [analyst["id"]]
    assert store.get_work_order(analyst["id"])["status"] == "completed"
    kinds = [e["kind"] for e in store.list_events(analyst["id"])]
    assert "document_consumed_settled" in kinds


def test_a_validating_analyst_under_a_live_io_is_left_alone(started, store):
    _io, analyst = _analyst_under(store, "plan_review")

    assert list(invariants.check_consumed_documents_settle(store)) == []
    assert store.get_work_order(analyst["id"])["status"] == "validating"


def test_review_feedback_to_an_analyst_lists_what_its_io_filed_now(started, store):
    io, analyst = _analyst_under(store, "plan_review")
    report = a_report()
    report["findings"][0]["created_orders"] = [{"id": "wo-9aa92369", "type": "work"}]
    store.update_feature_order(io["id"], plan=ops.db.to_json(report))

    note = ops.non_landing_review_note(store, store.get_work_order(analyst["id"]))

    assert ops.NON_LANDING_EVIDENCE_RULE in note
    assert "wo-9aa92369" in note
    assert ops.non_landing_review_note(store, {"id": "wo-1", "kind": "worker"}) == ""
