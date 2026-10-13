"""The evaluation pass: `Daemon.rules_tick`, the fact snapshot, and the five seed rules.

docs/superpowers/specs/2026-09-27-self-evolution.md §5.

**THE SITUATIONS HERE ARE SYNTHESISED, NOT REPLAYED.** The five seed rules each came out
of a real incident with an issue behind it (#786, #788, #806, #793, #784), and the
strongest evidence that a condition selects the situation it was written for would be a
replay of those orders' own rows. Those orders are not in a test fixture and the dev
databases are not a test dependency, so every situation below is BUILT from the store
fixtures to match the issue's shape. That is weaker evidence than a replay and the pull
request says so: a reader must not assume a replay happened. What these tests do prove is
that each condition selects the shape it names and stays silent on a HEALTHY order of the
same status — which is the half that carries the weight, because another rule's silence
would otherwise hide this one's noise.

Five properties beyond the ten seed tests:

1. **Laziness is asserted by NAME.** `holds.held` walks up to `holds._EVENT_LIMIT` events
   per order; the call-count tests below wrap that exact function, so a later refactor
   that reintroduces the walk fails loudly rather than quietly (spec §5.2.3). Since
   docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md
   the `state_durations` source reads the holds too, so the claim is the pair the tests
   make: NO walk when the live conditions name neither a hold field nor a time field, and
   AT MOST ONE walk per order when they name both.
2. **`armed` is inert in this release.** Nothing in the pass branches on
   `detectors.status`, so an armed detector reaching it before §6 lands records a fire
   and touches NOTHING else — no alarm, no message, no flag, no gate, and no event on the
   order's own timeline (Neo, question 882).
3. **A standing condition is one fire.** Not one per tick, and it may fire again only
   after it has cleared.
4. **Nothing that could not be read decides anything.** A detector that raises is
   recorded `unreadable` and the rest of the pass still runs.
5. **NO ORDER IS STARVED.** Both caps — `EVAL_MAX_ORDERS` and `EVAL_MAX_SECONDS` — cut a
   single oldest-first pass at the same place on every tick, so the tail would never be
   evaluated at all. The in-memory cursor rotates the start, so every eligible order is
   reached within `ceil(N / EVAL_MAX_ORDERS) + 1` ticks and a seconds-budget break resumes
   at the order after the last one evaluated.
"""

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

# -- the harness -----------------------------------------------------------------------


@pytest.fixture()
def catalog_on(tmp_path, project):
    """A catalog with the evaluation pass SWITCHED ON. `rules.enabled` ships false."""
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
    """The seeded detector for one gap class. Seeding is `CentralStore`'s own, so this
    reads what a real `os.db` holds rather than registering a copy."""
    found = central.list_detectors(gap_class=gap_class)
    assert found, f"no seeded detector for {gap_class}"
    return found[0]


def retract_all_but(central, keep: str) -> None:
    """Leave exactly one seed detector live. The negative tests assert BY detector id,
    but the positive ones read a count, and a second rule firing on the same order would
    make that count say something it does not mean."""
    for det in central.list_detectors():
        if det["gap_class"] != keep:
            central.retract_detector(det["id"], "not under test")


def backdate_status(store, wo_id: str, seconds: float) -> None:
    """Put this order's status span `seconds` into the past. `set_status` stamps now and
    takes no ts, so the span is moved after the fact rather than a private writer called."""
    store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE order_id=?",
                       (db.now() - seconds, wo_id))


def backdate_events(store, wo_id: str, seconds: float) -> None:
    ts = db.now() - seconds
    store.conn.execute("UPDATE wo_events SET ts=? WHERE wo_id=?", (ts, wo_id))


def fires_for(central, detector_id: str, order_id: str = "") -> list[dict]:
    return [f for f in central.list_rule_fires(detector_id=detector_id, limit=100)
            if not order_id or f["order_id"] == order_id]


# -- 1. the fact snapshot is built LAZILY, and `holds.held` is named ---------------------


def test_holds_are_never_read_when_no_live_detector_names_a_hold_or_time_field(
        started, spec, store, central, monkeypatch):
    """Spec §5.2.3, the white-box assertion it asks for by name.

    `holds.held` walks up to `holds._EVENT_LIMIT` events per order, so a tick whose live
    conditions can be answered from the work order's own row must not call it once.

    THE SEED CONDITIONS DO REACH IT, and that is not this pass being wasteful: every
    reading of `ops.state_durations` reads the holds, because
    docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md
    made the active basis part of the reading itself, and four of the five seed rules ask
    about time in status. The test below pins the part this pass does control — that the
    walk happens AT MOST ONCE per order, however many sources name it.
    """
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
    """Both the `holds` source and the `state_durations` source need the same reading, so
    the snapshot shares one. Two walks per order per tick is the regression this pins."""
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
    """ABSENT IS A THIRD THING. A source nobody asked for contributes no key at all —
    never `None`, never `False`, which the evaluator treats as different answers."""
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
    """`seconds_in_status_active` landed on main while this branch was open, under the
    `state_durations` source. The subtraction is off the holds THAT reading already
    carries, so naming the field costs no walk beyond the one `state_durations` makes.
    """
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
    """`rules.facts` is the CONTRACT and `ops.rule_facts` is the implementation. The
    grammar module stays a leaf at import time; see the comment on `rules.facts`."""
    wo = store.create_work_order("an order", status="running")
    row = store.get_work_order(wo["id"])
    now = db.now()
    through_contract = rules.facts(store, row, now=now,
                                   sources=frozenset({"work_order"}))
    direct = ops.rule_facts(store, row, now=now, sources=frozenset({"work_order"}))
    assert through_contract.values == direct.values
    assert through_contract.order_kind == "work_order"


# -- 2. the five seed rules: a positive and a negative each -----------------------------


def a_stale_panel_hold(store) -> dict:
    """Issue #786/#813: `needs_review`, flagged unsatisfiable, newest round PASSED."""
    wo = store.create_work_order("a judged order")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], invariants.VALIDATION_STUCK_BLOCKER)
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.close_validation_round(rnd["id"], "passed")
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_healthy_needs_review(store) -> dict:
    """The same status, nothing wrong: no flag, and the round is still open."""
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
    """Same status, and the question is with Neo with an attempt spent — which is the
    OS working, not a reviewer that was never reached."""
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
    """Issue #806: parked, auto-merge held on `sha_moved`, newest round PASSED."""
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
    """Same status, merging normally: no hold event at all."""
    wo = store.create_work_order("a parked order that is merging")
    store.set_status(wo["id"], "waiting_pr_merge", pr_url="https://example/pr/2")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.set_validation_head(rnd["id"], JUDGED)
    store.close_validation_round(rnd["id"], "passed")
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_red_base_inherited(store) -> dict:
    """Issue #793: parked, auto-merge held on `base_red`, half an hour gone."""
    wo = store.create_work_order("a parked order on a red base")
    store.set_status(wo["id"], "waiting_pr_merge", pr_url="https://example/pr/3")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    store.close_validation_round(rnd["id"], "passed")
    store.add_event(wo["id"], "automerge_held",
                    {"code": "base_red", "head_sha": "c" * 40, "round": 1})
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def an_overtaken_release_order(store) -> dict:
    """Issue #784: an open order INV-WORK-LANDED has already caught, idle an hour."""
    wo = store.create_work_order("ship the fix")
    store.add_event(wo["id"], "invariant",
                    {"invariant": "INV-WORK-LANDED", "detail": "already on main",
                     "repaired": False, "repair": ""})
    backdate_events(store, wo["id"], 2 * HOUR)
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


def a_healthy_pending_order(store) -> dict:
    """Same status, idle just as long, and nothing has landed under it."""
    wo = store.create_work_order("a pending order nobody has overtaken")
    store.add_event(wo["id"], "created", {})
    backdate_events(store, wo["id"], 2 * HOUR)
    backdate_status(store, wo["id"], 2 * HOUR)
    return store.get_work_order(wo["id"])


SEEDS = [
    ("stale-panel-hold", a_stale_panel_hold, a_healthy_needs_review),
    ("unreachable-neo-question", an_unreachable_neo_question, a_healthy_waiting_input),
    ("catch-up-round-burn", a_catch_up_round_burn, a_healthy_parked_order),
    ("red-base-inherited", a_red_base_inherited, a_healthy_parked_order),
    ("overtaken-release-order", an_overtaken_release_order, a_healthy_pending_order),
]


def _build(make, store, project):
    """Two shapes of builder — one needs the project path for Neo's DB.

    Chosen by SIGNATURE: a `try`/`except TypeError` around the two-argument call also
    swallows a genuine TypeError raised INSIDE the builder and retries it with the wrong
    arity, reporting the second failure instead of the real one.
    """
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
    """Asserted BY `detector_id`, never by a total count: another rule's silence would
    otherwise hide this one's noise (spec §5.3)."""
    det = detector_for(central, gap_class)
    wo = _build(make_good, store, project)

    tick(started, spec, store)

    assert fires_for(central, det["id"], wo["id"]) == []


# -- 3. `armed` is INERT in this release ------------------------------------------------


def test_an_armed_detector_records_a_fire_and_does_nothing_else(
        started, spec, store, central):
    """Neo, question 882. Nothing in this pass branches on `detectors.status`, so the
    order in which this child and the arming child merge cannot make anything act."""
    retract_all_but(central, "stale-panel-hold")
    det = detector_for(central, "stale-panel-hold")
    central.conn.execute("UPDATE detectors SET status=? WHERE id=?",
                         (rules.ARMED, det["id"]))
    central.conn.execute("UPDATE remedy_rules SET status=? WHERE detector_id=?",
                         (rules.ARMED, det["id"]))
    wo = a_stale_panel_hold(store)

    before = store.list_events(wo["id"], limit=500)
    tick(started, spec, store)
    after = store.list_events(wo["id"], limit=500)

    fires = fires_for(central, det["id"], wo["id"])
    assert len(fires) == 1
    assert fires[0]["mode"] == rules.DRY_RUN, "an armed detector wrote mode=armed"
    assert fires[0]["alarm_id"] == ""
    assert after == before, "the pass wrote an event on the order's timeline"
    assert store.alarms_of(wo["id"]) == []
    assert store.queued_messages(wo["id"]) == []
    assert store.pending_approvals(wo["id"]) == []
    assert store.get_work_order(wo["id"])["needs_attention"] == 1  # unchanged by us


# -- 4. dedupe: one fire per standing condition ----------------------------------------


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


# -- 5. a detector that cannot be read decides nothing ----------------------------------


def test_a_detector_whose_condition_no_longer_parses_is_recorded_unreadable_once(
        started, spec, store, central):
    """One row PER TICK, not per order: the fault is in the detector and reporting it
    per order would multiply one defect by the size of the fleet."""
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


# -- 6. the off switch ------------------------------------------------------------------


def test_with_rules_disabled_the_pass_does_not_run_at_all(
        jarvis_home, fake_claude, catalog_off, project):
    """OFF MEANS THE PASS DOES NOT RUN — not "runs in dry run" (spec §5.2)."""
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
    """ABSENT IS NOT ZERO: an off registry prints a sentence, not an empty count."""
    ops.start_os(str(catalog_off), foreground=True)
    data = ops.rules_list()
    assert data["enabled"] is False
    assert "OFF" in data["note"]


def test_rules_list_says_when_the_registry_is_on(started):
    assert ops.rules_list()["enabled"] is True


# -- 7. seeding ------------------------------------------------------------------------


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
    """The guard is a TEXT comparison against an INTEGER version, and it was wrong.

    `os_state` is a text column, so `get_state` hands back `"1"` while
    `rules.SEED_VERSION` is `1` — an unguarded `==` is False for ever and every
    `CentralStore()` re-runs the seed. `INSERT OR IGNORE` hides that completely until a
    row is DELETED, at which point the next open silently restores it. Asserted the only
    way that distinguishes the two: delete a row, open again, and look.
    """
    det = detector_for(central, "red-base-inherited")
    central.conn.execute("DELETE FROM remedy_rules WHERE detector_id=?", (det["id"],))
    central.conn.execute("DELETE FROM detectors WHERE id=?", (det["id"],))
    central.conn.commit()

    again = CentralStore()
    try:
        assert again.get_detector(det["id"]) is None
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


# -- 8. the three assertions §3.5 could not make ---------------------------------------
#
# Spec §5.3: this is the only section that depends on both §3 (the seed rows) and §4 (the
# primitives), so it is the only one that can carry these. They are NOT optional.
#
# TWO OF THE THREE FAIL AGAINST WHAT §3 AND §4 ACTUALLY SHIPPED, and the tests below say
# so by name rather than being softened. Neither seed rows nor the primitive registry are
# this section's to edit, so what is asserted is the EXACT set of mismatches — which
# means the day either side is fixed, this test fails and is read by a human, instead of
# a `xfail` that would let the gap ship silently for ever.

#: The seed rows whose `primitive` names something `remedies.REMEDIES` does not have.
#: `retry_neo_question` was DROPPED from §4 on Neo question 908 (`NeoStore` has no
#: `requeue_failed`), and seed rule 2 names it anyway: neither side could see the other's
#: text. The rule is inert rather than wrong — `rules.resolve` still resolves it and
#: `can_apply` stays `None`, which is "not determinable" and not "ready" — but nothing
#: can ever act on it.
KNOWN_MISSING_PRIMITIVES = {"retry_neo_question"}

#: The seed rows whose parameters do not satisfy their primitive's declared schema.
#: Both are §4 landing after §3 was written:
#:   * `drop_hold` gained two REQUIRED parameters (`cause`, `reason`); seed rule 1 passes
#:     `{}`.
#:   * `raise_attention` takes `template`, a key of the closed `ATTENTION_TEMPLATES`
#:     table; seed rule 5 passes `reason_key="overtaken-release-order"`, which is the
#:     wrong PARAMETER NAME and a value that is not in the table either.
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
    """THE RECONCILIATION §5.3 ASKS FOR, and it FAILS — loudly, by name.

    Seed rule 5 ships `params={"reason_key": "overtaken-release-order"}`. §4 defined
    `raise_attention` to take `template`, whose value must be one of the closed tuple in
    `remedies.ATTENTION_TEMPLATES`. So there are two faults, not one: the parameter name
    is wrong AND the value is outside the closed set.

    NEITHER IS FIXED HERE. The seed row belongs to §3 and the template table to §4; this
    section owns only the proof that they do not meet. What must never happen is the
    grammar or the tuple being WIDENED to make them meet — `raise_attention` renders its
    reason from a closed table and never relays one, and a table that accepts a rule's
    own string is a free-text attention reason with extra steps.
    """
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
    """The third of §3.5's three, and this one PASSES: `resolve` pairs a matching
    detector with its remedy rows whatever the registry thinks of the primitive."""
    seeds = rules.seed_rows()
    detectors = [s["detector"] for s in seeds]
    remedy_rows = {s["detector"]["id"]: s["remedies"] for s in seeds}
    for seed in seeds:
        cond = rules.parse_condition(seed["detector"]["condition"])
        # Facts built to satisfy this seed's OWN condition, so `resolve` reaches its
        # remedy rows. Every leaf is read off the parsed condition rather than restated,
        # so a seed whose condition changes is still exercised.
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


# -- 9. the caps, and the measured cost -------------------------------------------------


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
    """The PR body must state the wall clock of one `rules_tick` over N orders (§5.2.3).

    Measured here rather than asserted tightly: this is a deliverable for a reviewer, so
    the bound is loose enough never to flake and the number is printed.
    """
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


# -- 10. the cursor: a cap truncates a pass, it does not strand the tail -----------------
#
# Both caps stop at the same point on every tick, so without a cursor the orders past it
# are never evaluated — and a stranded NEW order is exactly what this feature exists to
# notice. The user rejected assumption #5 of wo-cdee6f9b on that ground and Neo objected
# on the same one; spec §5.2.


def evaluated_ids(monkeypatch) -> list[str]:
    """The orders the pass actually built a snapshot for, in iteration order. `counts`
    carries a number and this question is about WHICH, so the seam is `ops.rule_facts`."""
    seen: list[str] = []
    real = ops.rule_facts

    def spy(store_, wo, **kw):
        seen.append(str(wo["id"]))
        return real(store_, wo, **kw)

    monkeypatch.setattr(ops, "rule_facts", spy)
    return seen


def test_every_open_order_is_evaluated_within_a_bounded_number_of_ticks(
        started, spec, store, central, monkeypatch):
    """BOUNDED, not eventual: ceil(N / cap) ticks to cover N orders, plus one for the
    partial lap the cursor may start mid-way through."""
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
    """The break is mid-pass, so the cursor has to be advanced per order and not per
    pass. Only the daemon's clock is faked — patching `time.monotonic` itself would
    reach pytest."""
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
    """A settled, hidden or deleted order can be the cursor: it was open when the last
    pass read it. The fallback starts at the first order created after it."""
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
    """The cursor is the SORT KEY, not the id. Ids are random, so a lexical `id > cursor`
    fallback resumes at an arbitrary order — here the ids sort in the OPPOSITE order to
    `created_at`, which makes that bug visible instead of coincidentally right."""
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


# -- 11. the pass runs AFTER the invariants, within one tick -----------------------------


def test_the_rules_pass_runs_after_the_invariants_within_one_tick(started, monkeypatch):
    """Spec §5.1: `check_invariants` REPAIRS what is unambiguous on this same tick, so a
    detector pass running before it fires on conditions the OS was about to fix itself.

    Pinned by OBSERVATION, not by reading the source: both methods are replaced on the
    daemon and record their own name as `tick` calls them.
    """
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
