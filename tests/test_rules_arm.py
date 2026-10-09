"""Arming: a fire becomes an alarm and takes the gate that already exists.

docs/superpowers/specs/2026-09-27-self-evolution.md §6.

WHAT THESE TESTS ARE BUILT TO AVOID. A negative ("no approval was filed", "no second row")
is green on a path that never ran, so every refusal here sits beside the positive it is the
opposite of, in the same fixture. And nothing below writes `detectors.status` in SQL: the
verbs under test (`arm_detector`, `ops.rules_arm`) are the only door, and a test that
bypassed them would prove the daemon reads a column rather than that arming works.

The bridge is asserted END TO END against the real stores: the alarm row, the `rule_fired`
event, the `self_heal` approval and the `kind="approval"` Neo question are all read back
from the databases `remedies.propose` wrote, not from a mock of it.
"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, health, ops, remedies, rules, timeline
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import (ALARM_EVENT_KINDS, ALARM_SOURCES, NO_TURN,
                                  ProjectStore)

COND = {"field": "status", "op": "eq", "value": "needs_review"}
ARGUMENT = "ask the session where it got to"


# -- the harness -----------------------------------------------------------------------


def _write_catalog(tmp_path, project, *, remedies_allowed=None, rules_cfg=None,
                   project_rules=None):
    """A catalog with the evaluation pass ON. `remedies_allowed=None` leaves the
    supervisor's remedies at their shipped default (off, empty allow-list)."""
    os_cfg = {
        "defaults": {"model": "sonnet", "max_in_flight": 50},
        "notifications": {"sinks": ["log"]},
        "rules": rules_cfg or {"enabled": True},
    }
    if remedies_allowed is not None:
        os_cfg["supervisor"] = {"enabled": True,
                                "remedies": {"enabled": True,
                                             "allowed": list(remedies_allowed)}}
    entry = {"name": "proj_a", "path": str(project), "description": "test project"}
    if project_rules is not None:
        entry["rules"] = project_rules
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"os": os_cfg, "projects": [entry]}))
    return path


@pytest.fixture()
def boot(tmp_path, jarvis_home, fake_claude, project):
    """Start the OS on a catalog built from keyword arguments; returns the Daemon."""
    def go(**kw):
        path = _write_catalog(tmp_path, project, **kw)
        ops.start_os(str(path), foreground=True)
        return Daemon(load_catalog(path))
    return go


@pytest.fixture()
def central(jarvis_home):
    c = CentralStore()
    yield c
    c.close()


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _rule(central, *, argument=ARGUMENT, primitive="nudge", project="proj_a"):
    """One detector with one remedy row, every seeded rule retracted so the only fire in
    a test is this one's."""
    for d in central.list_detectors():
        central.retract_detector(d["id"], "not under test")
    det = central.add_detector("gap-under-test", COND, project=project, source="user")
    rm = central.add_remedy_rule(det["id"], primitive, argument=argument)
    return det, rm


def _tick(daemon, store):
    return daemon.rules_tick(daemon.catalog.project("proj_a"), store)


def _armed_fire(daemon, central, store, *, status="needs_review"):
    """Arm a fresh rule, put an order in its condition, run one pass; returns everything."""
    det, rm = _rule(central)
    ops.rules_arm(det["id"], "three clean dry-run hits", by="gonzalo")
    wo = store.create_work_order("stuck in review", status=status)
    counts = _tick(daemon, store)
    return det, rm, wo, counts


def _fires(central, det):
    return central.list_rule_fires(detector_id=det["id"], limit=50)


def _neo_approvals():
    neo = NeoStore()
    try:
        return [q for q in neo.list_questions() if q["kind"] == "approval"]
    finally:
        neo.close()


def _inbox(central, needle):
    return [r for r in central.conn.execute("SELECT * FROM inbox").fetchall()
            if needle in r["title"]]


# -- (a) arm: the audit, the pair, and every refusal --------------------------------------


def test_arm_audits_the_row_and_flips_the_detector_and_its_live_remedy_rows(central):
    det, rm = _rule(central)
    dead = central.add_remedy_rule(det["id"], "unblock", argument="x")
    central.retract_remedy_rule(dead["id"], "replaced")

    out = central.arm_detector(det["id"], by="gonzalo", reason="three clean hits")

    assert out["status"] == rules.ARMED
    assert (out["armed_by"], out["armed_reason"]) == ("gonzalo", "three clean hits")
    assert out["armed_at"] is not None
    assert central.get_remedy_rule(rm["id"])["status"] == rules.ARMED
    # A retracted remedy row is history; arming must not resurrect it.
    assert central.get_remedy_rule(dead["id"])["status"] == rules.RETRACTED


@pytest.mark.parametrize("case", ["unknown", "retracted", "armed", "no-reason",
                                  "blank-reason", "no-remedy"])
def test_arm_refuses_and_writes_nothing(central, case):
    det, rm = _rule(central)
    target = det["id"]
    reason = "because"
    if case == "unknown":
        target, exc = "dt-nope", KeyError
    elif case == "retracted":
        central.retract_detector(det["id"], "wrong")
        exc = ValueError
    elif case == "armed":
        central.arm_detector(det["id"], by="a", reason="first")
        exc = ValueError
    elif case == "no-reason":
        reason, exc = "", ValueError
    elif case == "blank-reason":
        reason, exc = "   ", ValueError
    else:
        central.retract_remedy_rule(rm["id"], "gone")
        exc = ValueError
    before = central.get_detector(det["id"])

    with pytest.raises(exc):
        central.arm_detector(target, by="gonzalo", reason=reason)

    assert central.get_detector(det["id"]) == before


def test_disarm_returns_the_set_to_dry_run_and_keeps_the_audit_of_the_arm(central):
    det, rm = _rule(central)
    central.arm_detector(det["id"], by="gonzalo", reason="earned it")

    out = central.disarm_detector(det["id"], "two false positives")

    assert out["status"] == rules.DRY_RUN
    assert central.get_remedy_rule(rm["id"])["status"] == rules.DRY_RUN
    assert (out["armed_by"], out["armed_reason"]) == ("gonzalo", "earned it")
    assert out["armed_at"] is not None
    with pytest.raises(ValueError):
        central.disarm_detector(det["id"], "again")      # no longer armed


def test_the_pair_flips_in_one_transaction(central, monkeypatch):
    """If the second UPDATE fails the first must not have landed."""
    det, rm = _rule(central)
    real = central.conn

    class Boom:
        def __init__(self):
            self.n = 0

        def execute(self, sql, *a):
            if sql.startswith("UPDATE remedy_rules"):
                raise RuntimeError("disk on fire")
            return real.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(central, "conn", Boom())
    with pytest.raises(RuntimeError):
        central.arm_detector(det["id"], by="a", reason="r")
    monkeypatch.setattr(central, "conn", real)

    assert central.get_detector(det["id"])["status"] == rules.DRY_RUN


def test_a_detector_row_that_predates_the_writers_still_lists_shows_and_arms(
        central, boot):
    boot()
    for d in central.list_detectors():
        central.retract_detector(d["id"], "not under test")
    central.conn.execute(
        "INSERT INTO detectors (id, ts, gap_class, condition) VALUES (?,?,?,?)",
        ("dt-old", 1.0, "old-gap", json.dumps(COND)))
    central.conn.execute(
        "INSERT INTO remedy_rules (id, detector_id, ts, primitive) VALUES (?,?,?,?)",
        ("rm-old", "dt-old", 1.0, "nudge"))

    assert any(r["id"] == "dt-old" for r in ops.rules_list()["rules"])
    assert ops.rules_show("dt-old")["detector"]["armed_at"] is None
    out = ops.rules_arm("dt-old", "legacy but earned")
    assert out["detector"]["status"] == rules.ARMED
    assert out["detector"]["armed_by"] == "user"


# -- (b) the announcement happens once ----------------------------------------------------


def test_arming_announces_once_at_info_and_a_reconcile_tick_adds_nothing(
        boot, central, store):
    daemon = boot()
    det, _ = _rule(central)

    ops.rules_arm(det["id"], "three clean dry-run hits", by="gonzalo")
    rows = _inbox(central, det["id"])
    assert len(rows) == 1 and rows[0]["level"] == "info"
    assert "gonzalo" in rows[0]["title"]

    daemon.tick()
    daemon.tick()

    assert len(_inbox(central, det["id"])) == 1

    with pytest.raises(ops.OpsError):
        ops.rules_arm(det["id"], "again")           # refused, and announces nothing
    assert len(_inbox(central, det["id"])) == 1


def test_ops_arm_needs_a_reason_and_maps_store_refusals_to_ops_errors(boot, central):
    boot()
    det, _ = _rule(central)
    with pytest.raises(ops.OpsError):
        ops.rules_arm(det["id"], "  ")
    with pytest.raises(ops.OpsError):
        ops.rules_arm("dt-nope", "why not")
    assert central.get_detector(det["id"])["status"] == rules.DRY_RUN


# -- (c) the armed fire: an alarm, an event, a self_heal approval and a Neo question ----------


def test_an_armed_fire_raises_an_alarm_and_files_the_existing_gate(boot, central, store):
    daemon = boot(remedies_allowed=["nudge"])
    det, rm, wo, counts = _armed_fire(daemon, central, store)

    (fire,) = _fires(central, det)
    assert (fire["mode"], fire["outcome"]) == (rules.ARMED, rules.PROPOSED)
    assert fire["remedy_rule_id"] == rm["id"] and fire["alarm_id"]
    assert counts["proposed"] == 1 and counts["refused"] == 0

    alarm = store.get_alarm(fire["alarm_id"])
    assert alarm["source"] == "rule" and alarm["probe"] == det["id"]
    assert alarm["seq"] == NO_TURN
    assert alarm["kind"] == "gap-under-test"
    assert alarm["remedy"] == "nudge" and alarm["remedy_argument"] == ARGUMENT
    assert alarm["status"] == "proposed" and alarm["wo_id"] == wo["id"]
    assert alarm["reason"] == rules.render_condition(COND)

    fired = [e for e in store.list_events(wo["id"], limit=500)
             if e["kind"] == "rule_fired"]
    assert len(fired) == 1
    payload = fired[0]["payload"] if isinstance(fired[0]["payload"], dict) else \
        json.loads(fired[0]["payload"])
    assert payload["alarm_id"] == alarm["id"] and payload["detector_id"] == det["id"]

    (approval,) = [a for a in store.list_approvals(wo["id"]) if a["kind"] == "self_heal"]
    assert approval["status"] == "pending"
    assert approval["id"] == alarm["remedy_approval_id"]
    (question,) = _neo_approvals()
    assert question["id"] == approval["neo_question_id"]
    # A proposal is not an act: the order is untouched and nothing reached its session.
    assert store.queued_messages(wo["id"]) == []
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert central.get_detector(det["id"])["hits"] == 1


def test_the_armed_pass_makes_no_network_call_and_spawns_no_subprocess(
        boot, central, store, monkeypatch):
    """`NeoStore.ask` inserts a row and `propose` is stores and events — VERIFIED here,
    not assumed, by making both seams explode and asserting the fire still lands."""
    import socket
    import subprocess

    daemon = boot(remedies_allowed=["nudge"])

    def boom(*a, **k):
        raise AssertionError("the rules pass reached the network or a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    det, _rm, _wo, _counts = _armed_fire(daemon, central, store)

    assert _fires(central, det)[0]["outcome"] == rules.PROPOSED


# -- (d) the allow-list refusal ----------------------------------------------------------


def test_a_refused_proposal_is_recorded_in_propose_s_own_words_and_files_nothing(
        boot, central, store):
    daemon = boot()                 # remedies ship enabled=False, allowed=()
    det, rm, wo, counts = _armed_fire(daemon, central, store)

    (fire,) = _fires(central, det)
    assert (fire["mode"], fire["outcome"]) == (rules.ARMED, rules.REFUSED)
    alarm = store.get_alarm(fire["alarm_id"])
    assert alarm["status"] == "escalated"
    assert fire["detail"] == alarm["verdict_reason"] != ""
    assert "supervisor.remedies.enabled" in fire["detail"]
    assert counts["refused"] == 1 and counts["proposed"] == 0
    assert [a for a in store.list_approvals(wo["id"]) if a["kind"] == "self_heal"] == []
    assert _neo_approvals() == []
    # A refusal is the GATE WORKING, not the rule being right: it is not a hit.
    assert central.get_detector(det["id"])["hits"] == 0


def test_a_propose_that_raises_is_unreadable_with_the_exception_and_is_retried(
        boot, central, store, monkeypatch):
    daemon = boot(remedies_allowed=["nudge"])
    det, rm = _rule(central)
    ops.rules_arm(det["id"], "earned", by="gonzalo")
    wo = store.create_work_order("stuck in review", status="needs_review")
    real = remedies.propose

    def explode(*a, **k):
        raise RuntimeError("neo.db is locked")

    monkeypatch.setattr(remedies, "propose", explode)
    counts = _tick(daemon, store)
    (fire,) = _fires(central, det)
    assert fire["outcome"] == rules.UNREADABLE and fire["mode"] == rules.ARMED
    assert "neo.db is locked" in fire["detail"]
    assert counts["unreadable"] == 1
    assert store.get_alarm(fire["alarm_id"])["status"] == "failed"   # not the supervisor's
    assert store.claim_next_alarm() is None
    assert central.get_detector(det["id"])["hits"] == 0

    # RETRYABLE: the unreadable fire is closed, so the next tick tries again.
    monkeypatch.setattr(remedies, "propose", real)
    _tick(daemon, store)
    outcomes = sorted(f["outcome"] for f in _fires(central, det))
    assert outcomes == [rules.PROPOSED, rules.UNREADABLE]
    assert store.get_work_order(wo["id"])["status"] == "needs_review"


# -- (e) THE COLLISION: the supervisor proposing on the same alarm ------------------------


def test_the_supervisor_proposing_on_a_rules_alarm_is_declined_by_the_existing_refusal(
        boot, central, store):
    daemon = boot(remedies_allowed=["nudge"])
    det, _rm, wo, _ = _armed_fire(daemon, central, store)
    (fire,) = _fires(central, det)
    alarm = store.get_alarm(fire["alarm_id"])
    cfg = daemon.catalog.project("proj_a").supervisor.remedies
    assert [a["id"] for a in store.list_approvals(wo["id"])] == [alarm["remedy_approval_id"]]

    refusal = remedies._refusal(store, alarm, "nudge", cfg)
    assert refusal is not None and "already awaiting a verdict" in refusal

    neo = NeoStore()
    try:
        again = remedies.propose(store, neo, "proj_a", store.get_work_order(wo["id"]),
                                 alarm, "nudge", "ask again", cfg, reason="the supervisor")
    finally:
        neo.close()
    assert again["proposed"] is False and "already awaiting a verdict" in again["reason"]
    # Still ONE gate request and ONE question: the collision filed nothing.
    assert len(store.list_approvals(wo["id"])) == 1
    assert len(_neo_approvals()) == 1


# -- (f) NO_TURN renders as absent -------------------------------------------------------


def test_no_turn_renders_as_absent_in_the_cli_and_on_the_alarm_pages(
        boot, central, store, capsys):
    daemon = boot(remedies_allowed=["nudge"])
    det, _rm, wo, _ = _armed_fire(daemon, central, store)
    (fire,) = _fires(central, det)
    assert ops.turn_label(NO_TURN) == "no turn"

    assert cli.main(["alarms", "show", fire["alarm_id"]]) == 0
    shown = capsys.readouterr().out
    assert "no turn" in shown and "turn -1" not in shown

    assert cli.main(["alarms"]) == 0
    assert "turn -1" not in capsys.readouterr().out

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    client = TestClient(create_app(), follow_redirects=False)
    detail = client.get(f"/alarms/proj_a/{fire['alarm_id']}")
    assert detail.status_code == 200
    assert "no turn" in detail.text and "turn -1" not in detail.text
    listing = client.get("/alarms")
    assert listing.status_code == 200 and "turn -1" not in listing.text


# -- (g) dedupe: one fire, one alarm, a refire only after the clear ------------------------


def test_a_standing_armed_condition_is_one_fire_and_refires_only_after_it_clears(
        boot, central, store):
    daemon = boot(remedies_allowed=["nudge"])
    det, _rm, wo, _ = _armed_fire(daemon, central, store)

    _tick(daemon, store)                       # the condition still stands
    assert len(_fires(central, det)) == 1
    assert len(store.alarms_of(wo["id"])) == 1
    assert len(store.list_approvals(wo["id"])) == 1

    store.set_status(wo["id"], "running")      # the condition clears
    counts = _tick(daemon, store)
    assert counts["closed"] == 1
    kinds = [e["kind"] for e in store.list_events(wo["id"], limit=500)
             if e["kind"].startswith("rule_")]
    assert kinds.count("rule_fired") == 1 and kinds.count("rule_cleared") == 1
    assert central.open_rule_fire(det["id"], wo["id"]) is None

    store.set_status(wo["id"], "needs_review")  # and comes back
    _tick(daemon, store)
    assert len(_fires(central, det)) == 2
    assert len(store.alarms_of(wo["id"])) == 2


def test_a_moved_fingerprint_does_not_file_a_second_request_while_the_first_awaits(
        boot, central, store):
    """The situation changes for reasons that are not a new gap (a queued message); the
    standing condition's proposal is still pending, so it is HELD, not re-filed."""
    daemon = boot(remedies_allowed=["nudge"])
    det, _rm, wo, _ = _armed_fire(daemon, central, store)
    store.queue_message(wo["id"], "an unrelated message")

    counts = _tick(daemon, store)

    assert counts["opened"] == 0
    assert len(_fires(central, det)) == 1
    assert len(store.list_approvals(wo["id"])) == 1


def test_a_dry_run_clearing_writes_no_rule_cleared_event(boot, central, store):
    daemon = boot()
    det, _ = _rule(central)
    wo = store.create_work_order("stuck", status="needs_review")
    _tick(daemon, store)
    store.set_status(wo["id"], "running")
    _tick(daemon, store)

    assert not [e for e in store.list_events(wo["id"], limit=500)
                if e["kind"].startswith("rule_")]
    assert _fires(central, det)[0]["mode"] == rules.DRY_RUN


# -- (h) false positives and the interlock ----------------------------------------------------


def _armed_fire_row(central, det, rm, order="wo-x", **kw):
    kw.setdefault("outcome", rules.PROPOSED)
    return central.record_rule_fire(
        detector_id=det["id"], project="proj_a", order_id=order,
        order_kind="work_order", fingerprint="fp", mode=rules.ARMED,
        remedy_rule_id=rm["id"], **kw)


def test_a_false_positive_increments_both_rows(boot, central):
    boot()
    det, rm = _rule(central)
    ops.rules_arm(det["id"], "earned")
    fire = _armed_fire_row(central, det, rm)

    out = ops.rules_false_positive(fire["id"], "that order was fine")

    assert out["demoted"] is False
    assert out["fire"]["false_positive"] == 1
    assert out["fire"]["false_positive_reason"] == "that order was fine"
    assert central.get_detector(det["id"])["false_positives"] == 1
    assert central.get_remedy_rule(rm["id"])["false_positives"] == 1
    assert central.get_detector(det["id"])["status"] == rules.ARMED


def test_two_false_positives_demote_the_pair_with_one_warning_and_a_third_does_not_again(
        boot, central):
    boot()
    det, rm = _rule(central)
    ops.rules_arm(det["id"], "earned", by="gonzalo")
    fires = [_armed_fire_row(central, det, rm, order=f"wo-{i}") for i in range(3)]

    ops.rules_false_positive(fires[0]["id"], "wrong one")
    out = ops.rules_false_positive(fires[1]["id"], "wrong two")

    assert out["demoted"] is True
    after = central.get_detector(det["id"])
    assert after["status"] == rules.DRY_RUN
    assert central.get_remedy_rule(rm["id"])["status"] == rules.DRY_RUN
    assert (after["armed_by"], after["armed_reason"]) == ("gonzalo", "earned")
    warnings = [r for r in _inbox(central, det["id"]) if r["level"] == "warning"]
    assert len(warnings) == 1 and "false positive" in warnings[0]["title"]
    assert "false positive" in warnings[0]["body"]

    third = ops.rules_false_positive(fires[2]["id"], "wrong three")
    assert third["demoted"] is False
    assert central.get_detector(det["id"])["false_positives"] == 3
    assert len([r for r in _inbox(central, det["id"])
                if r["level"] == "warning"]) == 1


def test_a_false_positive_is_refused_for_unknown_marked_unreasoned_unreadable_or_cleared(
        boot, central):
    boot()
    det, rm = _rule(central)
    ok = _armed_fire_row(central, det, rm)
    unreadable = _armed_fire_row(central, det, rm, outcome=rules.UNREADABLE)
    cleared = _armed_fire_row(central, det, rm, outcome=rules.CLEARED)

    for bad, why in ((99999, "x"), (ok["id"], "  "), (unreadable["id"], "x"),
                     (cleared["id"], "x")):
        with pytest.raises(ops.OpsError):
            ops.rules_false_positive(bad, why)
    assert central.get_detector(det["id"])["false_positives"] == 0

    ops.rules_false_positive(ok["id"], "wrong")
    with pytest.raises(ops.OpsError):
        ops.rules_false_positive(ok["id"], "wrong again")     # already marked
    assert central.get_detector(det["id"])["false_positives"] == 1


def test_a_dry_run_detector_collects_false_positives_without_being_demoted(
        boot, central):
    boot()
    det, _ = _rule(central)
    fires = [central.record_rule_fire(
        detector_id=det["id"], project="proj_a", order_id=f"wo-{i}",
        order_kind="work_order", fingerprint="fp", mode=rules.DRY_RUN,
        outcome=rules.RECORDED) for i in range(3)]
    for f in fires:
        assert ops.rules_false_positive(f["id"], "meh")["demoted"] is False
    assert central.get_detector(det["id"])["false_positives"] == 3
    assert _inbox(central, det["id"]) == []


def test_a_project_overriding_the_disarm_number_demotes_at_its_own(
        boot, central):
    boot(rules_cfg={"enabled": True, "false_positive_disarm": 1},
         project_rules={"false_positive_disarm": 3})
    det, rm = _rule(central)
    ops.rules_arm(det["id"], "earned")
    fires = [_armed_fire_row(central, det, rm, order=f"wo-{i}") for i in range(3)]

    assert ops.rules_false_positive(fires[0]["id"], "a")["demoted"] is False
    assert ops.rules_false_positive(fires[1]["id"], "b")["demoted"] is False
    assert ops.rules_false_positive(fires[2]["id"], "c")["demoted"] is True


def test_the_fleet_wide_disarm_number_applies_where_a_project_names_none(
        boot, central):
    boot(rules_cfg={"enabled": True, "false_positive_disarm": 1})
    det, rm = _rule(central)
    ops.rules_arm(det["id"], "earned")
    fire = _armed_fire_row(central, det, rm)
    assert ops.rules_false_positive(fire["id"], "a")["demoted"] is True


def test_the_disarm_setting_defaults_to_two_and_a_bad_value_is_refused(
        tmp_path, project):
    from jarvis.catalog import CatalogError, RulesConfig, parse_catalog

    assert RulesConfig().false_positive_disarm == 2
    base = {"os": {"rules": {"false_positive_disarm": 0}},
            "projects": [{"name": "proj_a", "path": str(project), "description": "d"}]}
    with pytest.raises(CatalogError):
        parse_catalog(base)
    base["os"]["rules"] = {"false_positive_disarm": "lots"}
    with pytest.raises(CatalogError):
        parse_catalog(base)


# -- (i) arming never moves the health fingerprint ------------------------------------------


def test_the_three_kinds_are_observer_kinds_and_an_armed_fire_s_events_do_not_move_it(
        boot, central, store):
    """Asserted on a REFUSED fire on purpose: it writes `rule_fired` and `remedy_refused`
    and nothing else onto the order, so an unchanged fingerprint isolates exactly the
    kinds §2's correctness rule is about. A PROPOSED fire also writes `gate_requested`,
    which the fingerprint legitimately counts; `_fire_armed` therefore takes the FIRE's
    fingerprint after the action, and the dedupe test below proves it engages."""
    for kind in ("rule_fired", "rule_dry_run", "rule_cleared"):
        assert kind in health.observer_kinds()

    daemon = boot()
    det, _rm = _rule(central)
    ops.rules_arm(det["id"], "earned", by="gonzalo")
    wo = store.create_work_order("stuck in review", status="needs_review")
    unit = {"kind": "work_order", "row": store.get_work_order(wo["id"])}
    before = health.fingerprint(store, unit)

    _tick(daemon, store)

    assert _fires(central, det)[0]["outcome"] == rules.REFUSED
    assert {e["kind"] for e in store.list_events(wo["id"], limit=500)} >= {
        "rule_fired", "remedy_refused"}
    assert health.fingerprint(store, {"kind": "work_order",
                                      "row": store.get_work_order(wo["id"])}) == before


# -- (j) the vocabularies --------------------------------------------------------------------


def test_the_event_vocabularies_agree_and_the_source_is_admitted(store):
    assert timeline.ALARM_KINDS == frozenset(ALARM_EVENT_KINDS)
    assert "rule" in ALARM_SOURCES
    wo = store.create_work_order("o")
    alarm = store.add_finding(wo["id"], kind="gap", reason="r", source="rule",
                              probe="dt-1", remedy="nudge", remedy_argument="a")
    assert (alarm["source"], alarm["remedy"], alarm["remedy_argument"]) == (
        "rule", "nudge", "a")
    plain = store.add_finding(wo["id"], kind="gap", reason="r")
    assert plain["remedy"] is None and plain["remedy_argument"] is None


def test_rule_dry_run_is_declared_and_written_by_nothing(boot, central, store):
    """§5 forbids a timeline event in dry run, so the declared kind stays unwritten."""
    daemon = boot()
    det, _ = _rule(central)
    wo = store.create_work_order("stuck", status="needs_review")
    _tick(daemon, store)
    assert _fires(central, det)[0]["mode"] == rules.DRY_RUN
    assert "rule_dry_run" in ALARM_EVENT_KINDS
    assert not [e for e in store.list_events(wo["id"], limit=500)
                if e["kind"] == "rule_dry_run"]


def test_the_timeline_describes_the_three_kinds_and_links_the_alarm():
    for kind, payload in (("rule_fired", {"alarm_id": "al-1", "detector_id": "dt-1",
                                          "reason": "r"}),
                          ("rule_dry_run", {"detector_id": "dt-1"}),
                          ("rule_cleared", {"alarm_id": "al-1", "detector_id": "dt-1"})):
        label, _detail = timeline._describe(kind, payload)
        assert label and kind not in label
    assert timeline._ref("rule_fired", {"alarm_id": "al-1"})["id"] == "al-1"


# -- the CLI ---------------------------------------------------------------------------------


def test_the_cli_arms_and_marks_false_positives_and_demands_a_reason(
        boot, central, capsys):
    boot()
    det, rm = _rule(central)

    with pytest.raises(SystemExit):
        cli.main(["rules", "arm", det["id"]])
    with pytest.raises(SystemExit):
        cli.main(["rules", "false-positive", "1"])

    assert cli.main(["rules", "arm", det["id"], "--reason", "earned", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["detector"]["status"] == rules.ARMED

    f1 = _armed_fire_row(central, det, rm, order="wo-1")
    f2 = _armed_fire_row(central, det, rm, order="wo-2")
    assert cli.main(["rules", "false-positive", str(f1["id"]), "--reason", "wrong"]) == 0
    assert "marked a false positive" in capsys.readouterr().out
    assert cli.main(["rules", "false-positive", str(f2["id"]), "--reason", "wrong",
                     "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["demoted"] is True

    assert cli.main(["rules", "arm", det["id"], "--reason", "again"]) == 0
    assert cli.main(["rules", "arm", det["id"], "--reason", "third"]) == 1   # already armed
    assert "error:" in capsys.readouterr().err
