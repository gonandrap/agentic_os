"""The evaluation pass: `Daemon.rules_tick`, the fact snapshot, and the five seed rules (docs/superpowers/specs/2026-09-27-self-evolution.md §5.)"""

# clock-exempt: class 3 — measures real monotonic elapsed to bound one rules_tick's wall cost; not a spin-wait
from __future__ import annotations

import inspect
import json
import math
import time

import pytest

from jarvis import db, holds, invariants, ops, remedies, rules
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

HOUR = 3600.0

# -- the harness


@pytest.fixture()
def catalog_on(tmp_path, project):
    """A catalog with the evaluation pass SWITCHED ON"""
    data = {
        "os": {
            "defaults": {"model": "sonnet", "max_in_flight": 50},
            "notifications": {"sinks": ["log"]},
            "rules": {"enabled": True},
        },
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def catalog_off(tmp_path, project):
    data = {
        "os": {
            "defaults": {"model": "sonnet", "max_in_flight": 50},
            "notifications": {"sinks": ["log"]},
        },
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_on, project):
    ops.start_os(str(catalog_on), foreground=True)
    return Daemon(load_catalog(catalog_on))


@pytest.fixture()
def spec(started):
    return started.catalog.project("proj_a")


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def central():
    c = CentralStore()
    yield c
    c.close()


def tick(daemon, spec, store) -> dict:
    return daemon.rules_tick(spec, store)


def detector_for(central, gap_class: str) -> dict:
    """The seeded detector for one gap class"""
    found = central.list_detectors(gap_class=gap_class)
    assert found, f"no seeded detector for {gap_class}"
    return found[0]


def retract_all_but(central, keep: str) -> None:
    """Leave exactly one seed detector live"""
    for det in central.list_detectors():
        if det["gap_class"] != keep:
            central.retract_detector(det["id"], "not under test")


def backdate_status(store, wo_id: str, seconds: float) -> None:
    """Put this order's status span `seconds` into the past"""
    store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE order_id=?",
                       (db.now() - seconds, wo_id))


def backdate_events(store, wo_id: str, seconds: float) -> None:
    ts = db.now() - seconds
    store.conn.execute("UPDATE wo_events SET ts=? WHERE wo_id=?", (ts, wo_id))


def fires_for(central, detector_id: str, order_id: str = "") -> list[dict]:
    return [f for f in central.list_rule_fires(detector_id=detector_id, limit=100)
            if not order_id or f["order_id"] == order_id]


# -- 1. the fact snapshot is built LAZILY, and `holds.held` is named


def test_holds_are_never_read_when_no_live_detector_names_a_hold_or_time_field(
        started, spec, store, central, monkeypatch):
    """Spec §5.2.3, the white-box assertion it asks for by name (docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md)"""
    for det in central.list_detectors():
        central.retract_detector(det["id"], "not under test")
    central.add_detector("status-only", {"field": "status", "op": "eq",
                                         "value": "needs_review"},
                         project="proj_a", source="user")
    calls = []
    real = holds.held
    monkeypatch.setattr(holds, "held",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    wo = store.create_work_order("an ordinary order", status="running")
    store.add_event(wo["id"], "dispatched", {})

    tick(started, spec, store)

    assert calls == [], ("a tick whose live detectors name no hold field and no time "
                         "field called holds.held — the snapshot is no longer lazy")


def test_the_holds_walk_happens_at_most_once_per_order(
        started, spec, store, central, monkeypatch):
    """Both the `holds` source and the `state_durations` source need the same reading"""
    central.add_detector("hold-watch", {"all": [
        {"field": "hold_cause", "op": "eq", "value": "gate"},
        {"field": "seconds_in_status", "op": "gte", "value": 60}]},
        project="proj_a", source="user")
    calls = []
    real = holds.held
    monkeypatch.setattr(holds, "held",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    wo = store.create_work_order("an ordinary order", status="running")
    store.add_event(wo["id"], "dispatched", {})

    tick(started, spec, store)

    assert len(calls) == 1, f"holds.held was walked {len(calls)} times for one order"


def test_holds_are_read_when_a_live_detector_names_a_hold_field(
        started, spec, store, central, monkeypatch):
    calls = []
    real = holds.held
    monkeypatch.setattr(holds, "held",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    central.add_detector("hold-watch", {"field": "hold_cause", "op": "eq",
                                        "value": "gate"},
                         project="proj_a", source="user")
    wo = store.create_work_order("an ordinary order", status="running")
    store.add_event(wo["id"], "dispatched", {})

    tick(started, spec, store)

    assert calls, "a detector naming a hold field did not cause holds.held to be read"


def test_an_absent_source_leaves_its_fields_out_of_values_rather_than_none(store):
    """ABSENT IS A THIRD THING"""
    wo = store.create_work_order("an order", status="running")
    facts = ops.rule_facts(store, store.get_work_order(wo["id"]), now=db.now(),
                           sources=frozenset({"work_order"}), project="proj_a")
    assert facts.values["status"] == "running"
    assert "hold_cause" not in facts.values
    assert "seconds_in_status" not in facts.values
    assert "budget_usd" not in facts.values


def test_a_null_column_is_absent_and_a_budget_of_none_is_not_a_budget_of_zero(store):
    wo = store.create_work_order("an order", status="running")
    facts = ops.rule_facts(store, store.get_work_order(wo["id"]), now=db.now(),
                           sources=frozenset({"work_order", "budget", "pull_request"}),
                           project="proj_a")
    assert "budget_usd" not in facts.values
    assert "pr_url" not in facts.values
    assert "attention_reason" not in facts.values
    assert facts.values["needs_attention"] is False   # the FLAG is recorded, and is a bool


def test_the_active_basis_subtracts_a_hold_from_the_reading_it_already_has(store):
    """`seconds_in_status_active` landed on main while this branch was open"""
    wo = store.create_work_order("an order that was held", status="running")
    store.add_event(wo["id"], "hold_started", {"cause": "usage_limit"})
    store.add_event(wo["id"], "hold_cleared", {"cause": "usage_limit"})
    backdate_status(store, wo["id"], 4 * HOUR)
    backdate_events(store, wo["id"], 3 * HOUR)

    facts = ops.rule_facts(store, store.get_work_order(wo["id"]), now=db.now(),
                           sources=frozenset({"state_durations"}), project="proj_a")

    assert facts.values["seconds_in_status"] >= 4 * HOUR - 60
    assert (facts.values["seconds_in_status_active"]
            <= facts.values["seconds_in_status"])


def test_rules_facts_delegates_to_the_reader_that_owns_the_sources(store):
    """`rules.facts` is the CONTRACT and `ops.rule_facts` is the implementation"""
    wo = store.create_work_order("an order", status="running")
    row = store.get_work_order(wo["id"])
    now = db.now()
    through_contract = rules.facts(store, row, now=now,
                                   sources=frozenset({"work_order"}))
    direct = ops.rule_facts(store, row, now=now, sources=frozenset({"work_order"}))
    assert through_contract.values == direct.values
    assert through_contract.order_kind == "work_order"


# -- 2. the five seed rules: a positive and a negative each


def a_stale_panel_hold(store) -> dict:
    """Issue #786/#813: `needs_review`, flagged unsatisfiable, newest round PASSED"""
    wo = store.create_work_order("a judged order")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], invariants.VALIDATION_STUCK_BLOCKER)
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.close_validation_round(rnd["id"], "passed")
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_healthy_needs_review(store) -> dict:
    """The same status, nothing wrong: no flag, and the round is still open"""
    wo = store.create_work_order("a judged order that is fine")
    store.set_status(wo["id"], "needs_review")
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def an_unreachable_neo_question(store, project) -> dict:
    from jarvis.neo_store import NeoStore

    wo = store.create_work_order("an order parked on a question")
    store.set_status(wo["id"], "waiting_input")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", wo["id"], "which one?")
        neo.conn.execute("UPDATE questions SET status='failed', attempts=0 WHERE id=?",
                         (q["id"],))
    finally:
        neo.close()
    return store.get_work_order(wo["id"])


def a_healthy_waiting_input(store) -> dict:
    """Same status, and the question is with Neo with an attempt spent"""
    from jarvis.neo_store import NeoStore

    wo = store.create_work_order("an order parked on a live question")
    store.set_status(wo["id"], "waiting_input")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", wo["id"], "which one?")
        neo.conn.execute("UPDATE questions SET status='queued', attempts=1 WHERE id=?",
                         (q["id"],))
    finally:
        neo.close()
    return store.get_work_order(wo["id"])


JUDGED = "a" * 40


def a_catch_up_round_burn(store) -> dict:
    """Issue #806: parked, auto-merge held on `sha_moved`, newest round PASSED"""
    wo = store.create_work_order("a parked order whose head moved")
    store.set_status(wo["id"], "waiting_pr_merge", pr_url="https://example/pr/1")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(rnd["id"], JUDGED)
    store.close_validation_round(rnd["id"], "passed")
    store.add_event(wo["id"], "automerge_held",
                    {"code": "sha_moved", "judged_sha": JUDGED, "head_sha": "b" * 40,
                     "round": 1})
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_healthy_parked_order(store) -> dict:
    """Same status, merging normally: no hold event at all"""
    wo = store.create_work_order("a parked order that is merging")
    store.set_status(wo["id"], "waiting_pr_merge", pr_url="https://example/pr/2")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(rnd["id"], JUDGED)
    store.close_validation_round(rnd["id"], "passed")
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_red_base_inherited(store) -> dict:
    """Issue #793: parked, auto-merge held on `base_red`, half an hour gone"""
    wo = store.create_work_order("a parked order on a red base")
    store.set_status(wo["id"], "waiting_pr_merge", pr_url="https://example/pr/3")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.close_validation_round(rnd["id"], "passed")
    store.add_event(wo["id"], "automerge_held",
                    {"code": "base_red", "head_sha": "c" * 40, "round": 1})
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def an_overtaken_release_order(store) -> dict:
    """Issue #784: an open order INV-WORK-LANDED has already caught, idle an hour"""
    wo = store.create_work_order("ship the fix")
    store.add_event(wo["id"], "invariant",
                    {"invariant": "INV-WORK-LANDED", "detail": "already on main",
                     "repaired": False, "repair": ""})
    backdate_events(store, wo["id"], 2 * HOUR)
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_healthy_pending_order(store) -> dict:
    """Same status, idle just as long, and nothing has landed under it"""
    wo = store.create_work_order("a pending order nobody has overtaken")
    store.add_event(wo["id"], "created", {})
    backdate_events(store, wo["id"], 2 * HOUR)
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def _kill_event(store, wo_id: str, round_no: int = 1) -> None:
    from jarvis.daemon import VALIDATION_KILLED_CAUSE

    store.add_event(wo_id, "validation_failed",
                    {"round": round_no, "cause": VALIDATION_KILLED_CAUSE,
                     "attempt": 1, "rc": 143,
                     "error": "killed by a signal (rc=143)", "investigation": ""})


def a_killed_validation_panel(store) -> dict:
    """Issue #1039: the seats of one order's round killed by a signal TWICE"""
    wo = store.create_work_order("an order whose panel keeps being killed")
    store.set_status(wo["id"], "validating")
    _kill_event(store, wo["id"])
    _kill_event(store, wo["id"])
    return store.get_work_order(wo["id"])


def a_healthy_single_kill(store) -> dict:
    """One kill is a blip, and three TRANSPORT outages are a different cause entirely"""
    wo = store.create_work_order("an order killed once, and unreachable thrice")
    store.set_status(wo["id"], "validating")
    _kill_event(store, wo["id"])
    for attempt in (1, 2, 3):
        store.add_event(wo["id"], "validation_failed",
                        {"round": 1, "cause": "transport", "attempt": attempt,
                         "error": "connection reset"})
    return store.get_work_order(wo["id"])


SEEDS = [
    ("stale-panel-hold", a_stale_panel_hold, a_healthy_needs_review),
    ("unreachable-neo-question", an_unreachable_neo_question, a_healthy_waiting_input),
    ("catch-up-round-burn", a_catch_up_round_burn, a_healthy_parked_order),
    ("red-base-inherited", a_red_base_inherited, a_healthy_parked_order),
    ("overtaken-release-order", an_overtaken_release_order, a_healthy_pending_order),
    ("killed-validation-panel", a_killed_validation_panel, a_healthy_single_kill),
]


def test_the_causes_of_a_validation_failure_are_counted_by_cause(store):
    """§5 of docs/superpowers/specs/ 2026-10-14-a-killed-validation-panel-opens-an-investigation.md: every..."""
    wo = a_killed_validation_panel(store)
    store.add_event(wo["id"], "validation_failed",
                    {"round": 1, "cause": "transport", "attempt": 1, "error": "reset"})

    facts = ops.rule_facts(store, store.get_work_order(wo["id"]), now=db.now(),
                           sources=frozenset({"validation_failures"}), project="proj_a")

    assert facts.values["validation_failure_causes"] == {"killed": 2, "transport": 1}


def test_the_failure_causes_are_absent_when_the_source_is_not_requested(store):
    """Its own source slug is the whole point: a condition about `status` must never pay for the event..."""
    wo = a_killed_validation_panel(store)

    facts = ops.rule_facts(store, store.get_work_order(wo["id"]), now=db.now(),
                           sources=frozenset({"work_order"}), project="proj_a")

    assert "validation_failure_causes" not in facts.values


def test_the_killed_panel_condition_needs_two_kills(store):
    """§5: more than once on the same order"""
    condition = next(json.loads(s["detector"]["condition"]) for s in rules.seed_rows()
                     if s["detector"]["gap_class"] == "killed-validation-panel")
    parsed = rules.parse_condition(json.dumps(condition))
    sources = rules.sources_used(parsed)

    twice = a_killed_validation_panel(store)
    once = a_healthy_single_kill(store)

    def evaluate(wo_id: str) -> bool:
        facts = ops.rule_facts(store, store.get_work_order(wo_id), now=db.now(),
                               sources=sources, project="proj_a")
        return bool(rules.evaluate(parsed, facts).matched)

    assert evaluate(twice["id"]) is True
    assert evaluate(once["id"]) is False


def _build(make, store, project):
    """Two shapes of builder — one needs the project path for Neo's DB"""
    if len(inspect.signature(make).parameters) == 2:
        return make(store, project)
    return make(store)


@pytest.mark.parametrize("gap_class,make_bad,_make_good",
                         SEEDS, ids=[s[0] for s in SEEDS])
def test_each_seed_rule_fires_on_the_situation_it_was_written_for(
        started, spec, store, central, project, gap_class, make_bad, _make_good):
    retract_all_but(central, gap_class)
    det = detector_for(central, gap_class)
    wo = _build(make_bad, store, project)

    counts = tick(started, spec, store)

    fires = fires_for(central, det["id"], wo["id"])
    assert len(fires) == 1, f"{gap_class} did not fire on its own situation: {counts}"
    assert fires[0]["outcome"] == rules.RECORDED
    assert fires[0]["mode"] == rules.DRY_RUN


@pytest.mark.parametrize("gap_class,_make_bad,make_good",
                         SEEDS, ids=[s[0] for s in SEEDS])
def test_each_seed_rule_is_silent_on_a_healthy_order_of_the_same_status(
        started, spec, store, central, project, gap_class, _make_bad, make_good):
    """Asserted BY `detector_id`, never by a total count: another rule's silence would otherwise hide this..."""
    det = detector_for(central, gap_class)
    wo = _build(make_good, store, project)

    tick(started, spec, store)

    assert fires_for(central, det["id"], wo["id"]) == []


# -- 3. a pair not armed on both halves is INERT


def test_a_pair_that_is_not_armed_on_both_halves_records_a_fire_and_does_nothing_else(
        started, spec, store, central):
    """Neo, question 882, as §6 narrowed it: the pass now branches on the PAIR's `rules.effective_status`"""
    retract_all_but(central, "stale-panel-hold")
    det = detector_for(central, "stale-panel-hold")
    central.conn.execute("UPDATE remedy_rules SET status=? WHERE detector_id=?",
                         (rules.ARMED, det["id"]))
    wo = a_stale_panel_hold(store)

    before = store.list_events(wo["id"], limit=500)
    counts = tick(started, spec, store)
    after = store.list_events(wo["id"], limit=500)

    assert counts["proposed"] == 0 and counts["refused"] == 0
    fires = fires_for(central, det["id"], wo["id"])
    assert len(fires) == 1
    assert fires[0]["mode"] == rules.DRY_RUN, "a half-armed pair wrote mode=armed"
    assert fires[0]["alarm_id"] == ""
    assert after == before, "the pass wrote an event on the order's timeline"
    assert store.alarms_of(wo["id"]) == []
    assert store.queued_messages(wo["id"]) == []
    assert store.pending_approvals(wo["id"]) == []
    assert store.get_work_order(wo["id"])["needs_attention"] == 1  # unchanged by us


# -- 4. dedupe: one fire per standing condition


def test_a_condition_standing_across_two_ticks_is_one_fire(
        started, spec, store, central):
    retract_all_but(central, "stale-panel-hold")
    det = detector_for(central, "stale-panel-hold")
    wo = a_stale_panel_hold(store)

    tick(started, spec, store)
    tick(started, spec, store)

    assert len(fires_for(central, det["id"], wo["id"])) == 1


def test_the_fire_closes_when_the_condition_clears_and_may_open_again(
        started, spec, store, central):
    retract_all_but(central, "stale-panel-hold")
    det = detector_for(central, "stale-panel-hold")
    wo = a_stale_panel_hold(store)

    tick(started, spec, store)
    fire = central.open_rule_fire(det["id"], wo["id"])
    assert fire is not None

    store.clear_attention(wo["id"])                 # the condition no longer holds
    tick(started, spec, store)
    assert central.open_rule_fire(det["id"], wo["id"]) is None
    assert central.get_rule_fire(fire["id"])["cleared_at"] is not None

    store.flag_attention(wo["id"], invariants.VALIDATION_STUCK_BLOCKER)
    backdate_status(store, wo["id"], 2 * HOUR)
    tick(started, spec, store)
    assert len(fires_for(central, det["id"], wo["id"])) == 2


# -- 5. a detector that cannot be read decides nothing


def test_a_detector_whose_condition_no_longer_parses_is_recorded_unreadable_once(
        started, spec, store, central):
    """One row PER TICK, not per order: the fault is in the detector and reporting it per order would..."""
    retract_all_but(central, "stale-panel-hold")
    det = detector_for(central, "stale-panel-hold")
    central.conn.execute(
        "UPDATE detectors SET condition=? WHERE id=?",
        (json.dumps({"field": "moon_phase", "op": "eq", "value": "full"}), det["id"]))
    for _ in range(3):
        store.create_work_order("an order", status="running")

    counts = tick(started, spec, store)

    unreadable = [f for f in central.list_rule_fires(detector_id=det["id"], limit=50)
                  if f["outcome"] == rules.UNREADABLE]
    assert len(unreadable) == 1
    assert counts["unreadable"] == 1


def test_a_detector_that_raises_does_not_stop_the_pass(
        started, spec, store, central, monkeypatch):
    stale = detector_for(central, "stale-panel-hold")
    boom = central.add_detector("explodes", {"field": "status", "op": "eq",
                                             "value": "needs_review"},
                                project="proj_a", source="user")
    wo = a_stale_panel_hold(store)

    real = rules.evaluate

    def blow_up(cond, facts_):
        if cond == json.loads(boom["condition"]):
            raise RuntimeError("a detector that misbehaves")
        return real(cond, facts_)

    monkeypatch.setattr(rules, "evaluate", blow_up)
    counts = tick(started, spec, store)

    assert [f["outcome"] for f in fires_for(central, boom["id"], wo["id"])] == [
        rules.UNREADABLE]
    assert fires_for(central, stale["id"], wo["id"]), "the rest of the pass stopped"
    assert counts["unreadable"] >= 1


def test_an_order_whose_snapshot_cannot_be_built_does_not_stop_the_pass(
        started, spec, store, central, monkeypatch):
    retract_all_but(central, "stale-panel-hold")
    det = detector_for(central, "stale-panel-hold")
    bad = store.create_work_order("the order that cannot be read", status="running")
    good = a_stale_panel_hold(store)

    real = ops.rule_facts

    def refuse(store_, wo, **kw):
        if wo["id"] == bad["id"]:
            raise RuntimeError("no snapshot for you")
        return real(store_, wo, **kw)

    monkeypatch.setattr(ops, "rule_facts", refuse)
    counts = tick(started, spec, store)

    assert fires_for(central, det["id"], good["id"]), "one bad order stopped the pass"
    assert counts["unreadable"] >= 1


# -- 6. the off switch


def test_with_rules_disabled_the_pass_does_not_run_at_all(
        jarvis_home, fake_claude, catalog_off, project):
    """OFF MEANS THE PASS DOES NOT RUN — not "runs in dry run" (spec §5.2)"""
    ops.start_os(str(catalog_off), foreground=True)
    daemon = Daemon(load_catalog(catalog_off))
    spec_ = daemon.catalog.project("proj_a")
    store = ProjectStore(project)
    central = CentralStore()
    try:
        a_stale_panel_hold(store)
        counts = daemon.rules_tick(spec_, store)
        assert central.list_rule_fires(limit=50) == []
        assert counts["enabled"] is False
        assert counts["orders"] == 0
    finally:
        store.close()
        central.close()


def test_rules_list_says_when_the_registry_is_switched_off(jarvis_home, fake_claude,
                                                           catalog_off, project):
    """ABSENT IS NOT ZERO: an off registry prints a sentence, not an empty count"""
    ops.start_os(str(catalog_off), foreground=True)
    data = ops.rules_list()
    assert data["enabled"] is False
    assert "OFF" in data["note"]


def test_rules_list_says_when_the_registry_is_on(started):
    assert ops.rules_list()["enabled"] is True


# -- 7. seeding


def test_the_five_rules_are_seeded_with_their_remedy_rows(started, central):
    seeded = central.list_detectors()
    assert {d["gap_class"] for d in seeded} == {s["detector"]["gap_class"]
                                                for s in rules.seed_rows()}
    for det in seeded:
        assert central.remedy_rules_for(det["id"]), f"{det['id']} has no remedy row"
        assert det["status"] == rules.DRY_RUN
        assert det["project"] == ""          # fleet-wide: these are OS gaps


def test_the_version_guard_actually_guards_and_a_second_open_re_seeds_nothing(
        started, central):
    """The guard is a TEXT comparison against an INTEGER version, and it was wrong"""
    det = detector_for(central, "red-base-inherited")
    central.conn.execute("DELETE FROM remedy_rules WHERE detector_id=?", (det["id"],))
    central.conn.execute("DELETE FROM detectors WHERE id=?", (det["id"],))
    central.conn.commit()

    again = CentralStore()
    try:
        assert again.get_detector(det["id"]) is None
    finally:
        again.close()


def test_a_database_already_at_version_one_gains_exactly_the_sixth_seed(started, central):
    """The addendum to §5: the pass returns early on a matching version"""
    sixth = detector_for(central, "killed-validation-panel")
    retracted = detector_for(central, "stale-panel-hold")
    central.retract_detector(retracted["id"], "the user disagreed")
    central.conn.execute("DELETE FROM remedy_rules WHERE detector_id=?", (sixth["id"],))
    central.conn.execute("DELETE FROM detectors WHERE id=?", (sixth["id"],))
    central.set_state("detectors_seed", "1")
    central.conn.commit()

    again = CentralStore()
    try:
        assert again.get_detector(sixth["id"])["gap_class"] == "killed-validation-panel"
        assert again.remedy_rules_for(sixth["id"])
        assert again.get_state("detectors_seed") == "2"
        assert again.get_detector(retracted["id"])["status"] == rules.RETRACTED, (
            "re-seeding resurrected a detector a person retracted")
        assert len(again.list_detectors(include_retired=True)) == 6
    finally:
        again.close()


def test_seeding_is_idempotent_and_a_retracted_seed_stays_retracted(started, central):
    det = detector_for(central, "stale-panel-hold")
    central.retract_detector(det["id"], "the user disagreed")

    again = CentralStore()
    try:
        again._seed_detectors()
        again._seed_detectors()
        rows = again.list_detectors(include_retired=True)
        assert len([d for d in rows if d["gap_class"] == "stale-panel-hold"]) == 1
        assert again.get_detector(det["id"])["status"] == rules.RETRACTED
    finally:
        again.close()


# -- 8. the three assertions §3.5 could not make

#: The seed rows whose `primitive` names something `remedies.REMEDIES` does not have
KNOWN_MISSING_PRIMITIVES = {"retry_neo_question"}

#: The seed rows whose parameters do not satisfy their primitive's declared schema
KNOWN_PARAM_MISMATCHES = {"drop_hold", "raise_attention"}


def seed_remedy_rows():
    for seed in rules.seed_rows():
        for row in seed["remedies"]:
            yield seed["detector"]["gap_class"], row


def test_every_seed_primitive_is_in_the_registry_except_the_one_neo_908_dropped():
    missing = {row["primitive"] for _gap, row in seed_remedy_rows()
               if row["primitive"] not in remedies.REMEDIES}
    assert missing == KNOWN_MISSING_PRIMITIVES, (
        f"the set of seed primitives with no registry entry changed: {missing}. "
        f"The registry ships {sorted(remedies.REMEDIES)}. Either a seed row was fixed "
        f"(delete it from KNOWN_MISSING_PRIMITIVES) or one was broken.")


def test_every_seed_parameter_set_that_can_be_graded_satisfies_its_schema():
    bad = {}
    for gap, row in seed_remedy_rows():
        if row["primitive"] in KNOWN_MISSING_PRIMITIVES:
            continue          # no registry entry, so there is no schema to grade against
        problems = rules.validate_params(row["primitive"], json.loads(row["params"]))
        if problems:
            bad[row["primitive"]] = (gap, problems)
    assert set(bad) == KNOWN_PARAM_MISMATCHES, (
        f"the set of seed rows failing their primitive's schema changed: {bad}")


def test_seed_rule_5_names_a_reason_key_that_is_not_a_real_attention_template():
    """THE RECONCILIATION §5.3 ASKS FOR, and it FAILS — loudly, by name"""
    rule5 = [row for gap, row in seed_remedy_rows()
             if gap == "overtaken-release-order"]
    assert len(rule5) == 1
    params = json.loads(rule5[0]["params"])

    assert "reason_key" in params and "template" not in params
    assert params["reason_key"] not in remedies.ATTENTION_TEMPLATES, (
        f"the real closed tuple of template keys is "
        f"{tuple(remedies.ATTENTION_TEMPLATES)}")
    problems = rules.validate_params("raise_attention", params)
    assert problems, "the schema check stopped catching this"
    assert any("reason_key" in p for p in problems)


def test_resolve_returns_a_resolution_for_every_one_of_the_five_seed_rules(store):
    """The third of §3.5's three, and this one PASSES: `resolve` pairs a matching detector with its remedy..."""
    seeds = rules.seed_rows()
    detectors = [s["detector"] for s in seeds]
    remedy_rows = {s["detector"]["id"]: s["remedies"] for s in seeds}
    for seed in seeds:
        cond = rules.parse_condition(seed["detector"]["condition"])
        # Facts built to satisfy this seed's OWN condition, so `resolve` reaches its remedy rows
        values = {}
        for leaf in rules._leaves(cond):
            field, op, value = leaf["field"], leaf["op"], leaf["value"]
            if op == rules.EQ:
                values[field] = value
            elif op == rules.IN:
                values[field] = value[0]
            elif op == rules.GTE:
                values[field] = value + 1
            elif op == rules.LTE:
                values[field] = value - 1
            elif op == rules.EXISTS:
                values[field] = "x" * 40
            elif op == rules.COUNT_GTE:
                values[field] = {value["kind"]: value["value"]}
        facts = rules.Facts(project="proj_a", order_id="wo-1",
                            order_kind="work_order", values=values, now=db.now())
        out = rules.resolve(detectors, remedy_rows, facts)
        mine = [r for r in out
                if r.detector["id"] == seed["detector"]["id"]]
        assert mine, f"{seed['detector']['gap_class']} resolved to nothing"
        assert all(isinstance(r, rules.Resolution) for r in mine)


# -- 9. the caps, and the measured cost


def test_the_order_cap_stops_the_pass_and_says_that_it_did(
        started, spec, store, central, monkeypatch):
    from jarvis import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "EVAL_MAX_ORDERS", 2)
    for _ in range(5):
        store.create_work_order("an order", status="running")

    counts = tick(started, spec, store)

    assert counts["orders"] == 2
    assert counts["capped"] == "orders"


def test_one_tick_over_twenty_orders_is_measured(started, spec, store, central, capsys):
    """The PR body must state the wall clock of one `rules_tick` over N orders (§5.2.3)"""
    n = 20
    for i in range(n):
        wo = store.create_work_order(f"order {i}", status="running")
        store.add_event(wo["id"], "dispatched", {})

    t0 = time.monotonic()
    counts = tick(started, spec, store)
    elapsed = time.monotonic() - t0

    assert counts["orders"] == n
    with capsys.disabled():
        print(f"\nrules_tick over {n} orders, {counts['detectors']} detectors: "
              f"{elapsed * 1000:.0f} ms")
    assert elapsed < 10.0


# -- 10. the cursor: a cap truncates a pass, it does not strand the tail


def evaluated_ids(monkeypatch) -> list[str]:
    """The orders the pass actually built a snapshot for, in iteration order"""
    seen: list[str] = []
    real = ops.rule_facts

    def spy(store_, wo, **kw):
        seen.append(str(wo["id"]))
        return real(store_, wo, **kw)

    monkeypatch.setattr(ops, "rule_facts", spy)
    return seen


def test_every_open_order_is_evaluated_within_a_bounded_number_of_ticks(
        started, spec, store, central, monkeypatch):
    """BOUNDED, not eventual: ceil(N / cap) ticks to cover N orders"""
    from jarvis import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "EVAL_MAX_ORDERS", 10)
    n = 25
    wanted = {str(store.create_work_order(f"order {i}", status="running")["id"])
              for i in range(n)}
    seen = evaluated_ids(monkeypatch)

    for _ in range(math.ceil(n / 10) + 1):
        tick(started, spec, store)

    missed = wanted - set(seen)
    assert not missed, f"{len(missed)} of {n} orders were never evaluated: {missed}"


def test_the_seconds_budget_resumes_at_the_order_after_the_last_one_evaluated(
        started, spec, store, central, monkeypatch):
    """The break is mid-pass, so the cursor has to be advanced per order and not per pass"""
    from jarvis import daemon as daemon_mod

    created = [str(store.create_work_order(f"order {i}", status="running")["id"])
               for i in range(6)]
    seen = evaluated_ids(monkeypatch)

    calls = {"n": 0}

    def monotonic() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] <= 3 else 10_000.0

    class Clock:
        def __getattr__(self, name):
            return getattr(time, name)

    clock = Clock()
    clock.monotonic = monotonic                     # type: ignore[attr-defined]
    monkeypatch.setattr(daemon_mod, "time", clock)

    counts = tick(started, spec, store)
    assert counts["capped"] == "seconds"
    first = list(seen)
    assert first == created[:2], f"expected the first two orders, got {first}"

    monkeypatch.setattr(daemon_mod, "time", time)
    seen.clear()
    tick(started, spec, store)

    assert seen[0] == created[2], (
        f"the next pass restarted at {seen[0]} instead of resuming at {created[2]}")


def test_a_cursor_naming_an_order_that_is_no_longer_open_does_not_skip_the_pass(
        started, spec, store, central, monkeypatch):
    """A settled, hidden or deleted order can be the cursor: it was open when the last pass read it"""
    gone = store.create_work_order("an order that settled", status="running")
    store.set_status(gone["id"], "completed")
    open_ids = {str(store.create_work_order(f"order {i}", status="running")["id"])
                for i in range(3)}
    started.rules_cursor["proj_a"] = (float(gone["created_at"]), str(gone["id"]))
    seen = evaluated_ids(monkeypatch)

    counts = tick(started, spec, store)

    assert set(seen) == open_ids, f"the pass evaluated {seen}, counts={counts}"


def test_a_settled_cursor_resumes_at_the_order_created_after_it(
        started, spec, store, central, monkeypatch):
    """The cursor is the SORT KEY, not the id"""
    def order(wo_id: str, created_at: float, status: str = "running") -> str:
        wo = store.create_work_order(wo_id, status=status, wo_id=wo_id)
        store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                           (created_at, wo_id))
        return str(wo["id"])

    t0 = db.now() - 10 * HOUR
    order("wo-ccc", t0 + 1.0)
    order("wo-ddd", t0 + 1.5, status="completed")        # the cursor, settled since
    order("wo-bbb", t0 + 2.0)
    order("wo-aaa", t0 + 3.0)
    started.rules_cursor["proj_a"] = (t0 + 1.5, "wo-ddd")
    seen = evaluated_ids(monkeypatch)

    tick(started, spec, store)

    assert seen == ["wo-bbb", "wo-aaa", "wo-ccc"], (
        f"expected the order created after the cursor first, got {seen}")


# -- 11. the pass runs AFTER the invariants, within one tick


def test_the_rules_pass_runs_after_the_invariants_within_one_tick(started, monkeypatch):
    """Spec §5.1: `check_invariants` REPAIRS what is unambiguous on this same tick"""
    calls: list[str] = []

    def recorder(name: str):
        def run(*_a, **_k):
            calls.append(name)
            return {}
        return run

    monkeypatch.setattr(started, "check_invariants", recorder("check_invariants"))
    monkeypatch.setattr(started, "rules_tick", recorder("rules_tick"))

    started.tick()

    assert calls == ["check_invariants", "rules_tick"], (
        f"the rules pass did not run after the invariants: {calls}")
