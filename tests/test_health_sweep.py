"""The health sweep: what makes the OS look at an open unit, and what it raises (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import catalog, db, ops, probes as probes_mod, supervisor
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.health import due, fingerprint
from jarvis.project_store import NO_TURN, ProjectStore

CFG = catalog.SupervisorConfig()

#: How the fake `claude` tells a sweep from a cost review, and how this file does
CHECKLIST = probes_mod.render_checklist(probes_mod.DEFAULT_PROBES).splitlines()[0]


#: The floors, shrunk to their legal minimum
FLOOR_MINUTES = 1
STEP_MINUTES = 2 * FLOOR_MINUTES


def _enable(catalog_file, **overrides) -> None:
    """Turn BOTH switches on"""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True, "health_enabled": True,
                                "health_min_interval_minutes": FLOOR_MINUTES,
                                "health_stale_minutes": FLOOR_MINUTES, **overrides}
    catalog_file.write_text(json.dumps(data))


class _Clock:
    """A hand-advanced `db.now`, installed for the whole test"""

    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, minutes: float = STEP_MINUTES) -> float:
        self.at += minutes * 60.0
        return self.at


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock(time.time())
    monkeypatch.setattr("jarvis.db.now", c)
    return c


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


def _health_calls(fake_claude) -> list[dict]:
    return [call for call in fake_claude.calls
            if CHECKLIST in (call.get("system_prompt_seen") or "")]


def _agent_calls(kind: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        return [dict(r) for r in central.conn.execute(
            "SELECT * FROM agent_calls WHERE kind=?", (kind,)).fetchall()]
    finally:
        central.close()


def _sweep(daemon, clock, timeout: float = 20.0) -> None:
    """One sweep tick, waited out on the daemon's OWN guard rather than on a sleep"""
    clock.advance()
    daemon.tick_count = 1
    daemon.health_tick()
    deadline = time.monotonic() + timeout
    while daemon.health_sweeping and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not daemon.health_sweeping, "the health sweep never finished"


def _wo(store, title="the quiet one", **fields) -> str:
    return store.create_work_order(title, **fields)["id"]


def _feature(store, title="the feature", status="executing", carrier=True) -> str:
    """One feature order and, by default, a SETTLED carrier for it"""
    fo_id = store.create_feature_order(title)["id"]
    store.set_feature_status(fo_id, status)
    if carrier:
        store.create_work_order("carries it", parent_id=fo_id, kind="manager",
                                status="completed")
    return fo_id


def _reviews(store, kind="work_order", subject_id="") -> list[dict]:
    return store.health_reviews_of(kind, subject_id)


def _findings(store) -> list[dict]:
    return [a for a in store.alarms_across() if a["source"] == "health"]


# -- `due`: the pure spend decision


def test_a_unit_younger_than_the_floor_is_not_looked_at_at_all():
    """The floor applies to the FIRST look too: a unit created moments ago has nothing to say yet"""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30)
    minute = 60.0
    assert due(None, "fp", cfg, now=29 * minute, created=0.0) is None
    assert due(None, "fp", cfg, now=31 * minute, created=0.0) == "first-look"


def test_a_moved_fingerprint_is_changed_and_a_still_one_is_stale_and_neither_is_early():
    """The three triggers and the two floors"""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30,
                                   health_stale_minutes=720)
    minute = 60.0
    old = {"ts": 0.0, "fingerprint": "before"}

    assert due(old, "after", cfg, now=29 * minute, created=0.0) is None
    assert due(old, "after", cfg, now=31 * minute, created=0.0) == "changed"
    # Unchanged and young: nothing has happened
    assert due(old, "before", cfg, now=31 * minute, created=0.0) is None
    assert due(old, "before", cfg, now=721 * minute, created=0.0) == "stale"


def test_the_stale_clause_is_what_earns_the_feature():
    """A pure delta trigger never fires on "nothing has changed for three days\""""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30,
                                   health_stale_minutes=720)
    three_days = 3 * 24 * 60 * 60.0
    assert due({"ts": 0.0, "fingerprint": "frozen"}, "frozen", cfg,
               now=three_days, created=0.0) == "stale"


# -- `fingerprint`: cheap, deterministic, and it moves when the unit does


def test_the_fingerprint_moves_for_every_thing_a_work_order_can_do(store):
    """Each component asserted by MUTATING it"""
    wo_id = _wo(store)
    subject = {"kind": "work_order", "row": store.get_work_order(wo_id)}
    first = fingerprint(store, subject)

    def moved(**_unused) -> str:
        return fingerprint(store, {"kind": "work_order",
                                   "row": store.get_work_order(wo_id)})

    store.update_work_order(wo_id, status="running")
    after_status = moved()
    assert after_status != first

    store.create_turn(wo_id, "dispatch", "go")
    after_turn = moved()
    assert after_turn != after_status

    store.add_event(wo_id, "note", {})
    after_event = moved()
    assert after_event != after_turn

    store.queue_message(wo_id, "have you tried turning it off")
    after_message = moved()
    assert after_message != after_event

    store.add_assumption(wo_id, "I assumed the port was 8787")
    assert moved() != after_message

    # And it is a FUNCTION of the state
    assert moved() == moved()


def test_the_os_watching_a_unit_does_not_count_as_the_unit_moving(store):
    """The subtlest rule in the section"""
    from jarvis.project_store import ALARM_EVENT_KINDS

    wo_id = _wo(store)
    subject = {"kind": "work_order", "row": store.get_work_order(wo_id)}
    before = fingerprint(store, subject)

    for kind in ALARM_EVENT_KINDS:
        store.add_event(wo_id, kind, {})
    store.flag_attention(wo_id, "look at me")
    after = {"kind": "work_order", "row": store.get_work_order(wo_id)}
    assert fingerprint(store, after) == before

    # The positive partner, in the same test: a fingerprint that ignored EVERY event would pass the...
    store.add_event(wo_id, "turn_started", {})
    assert fingerprint(store, {"kind": "work_order",
                               "row": store.get_work_order(wo_id)}) != before


def test_a_features_fingerprint_follows_its_children_and_its_rounds(store):
    fo_id = _feature(store)
    child = store.create_work_order("a child", parent_id=fo_id)["id"]
    subject = {"kind": "feature_order", "row": store.get_feature_order(fo_id)}
    first = fingerprint(store, subject)

    store.update_work_order(child, status="failed")
    second = fingerprint(store, {"kind": "feature_order",
                                 "row": store.get_feature_order(fo_id)})
    assert second != first

    store.open_validation_round(fo_id=fo_id, fingerprint="abc")
    assert fingerprint(store, {"kind": "feature_order",
                               "row": store.get_feature_order(fo_id)}) != second


# -- it ships off, and the pin needs its sibling


def test_with_the_catalog_untouched_the_sweep_does_nothing_at_all(
        started, fake_claude, store, clock):
    """UNFALSIFIABLE ON ITS OWN — green on an empty diff and green on a working one"""
    _wo(store, status="running")
    _feature(store)

    _sweep(started(), clock)

    assert _health_calls(fake_claude) == []
    assert _agent_calls("health") == []
    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 0
    assert store.alarms_across() == []


def test_with_both_switches_on_the_same_fixture_produces_all_four(
        started, catalog_file, fake_claude, store, clock):
    """The sibling that makes the pin above falsifiable"""
    _enable(catalog_file)
    _wo(store, status="running")

    _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1
    assert len(_agent_calls("health")) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 1
    assert len(_findings(store)) == 1


def test_the_cost_review_switch_alone_arms_nothing(started, catalog_file, fake_claude,
                                                   store, clock):
    """`health_enabled` sits ON TOP of `enabled`: a project may want a reviewer for its cost alarms..."""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True,
                                "health_min_interval_minutes": FLOOR_MINUTES}
    catalog_file.write_text(json.dumps(data))
    _wo(store, status="running")

    _sweep(started(), clock)

    assert _health_calls(fake_claude) == []
    assert _findings(store) == []


# -- the done condition


def test_one_tick_makes_one_call_and_leaves_one_finding_on_the_record(
        started, catalog_file, fake_claude, store, clock):
    """ONE MODEL CALL PER UNIT, NOT ONE PER PROBE"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")

    _sweep(started(), clock)

    (call,) = _health_calls(fake_claude)
    system = call["system_prompt_seen"]
    armed = probes_mod.armed(CFG.probes, "work_order")
    assert len(armed) > 1, "one probe cannot discriminate one call per unit from one per probe"
    for probe in armed:
        assert f"## {probe.id} — {probe.title}" in system

    (alarm,) = _findings(store)
    assert alarm["source"] == "health"
    assert alarm["probe"] == armed[0].id
    assert alarm["kind"] == armed[0].id
    assert alarm["seq"] == NO_TURN
    assert alarm["subject_kind"] == "work_order"
    assert alarm["fo_id"] is None
    # `alarm_status` is the ROW's; `status` is the work order's
    assert alarm["alarm_status"] == "raised"
    assert armed[0].id in alarm["reason"]

    (event,) = store.events_of_kind(wo_id, "health_finding")
    payload = json.loads(event["payload"])
    assert payload == {"alarm_id": alarm["id"], "probe": armed[0].id,
                       "subject_kind": "work_order", "subject_id": wo_id,
                       "reason": alarm["reason"]}

    (review,) = _reviews(store, subject_id=wo_id)
    assert review["outcome"] == "findings"
    assert review["trigger"] == "first-look"
    assert review["findings"] == 1
    assert review["detail"] == armed[0].id
    assert review["fingerprint"]

    assert store.get_work_order(wo_id)["needs_attention"] == 1
    assert store.get_work_order(wo_id)["attention_reason"] == alarm["reason"]

    from jarvis import agent_usage
    (row,) = _agent_calls("health")
    assert row["project"] == "proj_a" and row["wo_id"] == wo_id
    assert agent_usage.KIND_LABELS["health"] != "health"
    assert agent_usage.describe("health") == "supervisor health review"


def test_the_sweep_is_billed_apart_from_the_cost_review(started, catalog_file, store, clock):
    """A SEPARATE `agent_calls.kind`, because the sweep is the standing cost of watching and the review is..."""
    _enable(catalog_file)
    _wo(store, status="running")

    _sweep(started(), clock)

    assert len(_agent_calls("health")) == 1
    assert _agent_calls("supervisor") == [], "a sweep is not a review"


def test_a_clean_bill_of_health_writes_a_row_and_no_alarm(
        started, catalog_file, fake_claude, store, clock):
    _enable(catalog_file)
    wo_id = _wo(store, status="running", description="FORCE_HEALTH_CLEAR")

    _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1, "the healthy case still spends the call"
    assert _findings(store) == []
    (review,) = _reviews(store, subject_id=wo_id)
    assert review["outcome"] == "clear"
    assert review["findings"] == 0 and review["detail"] == ""
    assert store.get_work_order(wo_id)["needs_attention"] == 0


def test_a_probe_the_project_never_armed_is_dropped_and_the_rest_survive(
        started, catalog_file, monkeypatch, fake_claude, store, clock):
    """A hallucinated id is ONE BAD FINDING, not a bad reply"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")
    with monkeypatch.context() as m:
        m.setenv("FAKE_HEALTH_PROBE", "the-vibes-are-off")
        _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1
    assert _findings(store) == []
    (review,) = _reviews(store, subject_id=wo_id)
    assert review["outcome"] == "clear", "nothing survived the drop, so nothing was found"


# -- the three failure shapes


@pytest.mark.parametrize("token", ["FORCE_HEALTH_GARBAGE",   # not JSON at all
                                   "FORCE_HEALTH_FAIL",      # ClaudeCliError
                                   "FORCE_HEALTH_NO_FINDINGS"])  # JSON, no `findings`
def test_a_failure_never_becomes_a_judgement(started, catalog_file, store, clock, token):
    """THE FAIL-SAFE POLARITY IS INVERTED HERE"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running", description=token)

    _sweep(started(), clock)

    assert _findings(store) == []
    assert store.get_work_order(wo_id)["needs_attention"] == 0
    assert store.events_of_kind(wo_id, "health_finding") == []
    assert store.events_of_kind(wo_id, "health_reviewed") == []
    (review,) = _reviews(store, subject_id=wo_id)
    assert review["outcome"] == "failed"
    assert review["detail"], "a failure that says nothing cannot be diagnosed"


def test_a_failed_sweep_is_retried_rather_than_counted_as_a_look(
        started, catalog_file, fake_claude, store, clock):
    """How "the fingerprint was not recorded as reviewed" becomes OBSERVABLE rather than asserted about an..."""
    _enable(catalog_file)
    wo_id = _wo(store, status="running", description="FORCE_HEALTH_GARBAGE")
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    reviews = _reviews(store, subject_id=wo_id)
    assert [r["outcome"] for r in reviews] == ["failed", "failed"]
    assert [r["trigger"] for r in reviews] == ["first-look", "first-look"], (
        "a failure is not a look, so the second tick is still the first look")


# -- the dedupe, which is the trap


def test_a_still_true_symptom_is_not_re_raised_until_the_unit_moves(
        started, catalog_file, fake_claude, store, clock):
    """THE FOUR ASSERTIONS THE SPEC SPECIFIES"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    # (1) two sweeps GENUINELY RAN — without this the rest is true of a sweep that never reached a model...
    assert len(_health_calls(fake_claude)) == 2
    # (2) two looks at the same state, one finding
    reviews = _reviews(store, subject_id=wo_id)
    assert len(reviews) == 2
    assert reviews[0]["fingerprint"] == reviews[1]["fingerprint"]
    assert len(_findings(store)) == 1

    # (3) STATUS MUST NOT ENTER THE PREDICATE
    (alarm,) = _findings(store)
    store.update_alarm(alarm["id"], status="acked")
    store.clear_attention(wo_id)
    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == 3
    assert len(_findings(store)) == 1, "an acked finding is not a licence to re-raise"
    assert store.get_work_order(wo_id)["needs_attention"] == 0

    # (4) and when the state genuinely moves, a still-true symptom is worth re-stating
    store.add_event(wo_id, "note", {"n": 1})
    _sweep(daemon, clock)
    reviews = _reviews(store, subject_id=wo_id)
    assert reviews[-1]["fingerprint"] != reviews[0]["fingerprint"]
    assert reviews[-1]["trigger"] == "changed"
    assert len(_findings(store)) == 2


# -- what gets swept, and how many


def test_a_settled_feature_is_not_swept_and_an_open_one_with_a_carrier_is(
        started, catalog_file, fake_claude, store, clock):
    """One test with a total call count of exactly 1: two features, one candidate"""
    _feature(store, "already done", status="completed")
    open_fo = _feature(store, "still going", status="executing")
    _enable(catalog_file)

    _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1
    (alarm,) = _findings(store)
    assert alarm["subject_kind"] == "feature_order"
    assert alarm["fo_id"] == open_fo
    assert alarm["wo_id"] == store.carrier_for_feature(open_fo)["id"]
    assert alarm["seq"] == NO_TURN
    (review,) = _reviews(store, kind="feature_order", subject_id=open_fo)
    assert review["outcome"] == "findings"
    # The flag lands on the CARRIER, which is not itself in trouble, so it has to name the record to open
    carrier = store.get_work_order(alarm["wo_id"])
    assert carrier["needs_attention"] == 1
    assert carrier["attention_reason"].startswith(f"{open_fo}: ")


def test_a_feature_with_no_carrier_is_never_offered_to_the_sweep(
        started, catalog_file, fake_claude, store, clock):
    """A feature nobody has planned has no session at all and nothing to record a finding on"""
    _feature(store, "unplanned", status="pending", carrier=False)
    _enable(catalog_file)

    _sweep(started(), clock)

    assert _health_calls(fake_claude) == []
    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 0


def test_an_ungoverned_work_order_is_not_the_os_s_to_judge(
        started, catalog_file, fake_claude, store, clock):
    _wo(store, "the user's own session", status="running", origin="injected")
    _wo(store, "ours", status="running")
    _enable(catalog_file)

    _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1


def test_the_cap_rotates_rather_than_starving_the_tail(
        started, catalog_file, fake_claude, store, clock):
    """`health_max_units_per_tick` bounds the spend"""
    _enable(catalog_file, health_max_units_per_tick=4)
    ids = [_wo(store, f"unit {n}", status="running") for n in range(5)]
    daemon = started()

    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == 4
    swept = {i for i in ids if _reviews(store, subject_id=i)}
    assert len(swept) == 4
    (starved,) = [i for i in ids if i not in swept]

    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == 8
    assert _reviews(store, subject_id=starved), "the fifth unit waited a tick, not for ever"


def test_the_cadence_is_a_setting_and_a_tick_off_it_sweeps_nothing(
        started, catalog_file, fake_claude, store, clock):
    _enable(catalog_file, health_every_ticks=20)
    _wo(store, status="running")
    daemon = started()

    daemon.tick_count = 2
    daemon.health_tick()
    while daemon.health_sweeping:
        time.sleep(0.01)
    assert _health_calls(fake_claude) == []

    _sweep(daemon, clock)   # tick 1 of 20
    assert len(_health_calls(fake_claude)) == 1


# -- the ledger's own housekeeping


def test_deleting_a_work_order_takes_its_health_reviews_and_leaves_the_others(store):
    """`health_reviews` has no foreign key — the subject may be either kind — so the cascade is by hand"""
    doomed = _wo(store, "doomed")
    survivor = _wo(store, "survivor")
    fo_id = _feature(store)
    for kind, subject in (("work_order", doomed), ("work_order", survivor),
                          ("feature_order", fo_id)):
        store.record_health_review(kind, subject, fingerprint="fp", trigger="first-look",
                                   outcome="clear")

    counts = store.delete_work_order(doomed)

    assert set(counts) == {"events", "messages", "turns", "assumptions", "approvals",
                           "notifications"}
    assert _reviews(store, subject_id=doomed) == []
    assert len(_reviews(store, subject_id=survivor)) == 1
    assert len(_reviews(store, kind="feature_order", subject_id=fo_id)) == 1


def test_the_looking_is_recorded_apart_from_the_finding(started, catalog_file, store, clock):
    """Writing "we looked and found nothing" into `wo_alarms` would fill `alarms_across`"""
    _enable(catalog_file)
    _wo(store, status="running", description="FORCE_HEALTH_CLEAR")

    _sweep(started(), clock)

    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 1
    assert store.alarms_across() == []


def test_the_sweeps_own_looking_stays_off_the_default_timeline(
        started, catalog_file, store, clock):
    """`health_reviewed` fires on every sweep including the clear ones"""
    from jarvis import timeline

    _enable(catalog_file)
    wo_id = _wo(store, status="running")

    _sweep(started(), clock)

    assert timeline.event_level("health_reviewed") == "debug"
    assert timeline.event_level("health_finding") != "debug"
    kinds = [e["kind"] for e in timeline.build_timeline(store.get_work_order(wo_id), store.list_events(wo_id), store.list_messages(wo_id))]
    assert "health_reviewed" not in kinds
    assert "health_finding" in kinds


# -- the seams this section must not have moved


def test_the_sweeps_packet_carries_the_trigger_and_never_an_alarm_that_does_not_exist(
        started, catalog_file, fake_claude, store, clock):
    """`build_evidence(alarm=None)` omits the alarm section rather than synthesising one: a fabricated..."""
    _enable(catalog_file)
    wo_id = _wo(store, "a distinctive title", status="running")

    _sweep(started(), clock)

    (call,) = _health_calls(fake_claude)
    packet = call["argv"][call["argv"].index("-p") + 1]
    assert packet.startswith(supervisor.HEALTH_HEADER)
    assert supervisor.HEALTH_WHY["first-look"] in packet
    assert "# The alarm" not in packet
    assert "# The work order" in packet and wo_id in packet


def test_the_cost_review_prompt_is_untouched_by_the_sweep_sharing_its_persona(store):
    """The checklist is APPENDED, so a review's cached prefix is byte-identical to what it was before this..."""
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        review = supervisor.build_system_prompt(neo, "proj_a", CFG.learnings_limit)
        sweep = supervisor.build_system_prompt(
            neo, "proj_a", CFG.learnings_limit,
            probes=probes_mod.armed(CFG.probes, "work_order"))
    finally:
        neo.close()

    assert CHECKLIST not in review
    assert sweep.startswith(review)


def test_a_sweep_is_indistinguishable_from_a_review_by_persona_alone(
        started, catalog_file, fake_claude, store, clock):
    """WHY `_health_calls` EXISTS"""
    from jarvis.supervisor import SUPERVISOR_PERSONA

    _enable(catalog_file)
    _wo(store, status="running")

    _sweep(started(), clock)

    (call,) = _health_calls(fake_claude)
    system = call["system_prompt_seen"]
    assert SUPERVISOR_PERSONA.splitlines()[0] in system
    assert CHECKLIST in system, "and the checklist is the only thing that separates them"


def test_the_sweep_gets_its_own_pool_and_its_own_guard(started):
    """NOT the supervisor's"""
    daemon = started()
    assert daemon.health_pool is not daemon.supervisor_pool
    assert daemon.health_pool is not daemon.neo_pool
    assert daemon.health_sweeping is False


def test_a_second_tick_cannot_queue_behind_a_sweep_still_in_flight(started):
    daemon = started()
    daemon.tick_count = 1
    daemon.health_sweeping = True
    daemon.health_tick()
    assert daemon.health_sweeping is True  # nothing was submitted; the guard held


def test_the_sweep_decides_and_does_not_act(started, catalog_file, fake_claude, store, clock):
    """`supervisor.py` RAISES; the existing review path judges it unchanged"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")

    _sweep(started(), clock)

    assert len(_findings(store)) == 1
    assert store.get_work_order(wo_id)["status"] == "running"
    assert store.queued_messages(wo_id) == []
    assert store.list_messages(wo_id) == []


def test_health_reviews_outcomes_are_a_declared_vocabulary(store):
    from jarvis.project_store import HEALTH_OUTCOMES

    assert set(HEALTH_OUTCOMES) == {"clear", "findings", "failed", "held"}
    with pytest.raises(AssertionError):
        store.record_health_review("work_order", "wo-1", fingerprint="fp",
                                   trigger="first-look", outcome="fine-i-guess")


def test_count_events_is_unbounded_where_list_events_is_capped(store):
    """`list_events` caps at 200, which is fine for a page and wrong for a fingerprint: a busy order's..."""
    wo_id = _wo(store)
    for n in range(205):
        store.add_event(wo_id, "note", {"n": n})
    assert len(store.list_events(wo_id)) == 200
    assert store.count_events(wo_id) == 206, "and the `created` event is one of them"


def test_a_records_own_clock_is_what_ages_it(store, monkeypatch):
    """`due` reads `db.now`, so a row created in the past is genuinely in the past"""
    long_ago = db.now() - 3 * 24 * 60 * 60
    with monkeypatch.context() as m:
        m.setattr("jarvis.db.now", lambda: long_ago)
        wo_id = _wo(store, "the old one")
        store.record_health_review("work_order", wo_id, fingerprint="frozen",
                                   trigger="first-look", outcome="clear")

    last = store.last_health_review("work_order", wo_id)
    assert last["ts"] == long_ago
    assert due(last, "frozen", CFG, db.now(),
               float(store.get_work_order(wo_id)["created_at"])) == "stale"


# -- the floor applies to ATTEMPTS, not to judgements (issue #216)


def test_the_floor_applies_to_a_failed_attempt_too():
    """THE SECOND HALF OF #216, and the half that turned a dud feature into a $101 bill"""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30)
    minute = 60.0

    # Every sweep so far has failed: no review to compare against, but an attempt to count from
    assert due(None, "fp", cfg, now=100 * minute, created=0.0,
               last_attempt=90 * minute) is None
    assert due(None, "fp", cfg, now=121 * minute, created=0.0,
               last_attempt=90 * minute) == "first-look"


def test_the_floor_is_unchanged_when_nothing_has_failed():
    """The guard must be invisible to a healthy fleet: with every attempt recorded as a review"""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30,
                                   health_stale_minutes=720)
    minute = 60.0
    old = {"ts": 0.0, "fingerprint": "before"}

    assert due(old, "after", cfg, now=29 * minute, created=0.0, last_attempt=0.0) is None
    assert due(old, "after", cfg, now=31 * minute, created=0.0,
               last_attempt=0.0) == "changed"
    assert due(old, "before", cfg, now=721 * minute, created=0.0,
               last_attempt=0.0) == "stale"
    # And with the argument omitted entirely
    assert due(None, "fp", cfg, now=31 * minute, created=0.0) == "first-look"


def test_a_run_of_failures_costs_the_floor_and_not_the_tick(
        started, catalog_file, fake_claude, store, clock):
    """WHAT THE BILL ACTUALLY MEASURES"""
    _enable(catalog_file, health_min_interval_minutes=60)
    wo_id = _wo(store, status="running", description="FORCE_HEALTH_NO_FINDINGS")
    daemon = started()
    clock.advance(minutes=61)

    for _ in range(6):
        _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 1, (
        "a failed sweep must wait out the floor before it is retried")
    assert [r["outcome"] for r in _reviews(store, subject_id=wo_id)] == ["failed"]

    clock.advance(minutes=61)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2, (
        "and it must still be retried once the floor has elapsed")


def test_the_sweep_asks_the_model_for_findings_not_for_a_decision(
        started, catalog_file, fake_claude, store, clock):
    """END TO END, against the prompt that really goes out"""
    _enable(catalog_file)
    _wo(store, status="running")

    _sweep(started(), clock)

    (call,) = _health_calls(fake_claude)
    prompt = call["system_prompt_seen"]
    assert '"findings"' in prompt
    assert prompt.index('"findings"') > prompt.index('"decision"')


# -- the canary: a sweep that judges nothing must say so (issue #216)


def _force_failures(store, n: int) -> None:
    for i in range(n):
        store.record_health_review("work_order", f"wo-{i}", fingerprint="fp",
                                   trigger="first-look", outcome="failed",
                                   detail='unreadable health sweep output: '
                                          '{"decision": "ack", "reason": "..."}')


def test_a_sweep_that_never_judges_anything_is_reported(store):
    """WHAT #216 ACTUALLY COST US: not the broken prompt, but that nothing said so for 2215 sweeps"""
    from jarvis.invariants import (
        HEALTH_SWEEP_FAILURE_RUN,
        check_health_sweep_produces_judgements,
    )

    _force_failures(store, HEALTH_SWEEP_FAILURE_RUN)

    (violation,) = list(check_health_sweep_produces_judgements(store))
    assert violation.invariant == "INV-HEALTH-SWEEP-MUTE"
    assert not violation.repaired, "the rows are honest; the prompt is what is broken"
    assert '"decision"' in violation.detail, (
        "the detail must name the reply that failed, or the check sends the reader "
        "looking with nothing to look at")


def test_the_canary_is_silent_on_a_sweep_that_works_and_on_one_that_never_ran(store):
    """Both quiet cases, because each is green on its own against a check that returns nothing at all"""
    from jarvis.invariants import (
        HEALTH_SWEEP_FAILURE_RUN,
        check_health_sweep_produces_judgements,
    )

    assert list(check_health_sweep_produces_judgements(store)) == []

    _force_failures(store, HEALTH_SWEEP_FAILURE_RUN - 1)
    assert list(check_health_sweep_produces_judgements(store)) == []

    # One judgement anywhere in the run is what says the sweep can still work
    store.record_health_review("work_order", "wo-ok", fingerprint="fp",
                               trigger="first-look", outcome="clear")
    _force_failures(store, HEALTH_SWEEP_FAILURE_RUN - 1)
    assert list(check_health_sweep_produces_judgements(store)) == []


def test_the_canary_fires_through_doctor_on_the_real_sweep_path(
        started, catalog_file, store, clock):
    """END TO END against the defect as it really happened: the fake answers with the cost review's shape (#216)"""
    from jarvis.invariants import HEALTH_SWEEP_FAILURE_RUN, check_project

    _enable(catalog_file)
    _wo(store, status="running", description="FORCE_HEALTH_NO_FINDINGS")
    daemon = started()

    for _ in range(HEALTH_SWEEP_FAILURE_RUN):
        _sweep(daemon, clock)

    swept = store.recent_health_reviews(HEALTH_SWEEP_FAILURE_RUN)
    assert len(swept) == HEALTH_SWEEP_FAILURE_RUN, "the sweeps must really have run"
    assert {r["outcome"] for r in swept} == {"failed"}

    found = [v for v in check_project(store, repair=False)
             if v.invariant == "INV-HEALTH-SWEEP-MUTE"]
    assert len(found) == 1, "a sweep that has judged nothing in ten tries must be news"


def test_the_canary_ages_out_a_run_of_failures_that_is_over(store, clock):
    """THE CANARY'S OWN DEFECT, found in production the hour it shipped (#216)"""
    from jarvis.invariants import (
        HEALTH_SWEEP_FAILURE_RUN,
        check_health_sweep_produces_judgements,
    )

    _force_failures(store, HEALTH_SWEEP_FAILURE_RUN)
    assert list(check_health_sweep_produces_judgements(store)), "live: it must report"

    # The same rows, a day later, with nothing since
    clock.advance(minutes=60 * 24)
    assert list(check_health_sweep_produces_judgements(store)) == [], (
        "a run of failures that stopped is history, not an alarm")


def test_a_sweep_still_failing_now_is_reported_however_old_the_run_started(store, clock):
    """The other side of the bound, and the one that must not be lost: a sweep that has been failing for a..."""
    from jarvis.invariants import (
        HEALTH_SWEEP_FAILURE_RUN,
        check_health_sweep_produces_judgements,
    )

    _force_failures(store, HEALTH_SWEEP_FAILURE_RUN)
    clock.advance(minutes=60 * 24)
    _force_failures(store, 1)          # and it failed again just now

    assert list(check_health_sweep_produces_judgements(store)), (
        "still failing is still news, however long it has been true")


# -- a usage limit is not a failed sweep


class _MovedNow:
    """`db.now` moved for the body of a `with`, and nothing else touched"""

    def __init__(self, at: float) -> None:
        self.at, self.was = at, db.now

    def __enter__(self):
        import jarvis.db

        jarvis.db.now = lambda: self.at
        return self

    def __exit__(self, *exc):
        import jarvis.db

        jarvis.db.now = self.was


def _held(store) -> list[dict]:
    return [r for r in store.recent_health_reviews(100) if r["outcome"] == "held"]


def test_a_refused_sweep_is_held_and_not_recorded_as_a_failure(
        started, catalog_file, store, clock, fake_claude):
    """The ordering in `review_health` is the whole fix: `UsageLimitError` is a `ClaudeCliError` subclass"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")
    # Relative form, for the reason spelled out at `test_a_live_hold_buys_no_second_refusal`
    fake_claude.health_rate_limited(reset="in 5h")

    _sweep(started(), clock)

    (row,) = store.recent_health_reviews(10)
    assert row["outcome"] == "held"
    assert row["reopens_at"] > db.now()
    assert "session limit" in row["detail"]
    assert "could not be reached" not in row["detail"], (
        "the sweep WAS reached — the account refused it")
    assert _findings(store) == []
    assert [e for e in store.list_events(wo_id) if e["kind"] == "health_reviewed"] == []
    assert not store.get_work_order(wo_id)["needs_attention"]


def test_a_live_hold_buys_no_second_refusal_and_expires_by_the_clock(
        started, catalog_file, store, clock, fake_claude):
    """A recorded hold nobody reads is a quieter retry storm"""
    _enable(catalog_file)
    _wo(store, status="running", description="FORCE_HEALTH_CLEAR")
    # THE RELATIVE FORM, and that is what makes this test deterministic
    fake_claude.health_rate_limited(reset="in 5h")
    daemon = started()

    _sweep(daemon, clock)
    refused = len(_health_calls(fake_claude))
    assert refused == 1

    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == refused, "the window is still shut"
    assert len(store.recent_health_reviews(10)) == 1, "one hold, not one per tick"

    # The first tick after the window reopens sweeps again
    fake_claude.health_recover()
    clock.advance(minutes=60 * 25)
    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == refused + 1
    assert store.recent_health_reviews(1)[0]["outcome"] == "clear"


def test_a_held_row_is_not_an_attempt_and_does_not_floor_the_next_look(store):
    """Half of the fix: a refusal bought no model call"""
    wo_id = _wo(store, status="running")
    store.record_health_review("work_order", wo_id, fingerprint="fp",
                               trigger="first-look", outcome="held",
                               detail="You've hit your session limit · resets 2:10am",
                               reopens_at=db.now() + 3600)

    assert store.last_health_attempt_ts("work_order", wo_id) is None
    assert store.last_health_review("work_order", wo_id) is None
    created = db.now() - 10 * 60 * 60
    assert due(None, "fp", CFG, db.now(), created,
               last_attempt=store.last_health_attempt_ts("work_order", wo_id)) \
        == "first-look"
    # ...where a `failed` row still floors it: that one DID spend money (issue #216)
    store.record_health_review("work_order", wo_id, fingerprint="fp",
                               trigger="first-look", outcome="failed", detail="boom")
    assert store.last_health_attempt_ts("work_order", wo_id) is not None


def test_the_hold_is_project_wide_and_only_while_it_lasts(store):
    """The account's window is not a property of a work order"""
    assert store.health_sweep_hold() is None
    store.record_health_review("work_order", "wo-1", fingerprint="fp",
                               trigger="first-look", outcome="held",
                               detail="spent", reopens_at=db.now() + 600)

    hold = store.health_sweep_hold()
    assert hold is not None and hold[1] == "spent"
    assert hold[0] > db.now()

    # A judgement about another unit does not lift the account's window
    store.record_health_review("feature_order", "fo-1", fingerprint="fp",
                               trigger="stale", outcome="clear")
    assert store.health_sweep_hold() is not None

    with _MovedNow(db.now() + 601):
        assert store.health_sweep_hold() is None


def test_held_rows_never_trip_the_mute_canary_and_never_rescue_a_failure_run(store):
    """Held rows are filtered out BEFORE the last ten are taken: ten holds cannot trip it"""
    from jarvis.invariants import (
        HEALTH_SWEEP_FAILURE_RUN,
        check_health_sweep_produces_judgements,
    )

    for i in range(HEALTH_SWEEP_FAILURE_RUN):
        store.record_health_review("work_order", f"wo-{i}", fingerprint="fp",
                                   trigger="first-look", outcome="held",
                                   detail="spent", reopens_at=db.now() - 1)
    assert list(check_health_sweep_produces_judgements(store)) == []

    _force_failures(store, 5)
    store.record_health_review("work_order", "wo-x", fingerprint="fp",
                               trigger="first-look", outcome="held", detail="spent",
                               reopens_at=db.now() - 1)
    _force_failures(store, 5)
    (violation,) = list(check_health_sweep_produces_judgements(store))
    assert violation.invariant == "INV-HEALTH-SWEEP-MUTE"
    assert "FAILED" in violation.detail
    assert "hold, not a failure" in violation.detail


def test_a_live_hold_keeps_the_mute_canary_silent(store):
    """The check claims the sweep IS SPENDING model calls"""
    from jarvis.invariants import (
        HEALTH_SWEEP_FAILURE_RUN,
        check_health_sweep_produces_judgements,
    )

    _force_failures(store, HEALTH_SWEEP_FAILURE_RUN)
    assert list(check_health_sweep_produces_judgements(store))

    store.record_health_review("account", "", fingerprint="", trigger="account-window",
                               outcome="held", detail="spent",
                               reopens_at=db.now() + 3600)
    assert list(check_health_sweep_produces_judgements(store)) == []


def test_a_shut_account_records_one_hold_per_window_and_sweeps_nothing(
        started, catalog_file, store, clock, fake_claude):
    """Deduped on the MOMENT, not on the tick: at a 5s tick a three-hour window would otherwise write two..."""
    from jarvis import fleet

    _enable(catalog_file)
    _wo(store, status="running", description="FORCE_HEALTH_CLEAR")
    daemon = started()
    shut = fleet.Fleet(3, 0, fleet.Outage(project="proj_a", wo_id="wo-1",
                                          reopens_at=db.now() + 3600,
                                          message="You've hit your session limit"))

    for _ in range(20):
        daemon.tick_count = 1
        daemon._health_sweep(daemon._health_projects(), shut)

    account = [r for r in _held(store) if r["subject_kind"] == "account"]
    assert len(account) == 1, "one row per window, not one per tick"
    assert account[0]["reopens_at"] == shut.outage.reopens_at
    assert account[0]["trigger"] == "account-window"
    assert _health_calls(fake_claude) == [], "nothing was swept"

    later = fleet.Fleet(3, 0, fleet.Outage(project="proj_a", wo_id="wo-1",
                                           reopens_at=shut.outage.reopens_at + 3600,
                                           message="still spent"))
    daemon.tick_count = 1
    daemon._health_sweep(daemon._health_projects(), later)
    assert len([r for r in _held(store) if r["subject_kind"] == "account"]) == 2


def test_the_tick_hands_the_health_sweep_the_reading_it_already_took(
        started, catalog_file, store, clock):
    """`health_tick` threads the tick's one `fleet` reading through, and opens no store itself"""
    from jarvis import fleet

    _enable(catalog_file)
    daemon = started()
    seen = {}
    daemon._health_sweep = lambda projects, state=None: seen.update(state=state)
    shut = fleet.Fleet(3, 0, fleet.Outage(project="proj_a", wo_id="wo-1",
                                          reopens_at=db.now() + 3600, message="spent"))

    daemon.tick_count = 1
    daemon.health_tick(shut)
    deadline = time.monotonic() + 10
    while daemon.health_sweeping and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen["state"] is shut


# -- the STUCK sweep: §§1-2 of docs/superpowers/specs/2026-09-30-an-order-that-stops

HOUR = 3600.0


def stuck_catalog(tmp_path, projects: list[dict],
                  name: str = "stuck-catalog.json", **os_extra):
    """A REGISTERED catalog carrying `fleet_health` at its shipped defaults"""
    from jarvis.central_store import CentralStore

    path = tmp_path / name
    path.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "notifications": {"sinks": ["log"]}, **os_extra},
        "projects": projects,
    }))
    central = CentralStore()
    try:
        for p in projects:
            central.upsert_project(p["name"], p["path"], p["name"])
        central.set_state("catalog_path", str(path))
        central.conn.commit()
    finally:
        central.close()
    return path


@pytest.fixture()
def stuck_os(jarvis_home, project, tmp_path):
    cat = stuck_catalog(tmp_path, [{"name": "proj_a", "path": str(project),
                                    "description": "test project"}])
    store = ProjectStore(project)
    try:
        yield {"daemon": Daemon(load_catalog(cat)), "store": store, "path": cat}
    finally:
        store.close()


def park_order(store, wo_id: str, status: str, hours: float) -> float:
    """Back-date one order into `status` `hours` ago, on every clock the sweep reads"""
    store.set_status(wo_id, status)
    at = time.time() - hours * HOUR
    rows = store.conn.execute("SELECT id FROM wo_state_spans WHERE order_id=? "
                              "ORDER BY id", (wo_id,)).fetchall()
    for n, row in enumerate(reversed(rows)):
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?",
                           (at - n, row["id"]))
    store.conn.execute("UPDATE wo_events SET ts=? WHERE wo_id=?", (at, wo_id))
    store.conn.execute("UPDATE work_orders SET created_at=?, updated_at=? WHERE id=?",
                       (at, at, wo_id))
    store.conn.commit()
    return at


def usage_hold(store, wo_id: str, started: float, ended: float | None = None) -> None:
    """One usage-limit hold on this order's timeline, open unless `ended` is given"""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    events = [("turn_paused", {"reason": PAUSE_USAGE_LIMIT, "seq": 1}, started)]
    if ended is not None:
        events.append(("turn_resumed", {"retried_seq": 1}, ended))
    for kind, payload, at in events:
        store.add_event(wo_id, kind, payload)
        store.conn.execute(
            "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)", (at,))
    store.conn.commit()


def test_usage_limit_hold_is_not_stuck(stuck_os):
    """§2: the account's window is the one hold that excludes an order outright"""
    from jarvis import stuck
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    store = stuck_os["store"]
    wo = ops.create_work_order("proj_a", "parked behind the window")
    at = park_order(store, wo["id"], "waiting_pr_merge", hours=5)
    usage_hold(store, wo["id"], started=at)

    stuck_os["daemon"].stuck_tick(None)

    assert ops.list_investigation_orders("proj_a", include_settled=True) == []
    thresholds = {"waiting_pr_merge": 3 * HOUR}
    judged = stuck.assess("waiting_pr_merge", 5 * HOUR, 5 * HOUR, 5 * HOUR,
                          thresholds, 4 * HOUR, excluded_cause=PAUSE_USAGE_LIMIT)
    assert judged.excluded == PAUSE_USAGE_LIMIT and not judged.stuck
    # The same wall clock with nothing holding it IS stuck: the hold is the whole difference
    assert stuck.assess("waiting_pr_merge", 5 * HOUR, 5 * HOUR, 0.0,
                        thresholds, 4 * HOUR).stuck


def test_held_seconds_do_not_count(stuck_os):
    """§2: the threshold is on ACTIVE seconds, so a reopened window still discounts"""
    import jarvis.catalog as catalog_mod
    from jarvis import stuck

    store, daemon = stuck_os["store"], stuck_os["daemon"]
    # Held for all but half the shipped threshold, so `active_seconds` is what decides
    threshold = catalog_mod.DEFAULT_FLEET_HEALTH_THRESHOLDS["waiting_pr_merge"] * 60
    wo = ops.create_work_order("proj_a", "held for almost all of its five hours")
    at = park_order(store, wo["id"], "waiting_pr_merge", hours=5)
    usage_hold(store, wo["id"], started=at, ended=at + 5 * HOUR - threshold / 2)

    daemon.stuck_tick(None)
    assert ops.list_investigation_orders("proj_a", include_settled=True) == []
    judged = stuck.assess("waiting_pr_merge", 5 * HOUR, 5 * HOUR, 4 * HOUR,
                          {"waiting_pr_merge": 3 * HOUR}, 4 * HOUR)
    assert judged.active_seconds == HOUR and not judged.stuck

    # Eight hours in status, the same four held: `active_seconds` alone now passes
    at = park_order(store, wo["id"], "waiting_pr_merge", hours=8)
    usage_hold(store, wo["id"], started=at, ended=at + 4 * HOUR)
    daemon.stuck_tick(None)
    assert len(ops.list_investigation_orders("proj_a", include_settled=True)) == 1


def ci_hold(store, wo_id: str, round_no: int, started: float, reopens: float) -> None:
    """One `ci_pending` validation hold, in `Daemon._validation_ci_held`'s event shape"""
    from jarvis.project_store import VALIDATION_CI_CAUSE

    store.add_event(wo_id, "validation_failed",
                    {"round": round_no, "cause": VALIDATION_CI_CAUSE,
                     "reopens_at": reopens, "pending": ["unit (3.13)"]})
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)", (started,))
    store.conn.commit()


def running_turn(store, wo_id: str, started: float) -> dict:
    """One turn in flight, started `started` ago"""
    turn = store.create_turn(wo_id, "dispatch", "go")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (started, turn["id"]))
    store.conn.commit()
    return turn


def _validating(store, wo_title: str, minutes: float):
    """An order parked in `validating` with a round on record, `minutes` ago"""
    wo = ops.create_work_order("proj_a", wo_title)
    store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    return wo, park_order(store, wo["id"], "validating", hours=minutes / 60.0)


def _judged(store, now: float):
    """Every row the sweep judges, `stuck_only=False` — the shape `/stuck` reads"""
    from jarvis.catalog import FleetHealthConfig

    return ops.stuck_scan(store, "proj_a", FleetHealthConfig(), now,
                          stuck_only=False)[1]


def test_a_round_waiting_for_ci_is_excluded_and_still_a_visible_row(stuck_os):
    """§5: `excluded == "ci_pending"`, `stuck` False, and NO `continue`"""
    from jarvis.project_store import VALIDATION_CI_CAUSE

    store = stuck_os["store"]
    now = time.time()
    wo, at = _validating(store, "waiting for github", minutes=20)
    ci_hold(store, wo["id"], 1, started=at, reopens=now + 60)

    rows = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]]

    assert len(rows) == 1, "the held row left the sweep's output entirely"
    assert rows[0]["verdict"].excluded == VALIDATION_CI_CAUSE
    assert rows[0]["verdict"].stuck is False


def test_a_validating_order_with_no_ci_hold_is_still_judged(stuck_os):
    """§7(2): the exclusion must not generalise to every `validating` order"""
    store = stuck_os["store"]
    now = time.time()
    wo, _at = _validating(store, "nothing is holding this round", minutes=20)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]

    assert row["verdict"].excluded == ""
    assert row["verdict"].stuck is True


def test_overlapping_usage_and_ci_holds_are_discounted_once(stuck_os):
    """§4: the UNION of the two interval sets, never the sum"""
    store = stuck_os["store"]
    now = time.time()
    wo, at = _validating(store, "held by both", minutes=60)
    usage_hold(store, wo["id"], started=at, ended=at + 30 * 60)
    ci_hold(store, wo["id"], 1, started=at + 20 * 60, reopens=at + 40 * 60)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]

    assert abs(row["discounted"] - 40 * 60) < 5, row["discounted"]
    assert row["discounted"] < 30 * 60 + 20 * 60, "the overlap was counted twice"


def test_with_no_ci_hold_the_usage_figure_is_unchanged(stuck_os):
    """§4's identity: `union_seconds` over the usage holds alone equals `by_cause`"""
    from jarvis import holds
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    store = stuck_os["store"]
    now = time.time()
    wo, at = _validating(store, "held by the account only", minutes=60)
    usage_hold(store, wo["id"], started=at, ended=at + 30 * 60)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]

    usage = [h for h in holds.held(store, wo["id"], now=now)
             if h.cause == PAUSE_USAGE_LIMIT]
    expected = holds.by_cause(usage, row["since"], now, now).get(PAUSE_USAGE_LIMIT, 0.0)
    assert row["discounted"] == expected


def test_ci_hold_spans_are_clipped_to_the_status_span(stuck_os):
    """§3: every round, clipped to `[since, now]`, oldest first, nothing else's cause"""
    from jarvis.project_store import (VALIDATION_HELD_CAUSE,
                                      validation_ci_hold_spans)

    def ev(ts, round_no, reopens, cause="ci_pending"):
        return {"ts": ts, "payload": json.dumps(
            {"round": round_no, "cause": cause, "reopens_at": reopens})}

    events = [
        ev(130.0, 2, 150.0),                              # wholly inside
        ev(50.0, 1, 120.0),                               # began before `since`
        ev(180.0, 3, 400.0),                              # still live at `now`
        ev(60.0, 1, 90.0),                                # over before `since`
        ev(140.0, 2, 160.0, cause=VALIDATION_HELD_CAUSE),  # another cause entirely
    ]

    assert validation_ci_hold_spans(events, 100.0, 200.0) == [
        (100.0, 120.0), (130.0, 150.0), (180.0, 200.0)]
    assert validation_ci_hold_spans([], 100.0, 200.0) == []


def _judge(stuck_os, wo_id: str):
    """One order's `stuck.Verdict`, through the sweep's own reader"""
    spec = load_catalog(stuck_os["path"]).projects[0]
    _scanned, judged = ops.stuck_scan(stuck_os["store"], "proj_a", spec.fleet_health,
                                      time.time(), stuck_only=False)
    return next(r["verdict"] for r in judged if r["wo"]["id"] == wo_id)


def test_a_live_dependency_is_not_stuck(stuck_os):
    """§2.2 of docs/superpowers/specs/2026-10-09-a-designed-wait-is-not-a-stall.md"""
    from jarvis import stuck

    store = stuck_os["store"]
    dep = ops.create_work_order("proj_a", "the dependency")
    child = ops.create_work_order("proj_a", "waiting as --depends-on designed",
                                  depends_on=[dep["id"]])
    park_order(store, child["id"], "pending", hours=5)

    verdict = _judge(stuck_os, child["id"])
    assert verdict.stuck is False
    assert verdict.excluded == stuck.LIVE_DEPENDENCY


@pytest.mark.parametrize("how", ["cancelled", "deleted"])
def test_a_dead_dependency_is_still_judged(stuck_os, how):
    """§2.3: a dead edge cannot clear itself, so the order SHOULD be judged"""
    store = stuck_os["store"]
    dead = ops.create_work_order("proj_a", "the edge that can never clear")
    live = ops.create_work_order("proj_a", "the live edge")
    child = ops.create_work_order("proj_a", "blocked on a dead edge",
                                  depends_on=[dead["id"], live["id"]])
    if how == "deleted":
        ops.delete_work_order(dead["id"])
    else:
        store.set_status(dead["id"], "cancelled")
    park_order(store, child["id"], "pending", hours=5)

    verdict = _judge(stuck_os, child["id"])
    assert verdict.stuck is True and verdict.excluded == ""


def test_pending_with_no_dependencies_is_still_stuck(stuck_os):
    """§2.2: the exclusion is the EDGES, not the status"""
    store = stuck_os["store"]
    wo = ops.create_work_order("proj_a", "pending behind nothing at all")
    park_order(store, wo["id"], "pending", hours=5)

    verdict = _judge(stuck_os, wo["id"])
    assert verdict.stuck is True and verdict.excluded == ""


def test_edges_exclude_only_a_pending_order(stuck_os):
    """§2.2: the read is gated on status, not merely on edge presence"""
    store = stuck_os["store"]
    dep = ops.create_work_order("proj_a", "the dependency")
    child = ops.create_work_order("proj_a", "delivered behind a pull request",
                                  depends_on=[dep["id"]])
    park_order(store, child["id"], "waiting_pr_merge", hours=5)

    verdict = _judge(stuck_os, child["id"])
    assert verdict.stuck is True and verdict.excluded == ""


def test_usage_window_outranks_a_live_dependency(stuck_os):
    """§2.2's precedence: the window is the account's and outranks a per-order hold"""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    store = stuck_os["store"]
    dep = ops.create_work_order("proj_a", "the dependency")
    child = ops.create_work_order("proj_a", "live edges and a spent window",
                                  depends_on=[dep["id"]])
    at = park_order(store, child["id"], "pending", hours=5)
    usage_hold(store, child["id"], started=at)

    verdict = _judge(stuck_os, child["id"])
    assert verdict.stuck is False and verdict.excluded == PAUSE_USAGE_LIMIT


# -- a child held by its feature's unratified plan (the 2026-10-15 spec)


def _held_child(store, *, status: str = "pending", assumptions: int = 1,
                hours: float = 2.0):
    """`(feature, planner, child, at)`: a feature child `hours` in `status` whose planner has..."""
    from tests.test_feature_orders import a_bare_feature

    fo, planner, kid = a_bare_feature(store, status=status, assumptions=assumptions)
    at = park_order(store, kid["id"], status, hours=hours)
    # `park_order` back-dates the CHILD's timeline; the count is walked on the planner's
    store.conn.execute("UPDATE wo_events SET ts=? WHERE wo_id=? AND kind='assumption'",
                       (at, planner["id"]))
    store.conn.commit()
    return fo, planner, kid, at


def settle_plan(store, planner_id: str, at: float, count: int = 1) -> None:
    """The user's route, back-dated: every pending assumption ruled, one `reviewed`"""
    for a in store.pending_assumptions(planner_id):
        store.review_assumption(a["id"], "accepted")
    store.add_event(planner_id, "reviewed", {"accepted": True, "count": count})
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)", (at,))
    store.conn.commit()


def test_a_child_held_by_an_unratified_plan_is_not_stuck(stuck_os):
    """§3(1)"""
    from jarvis import stuck

    store = stuck_os["store"]
    _fo, planner, kid, _at = _held_child(store)

    verdict = _judge(stuck_os, kid["id"])

    assert verdict.stuck is False
    assert verdict.excluded == stuck.PLAN_ASSUMPTION_CAUSE
    assert "clears by itself" not in verdict.reason, verdict.reason
    assert "jarvis wo review" in verdict.reason, verdict.reason
    assert store.plan_hold(store.get_work_order(kid["id"]))["planner_id"] == planner["id"]


def test_a_lifted_plan_hold_is_judged_and_its_held_seconds_still_discounted(stuck_os):
    """§2.3 and §3(2): EXCLUSION ALONE IS NOT THE FIX"""
    store = stuck_os["store"]
    now = time.time()
    _fo, planner, kid, _at = _held_child(store, hours=2)
    settle_plan(store, planner["id"], now - 60)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == kid["id"]][0]

    assert row["verdict"].excluded == "", "the lifted hold still excluded the child"
    assert abs(row["verdict"].active_seconds - 60) < 5, row["verdict"].active_seconds
    assert row["verdict"].stuck is False, row["verdict"].reason


def test_plan_hold_spans_walk_the_pending_count_over_the_planners_timeline():
    """§3(3): pure, row-shaped, clipped to `[since"""
    from jarvis.project_store import plan_hold_spans

    def ev(ts, kind, **payload):
        return {"ts": ts, "kind": kind, "payload": json.dumps(payload)}

    events = [ev(50.0, "assumption"),                 # opened before `since`
              ev(60.0, "assumption"),
              ev(120.0, "reviewed", count=2),         # both settled at once
              ev(150.0, "assumption"),
              ev(160.0, "autoreview_accepted")]       # Neo's route, one at a time

    assert plan_hold_spans(events, 100.0, 200.0) == [(100.0, 120.0), (150.0, 160.0)]
    assert plan_hold_spans([ev(150.0, "assumption")], 100.0,
                           200.0) == [(150.0, 200.0)], "a live hold must end at `now`"
    assert plan_hold_spans([], 100.0, 200.0) == []
    assert plan_hold_spans([ev(70.0, "assumption"), ev(80.0, "reviewed", count=1)],
                           100.0, 200.0) == [], "a span over before `since` was kept"
    # A malformed count cannot drive the walk negative and swallow the next hold
    assert plan_hold_spans([ev(110.0, "reviewed", count=5), ev(150.0, "assumption")],
                           100.0, 200.0) == [(150.0, 200.0)]


def test_overlapping_usage_and_plan_holds_are_discounted_once(stuck_os):
    """§3(4): the UNION of the interval sets, never the sum"""
    store = stuck_os["store"]
    now = time.time()
    _fo, planner, kid, at = _held_child(store, hours=1)
    settle_plan(store, planner["id"], at + 40 * 60)
    usage_hold(store, kid["id"], started=at + 20 * 60, ended=at + 50 * 60)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == kid["id"]][0]

    assert abs(row["discounted"] - 50 * 60) < 5, row["discounted"]
    assert row["discounted"] < 40 * 60 + 30 * 60, "the overlap was counted twice"


def test_a_running_child_of_a_held_feature_is_judged_normally(stuck_os):
    """§2.4: the claim rule withholds a DISPATCH only"""
    store = stuck_os["store"]
    _fo, _planner, kid, _at = _held_child(store, status="running", hours=5)

    verdict = _judge(stuck_os, kid["id"])

    assert verdict.excluded == "" and verdict.stuck is True


def test_a_usage_window_outranks_a_plan_hold(stuck_os):
    """§2.4's precedence: the account's window is a fact about the whole fleet"""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    store = stuck_os["store"]
    _fo, _planner, kid, at = _held_child(store)
    usage_hold(store, kid["id"], started=at)

    verdict = _judge(stuck_os, kid["id"])

    assert verdict.stuck is False and verdict.excluded == PAUSE_USAGE_LIMIT


def test_the_held_child_stays_a_visible_row_and_its_two_readers_agree(stuck_os):
    """§3(6) and §4's rejection of a `continue`: kn-143a46df"""
    store = stuck_os["store"]
    now = time.time()
    _fo, planner, kid, _at = _held_child(store)

    rows = [r for r in _judged(store, now) if r["wo"]["id"] == kid["id"]]

    assert len(rows) == 1, "the held row left the sweep's output entirely"
    assert rows[0]["verdict"].stuck is False
    assert rows[0]["verdict"].threshold_seconds == 30 * 60, "the arithmetic is gone"
    assert f"jarvis wo review {planner['id']}" in rows[0]["status_label"]
    assert f"jarvis wo review {planner['id']}" in rows[0]["blocker"]
    assert "nothing the record can name" not in rows[0]["blocker"]


def test_a_pending_child_of_a_ratified_plan_is_still_judged(stuck_os):
    """NEGATIVE, §2.5: the exclusion is the HOLD and never `pending` generally"""
    store = stuck_os["store"]
    _fo, _planner, kid, _at = _held_child(store, assumptions=0)

    verdict = _judge(stuck_os, kid["id"])

    assert verdict.excluded == "" and verdict.stuck is True


# -- an order held by an open precondition (the 2026-10-19 spec)


def _precondition_held(store, *, status: str = "pending", refs=("bl-d5baa28b",),
                       hours: float = 2.0):
    """`(wo, at)`: one order `hours` into `status`, held by a row per `refs` since `at`"""
    wo = ops.create_work_order("proj_a", "wire the actor", blocked_by=list(refs))
    at = park_order(store, wo["id"], status, hours=hours)
    store.conn.execute("UPDATE preconditions SET created_at=? WHERE wo_id=?",
                       (at, wo["id"]))
    store.conn.commit()
    return store.get_work_order(wo["id"]), at


def clear_hold(store, wo_id: str, ref: str, at: float) -> None:
    """The user's route, back-dated: `jarvis wo unblock`'s own call, cleared at `at`"""
    store.clear_precondition(ref, by="user")
    store.conn.execute("UPDATE preconditions SET cleared_at=? WHERE wo_id=? AND ref=?",
                       (at, wo_id, ref))
    store.conn.commit()


def test_an_order_held_by_an_open_precondition_is_not_stuck(stuck_os):
    """§7(2) and §7(4)"""
    from jarvis import stuck

    store = stuck_os["store"]
    wo, _at = _precondition_held(store)

    verdict = _judge(stuck_os, wo["id"])

    assert verdict.stuck is False
    assert verdict.excluded == stuck.PRECONDITION_CAUSE
    assert "clears by itself" not in verdict.reason, verdict.reason
    assert "jarvis wo unblock" in verdict.reason, verdict.reason


def test_a_cleared_precondition_is_judged_and_its_held_seconds_still_discounted(stuck_os):
    """§3.1 and §7(5): EXCLUSION ALONE IS NOT THE FIX"""
    store = stuck_os["store"]
    now = time.time()
    wo, _at = _precondition_held(store, hours=2)
    clear_hold(store, wo["id"], "bl-d5baa28b", now - 60)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]

    assert row["verdict"].excluded == "", "the cleared hold still excluded the order"
    assert abs(row["verdict"].active_seconds - 60) < 5, row["verdict"].active_seconds
    assert row["verdict"].stuck is False, row["verdict"].reason


def test_precondition_hold_spans_are_row_shaped_and_clipped_to_the_status_span():
    """§3.2 and §7(1): pure over rows, spans and never a total, clipped to `[since, now]`"""
    from jarvis.project_store import precondition_hold_spans

    def row(created_at, cleared_at=None):
        return {"created_at": created_at, "cleared_at": cleared_at}

    assert precondition_hold_spans([row(150.0)], 100.0, 200.0) == [(150.0, 200.0)]
    assert precondition_hold_spans([row(150.0, 170.0)], 100.0, 200.0) == [(150.0, 170.0)]
    assert precondition_hold_spans([row(50.0)], 100.0,
                                   200.0) == [(100.0, 200.0)], "the normal case was lost"
    assert precondition_hold_spans([row(50.0, 80.0)], 100.0, 200.0) == []
    assert precondition_hold_spans([], 100.0, 200.0) == []
    # Overlapping rows are all returned and sorted — the union is `union_seconds`' job
    assert precondition_hold_spans([row(160.0), row(120.0, 180.0)], 100.0, 200.0) == [
        (120.0, 180.0), (160.0, 200.0)]


def test_overlapping_usage_and_precondition_holds_are_discounted_once(stuck_os):
    """§3.2: the UNION of the interval sets, never the sum"""
    store = stuck_os["store"]
    now = time.time()
    wo, at = _precondition_held(store, hours=1)
    clear_hold(store, wo["id"], "bl-d5baa28b", at + 40 * 60)
    usage_hold(store, wo["id"], started=at + 20 * 60, ended=at + 50 * 60)

    row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]

    assert abs(row["discounted"] - 50 * 60) < 5, row["discounted"]
    assert row["discounted"] < 40 * 60 + 30 * 60, "the overlap was counted twice"


def test_a_running_order_with_an_open_precondition_is_judged_normally(stuck_os):
    """§5 and §7(6): the row withholds a DISPATCH"""
    store = stuck_os["store"]
    wo, _at = _precondition_held(store, status="running", hours=5)

    verdict = _judge(stuck_os, wo["id"])

    assert verdict.excluded == "" and verdict.stuck is True


def test_a_validating_order_is_never_excluded_by_a_precondition_row(stuck_os):
    """§5's gate is also why §3.3's CI and base-red ranks cannot collide with this one: those two holds..."""
    store = stuck_os["store"]
    wo, _at = _precondition_held(store, status="validating", hours=5)

    verdict = _judge(stuck_os, wo["id"])

    assert verdict.excluded == "" and verdict.stuck is True


def test_a_usage_window_outranks_a_precondition(stuck_os):
    """§3.3's precedence: the account's window is a fact about the whole fleet"""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    store = stuck_os["store"]
    wo, at = _precondition_held(store)
    usage_hold(store, wo["id"], started=at)

    verdict = _judge(stuck_os, wo["id"])

    assert verdict.stuck is False and verdict.excluded == PAUSE_USAGE_LIMIT


def test_a_precondition_outranks_a_live_dependency(stuck_os):
    """§3.3, Neo 1982: the cause the sweep reports matches the sentence `status_label` prints on the same..."""
    from jarvis import stuck

    store = stuck_os["store"]
    dep = ops.create_work_order("proj_a", "the dependency")
    child = ops.create_work_order("proj_a", "held and behind an edge",
                                  depends_on=[dep["id"]])
    store.add_precondition(child["id"], "bl-d5baa28b")
    park_order(store, child["id"], "pending", hours=5)

    verdict = _judge(stuck_os, child["id"])

    assert verdict.stuck is False and verdict.excluded == stuck.PRECONDITION_CAUSE


def test_a_precondition_outranks_a_plan_hold(stuck_os):
    """§3.3: above `PLAN_ASSUMPTION_CAUSE` too, for the same one-ranking reason"""
    from jarvis import stuck

    store = stuck_os["store"]
    _fo, _planner, kid, _at = _held_child(store)
    store.add_precondition(kid["id"], "bl-d5baa28b")
    store.conn.commit()

    verdict = _judge(stuck_os, kid["id"])

    assert verdict.stuck is False and verdict.excluded == stuck.PRECONDITION_CAUSE


def test_the_held_order_stays_a_visible_row_and_its_two_readers_agree(stuck_os):
    """§2.2's rejection of a `continue` (kn-143a46df) and §4 and §7(7): the blocker column and..."""
    store = stuck_os["store"]
    now = time.time()
    wo, _at = _precondition_held(store)

    rows = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]]

    assert len(rows) == 1, "the held row left the sweep's output entirely"
    assert rows[0]["verdict"].stuck is False
    assert rows[0]["verdict"].threshold_seconds == 30 * 60, "the arithmetic is gone"
    assert f"jarvis wo unblock {wo['id']}" in rows[0]["status_label"]
    assert f"jarvis wo unblock {wo['id']}" in rows[0]["blocker"]
    assert "nothing the record can name" not in rows[0]["blocker"]


def test_assess_is_pure():
    """§1: the predicate reaches no store, no model and not even the clock"""
    import ast
    from pathlib import Path

    from jarvis import stuck

    names: set[str] = set()
    for node in ast.walk(ast.parse(Path(stuck.__file__).read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").lstrip(".").split(".")[0])
            names.update(a.name for a in node.names)
    forbidden = {"ops", "daemon", "claude_cli", "time"}
    assert not names & forbidden, sorted(names & forbidden)
    assert not [n for n in names if n.endswith("_store")], sorted(names)


def test_a_projects_own_cadence_is_honoured(jarvis_home, project, tmp_path):
    """Neo 1086 OVERRIDES §5: the cadence is a per-project catalog config"""
    import jarvis.catalog as catalog_mod
    from jarvis import db
    from jarvis.central_store import CentralStore

    assert catalog_mod.DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS == 60
    cat = load_catalog(stuck_catalog(
        tmp_path, [{"name": "proj_a", "path": str(project),
                    "fleet_health": {"sweep_every_ticks": 12}}],
        name="stuck-cadence.json"))
    assert cat.os.fleet_health.sweep_every_ticks == \
        catalog_mod.DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS
    cfg = cat.projects[0].fleet_health
    assert cfg.sweep_every_ticks == 12
    assert cfg.cooldown_minutes == catalog_mod.DEFAULT_FLEET_HEALTH_COOLDOWN_MINUTES

    daemon = Daemon(cat)
    assert daemon.stuck_cadence() == 12

    def due(tick: int) -> list:
        daemon.tick_count = tick
        return [p.name for p in daemon.stuck_due_projects()]

    assert due(1) == ["proj_a"]
    assert due(2) == [], "not due again until its own 12 ticks have passed"
    assert due(13) == ["proj_a"]

    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "parked")
        park_order(store, wo["id"], "waiting_pr_merge", hours=5)
    finally:
        store.close()
    daemon.stuck_tick(None)
    central = CentralStore()
    try:
        assert db.from_json(central.get_state(Daemon.STUCK_RUN_KEY), {})["scanned"] == 1
    finally:
        central.close()


# -- a manager's by-design idle is not a standstill: docs/superpowers/specs/2026-10-12-a


def a_manager(store, *children: tuple[str, str]) -> tuple[str, str, list[str]]:
    """A feature order, its idle-capable manager and its children in the named statuses"""
    fo = store.create_feature_order("a feature", description="an ask")
    store.set_feature_status(fo["id"], "executing")
    manager = store.create_work_order("manage the feature", parent_id=fo["id"],
                                      kind="manager")
    kids = []
    for title, status in children:
        child = store.create_work_order(title, parent_id=fo["id"])
        store.set_status(child["id"], status)
        kids.append(child["id"])
    return str(fo["id"]), str(manager["id"]), kids


def stuck_row(wo_id: str, project: str = "proj_a") -> dict:
    """This order's row from `ops.stuck_report` — every open order, judged"""
    return next(r for r in ops.stuck_report(project) if r["id"] == wo_id)


def test_a_manager_with_an_outstanding_child_is_not_a_candidate(stuck_os):
    """The measured incident, wo-2db95e7c: idle 76.8h with one child in `waiting_pr_merge`"""
    from jarvis import stuck

    store = stuck_os["store"]
    _fo, manager_id, _kids = a_manager(store, ("ship it", "waiting_pr_merge"))
    park_order(store, manager_id, "idle", hours=76.8)

    stuck_os["daemon"].stuck_tick(None)

    assert ops.list_investigation_orders("proj_a", include_settled=True) == []
    row = stuck_row(manager_id)
    assert row["stuck"] is False
    assert row["excluded"] == stuck.IDLE_MANAGER_CAUSE

    # NOT a grace period: ten times the threshold again later changes nothing
    park_order(store, manager_id, "idle", hours=768.0)
    stuck_os["daemon"].stuck_tick(None)
    assert ops.list_investigation_orders("proj_a", include_settled=True) == []
    assert stuck_row(manager_id)["excluded"] == stuck.IDLE_MANAGER_CAUSE


def test_a_manager_wedged_in_a_live_status_is_still_judged(stuck_os):
    """Round 1's blocking finding: the hold covers the by-design IDLE wait and nothing else"""
    store = stuck_os["store"]
    for status in ("running", "needs_review"):
        _fo, manager_id, _kids = a_manager(store, ("ship it", "waiting_pr_merge"))
        park_order(store, manager_id, status, hours=76.8)
        row = stuck_row(manager_id)
        assert row["excluded"] == "", status
        assert row["stuck"] is True, status


def test_a_stranded_manager_is_a_candidate_once_over_threshold_and_not_before(stuck_os):
    """Neo 1603 (1): the by-design test NARROWS candidacy"""
    import jarvis.catalog as catalog_mod

    store = stuck_os["store"]
    _fo, manager_id, _kids = a_manager(store)
    fallback = catalog_mod.DEFAULT_FLEET_HEALTH_FALLBACK_MINUTES / 60.0

    park_order(store, manager_id, "idle", hours=fallback / 2)
    assert stuck_row(manager_id)["stuck"] is False, "inside its threshold"
    assert stuck_row(manager_id)["excluded"] == ""

    park_order(store, manager_id, "idle", hours=fallback * 2)
    row = stuck_row(manager_id)
    assert row["stuck"] is True and row["excluded"] == ""


def test_either_queued_reading_holds_a_manager_back(stuck_os):
    """`_manager_handoff`'s two guards, one module along"""
    from jarvis import bus, stuck

    store = stuck_os["store"]
    fo_id, manager_id, _kids = a_manager(store)
    park_order(store, manager_id, "idle", hours=76.8)
    assert stuck_row(manager_id)["stuck"] is True, "nothing queued yet: stranded"
    store.queue_message(manager_id, "your feature needs this")
    assert not bus.nothing_queued_to_manager(store, fo_id, manager_id)
    assert stuck_row(manager_id)["excluded"] == stuck.IDLE_MANAGER_CAUSE

    other_fo, other_manager, _ = a_manager(store)
    park_order(store, other_manager, "idle", hours=76.8)
    assert stuck_row(other_manager)["stuck"] is True
    bus.post(store, subject=bus.Subject(fo_id=other_fo), from_role="reconciler",
             to_role="manager",
             payload=bus.ChildrenLanded(round=1, children=("wo-x",), note="landed"))
    assert not bus.nothing_queued_to_manager(store, other_fo, other_manager)
    assert stuck_row(other_manager)["excluded"] == stuck.IDLE_MANAGER_CAUSE


def test_a_superseded_child_does_not_hold_a_manager_but_an_exhausted_one_does(stuck_os):
    """The two places `outstanding_feature_children` differs from its neighbours:..."""
    from jarvis import stuck

    store = stuck_os["store"]
    fo_id, manager_id, kids = a_manager(store, ("answered for", "waiting_pr_merge"))
    park_order(store, manager_id, "idle", hours=76.8)
    store.supersede_children(fo_id, kids, note="the user answered for it")
    row = stuck_row(manager_id)
    assert row["excluded"] == "" and row["stuck"] is True

    _fo2, broke_id, _k2 = a_manager(store, ("out of money", "budget_exhausted"))
    park_order(store, broke_id, "idle", hours=76.8)
    assert stuck_row(broke_id)["excluded"] == stuck.IDLE_MANAGER_CAUSE


def test_assess_exclusion_precedence_is_kind_then_fleet_then_feature():
    """§1's precedence, over scalars: an exempt kind is not a subject at all"""
    from jarvis import stuck
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    def judged(**flags):
        return stuck.assess("idle", 10 * HOUR, 10 * HOUR, 0.0, {"idle": HOUR}, HOUR,
                            **flags)

    all_three = judged(kind="investigator", exempt_kinds=("investigator",),
                       excluded_cause=PAUSE_USAGE_LIMIT, by_design_idle=True)
    fleet_and_feature = judged(kind="manager", exempt_kinds=("investigator",),
                               excluded_cause=PAUSE_USAGE_LIMIT, by_design_idle=True)
    feature_only = judged(kind="manager", exempt_kinds=("investigator",),
                          by_design_idle=True)

    assert all_three.excluded == stuck.HOLD_EXEMPT_KIND
    assert fleet_and_feature.excluded == PAUSE_USAGE_LIMIT
    assert feature_only.excluded == stuck.IDLE_MANAGER_CAUSE
    for verdict in (all_three, fleet_and_feature, feature_only):
        assert verdict.stuck is False
        assert verdict.active_seconds == 10 * HOUR
        assert verdict.threshold_seconds == HOUR

    # The control: the same arithmetic with nothing excluding it IS stuck
    assert judged(kind="manager").stuck is True


def test_an_exempt_kind_is_an_excluded_row_not_an_absence(stuck_os, jarvis_home, project,
                                                          tmp_path):
    """§4: the `continue` emitted nothing"""
    from jarvis import stuck

    store = stuck_os["store"]
    wo = store.create_work_order("look into wo-1", kind="investigator")
    park_order(store, wo["id"], "waiting_pr_merge", hours=76.8)

    stuck_os["daemon"].stuck_tick(None)

    assert ops.list_investigation_orders("proj_a", include_settled=True) == []
    row = stuck_row(wo["id"])
    assert row["stuck"] is False
    assert row["excluded"] == stuck.HOLD_EXEMPT_KIND
    assert "investigator" in row["reason"]

    # The negative control, through the SAME two surfaces the fleet reads
    def swept(catalog) -> list[str]:
        run: dict = {"scanned": 0}
        return [c["wo"]["id"] for c in Daemon(catalog)._stuck_candidates(
            catalog.projects[0], None, time.time(), run)]

    assert swept(load_catalog(stuck_os["path"])) == [], "exempt: never a candidate"

    cat = load_catalog(stuck_catalog(
        tmp_path, [{"name": "proj_a", "path": str(project),
                    "fleet_health": {"exempt_kinds": []}}], name="no-exempt.json"))
    assert cat.projects[0].fleet_health.exempt_kinds == ()
    row = stuck_row(wo["id"])  # absent entirely under a hardcoded skip
    assert row["stuck"] is True and row["excluded"] == ""
    assert swept(cat) == [wo["id"]]


# -- a worker turn in flight: issue #1059


def test_a_turn_in_flight_is_not_stuck():
    """§8.1, the incident's scalars: 10.6 minutes of tool calls past the 5-minute `running` threshold"""
    from jarvis import stuck

    judged = stuck.assess("running", 10.6 * 60, 10.6 * 60, 0.0, {"running": 5 * 60},
                          HOUR, turn_in_flight=True)

    assert judged.stuck is False
    assert judged.excluded == stuck.TURN_IN_FLIGHT_CAUSE
    assert judged.clock == "activity"
    assert judged.active_seconds == 10.6 * 60
    assert judged.threshold_seconds == 5 * 60
    assert "writes nothing to the timeline" in judged.reason


def test_running_with_no_turn_in_flight_is_judged_as_today():
    """§8.2: the exclusion must not generalise to every `running` order"""
    from jarvis import stuck

    judged = stuck.assess("running", 10.6 * 60, 10.6 * 60, 0.0, {"running": 5 * 60},
                          HOUR)

    assert judged.stuck is True
    assert judged.excluded == ""
    assert judged.reason == ("0.2h of active time past the 0.1h threshold for running, "
                             "judged on time since its last activity")


def test_assess_precedence_puts_turn_in_flight_last():
    """§5: a fleet-wide fact outranks a fact about one order"""
    from jarvis import stuck
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    def judged(**flags):
        return stuck.assess("running", 10 * HOUR, 10 * HOUR, 0.0, {"running": HOUR},
                            HOUR, **flags)

    all_flags = judged(kind="investigator", exempt_kinds=("investigator",),
                       excluded_cause=PAUSE_USAGE_LIMIT, by_design_idle=True,
                       turn_in_flight=True)
    fleet_and_turn = judged(excluded_cause=PAUSE_USAGE_LIMIT, turn_in_flight=True)
    idle_and_turn = judged(by_design_idle=True, turn_in_flight=True)
    turn_only = judged(turn_in_flight=True)

    assert all_flags.excluded == stuck.HOLD_EXEMPT_KIND
    assert fleet_and_turn.excluded == PAUSE_USAGE_LIMIT
    assert idle_and_turn.excluded == stuck.IDLE_MANAGER_CAUSE
    assert turn_only.excluded == stuck.TURN_IN_FLIGHT_CAUSE
    for verdict in (all_flags, fleet_and_turn, idle_and_turn, turn_only):
        assert verdict.stuck is False
        assert verdict.active_seconds == 10 * HOUR
        assert verdict.threshold_seconds == HOUR

    # The control: the same arithmetic with nothing excluding it IS stuck
    assert judged().stuck is True


def test_a_live_turn_buys_no_investigation(stuck_os):
    """§8.4 and §8.5: a real `running` turn row excludes"""
    from jarvis import stuck

    store = stuck_os["store"]
    now = time.time()
    wo = ops.create_work_order("proj_a", "116 bash calls deep")
    at = park_order(store, wo["id"], "running", hours=0.2)
    running_turn(store, wo["id"], started=at)
    control = ops.create_work_order("proj_a", "nothing in flight at all")
    park_order(store, control["id"], "running", hours=0.2)

    stuck_os["daemon"].stuck_tick(None)

    assert len(ops.list_investigation_orders("proj_a", include_settled=True)) == 1
    assert not ops.live_investigation("proj_a", wo["id"])
    assert ops.live_investigation("proj_a", control["id"])
    # The anti-`continue` assertion (§7): an excluded row is still a judged row
    row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]
    assert row["verdict"].excluded == stuck.TURN_IN_FLIGHT_CAUSE
    assert row["verdict"].stuck is False
    assert abs(row["verdict"].active_seconds - 12 * 60) < 60


def test_a_live_turn_outside_running_is_still_judged(stuck_os):
    """§6: a `needs_review` wait is owed by the USER"""
    store = stuck_os["store"]
    now = time.time()
    for status in ("needs_review", "idle"):
        wo = ops.create_work_order("proj_a", f"a turn row while {status}")
        at = park_order(store, wo["id"], status, hours=5)
        running_turn(store, wo["id"], started=at)

        row = [r for r in _judged(store, now) if r["wo"]["id"] == wo["id"]][0]

        assert row["verdict"].excluded == "", status
        assert row["verdict"].stuck is True, status


# -- the idle feature manager: issue #1008


def _scan(store, *, stuck_only: bool = False) -> tuple[int, list[dict]]:
    """`ops.stuck_scan` over proj_a at the shipped `fleet_health` numbers"""
    return ops.stuck_scan(store, "proj_a", catalog.FleetHealthConfig(), time.time(),
                          stuck_only=stuck_only)


def _manager(store, fo_status: str = "executing", hours: float = 5.0,
             status: str = "idle", kind: str = "manager") -> dict:
    fo_id = _feature(store, status=fo_status, carrier=False)
    wo = store.create_work_order("manage it", parent_id=fo_id, kind=kind)
    park_order(store, wo["id"], status, hours=hours)
    return store.get_work_order(wo["id"])


def test_an_idle_manager_of_an_open_feature_is_excluded(stuck_os):
    store = stuck_os["store"]
    manager = _manager(store)
    # Neo q1603: the hold needs something OUTSTANDING, so the feature gets a live sibling
    store.create_work_order("ship it", parent_id=manager["parent_id"],
                            status="waiting_pr_merge")

    scanned, rows = _scan(store)
    assert scanned == 2
    row = next(r for r in rows if r["wo"]["id"] == manager["id"])
    assert row["verdict"].excluded == ops.IDLE_MANAGER_CAUSE
    assert not row["verdict"].stuck

    # The row stays scanned and stays in the full output; only `stuck_only` drops it
    assert _scan(store, stuck_only=True) == (2, [])


def test_an_idle_manager_with_nothing_outstanding_is_still_a_candidate(stuck_os):
    """Neo q1603: a live feature with nothing outstanding and nothing queued STRANDS its manager"""
    store = stuck_os["store"]
    manager = _manager(store)

    rows = {r["wo"]["id"]: r for r in _scan(store)[1]}
    assert rows[manager["id"]]["verdict"].excluded == ""
    assert rows[manager["id"]]["verdict"].stuck is True


def test_an_idle_manager_of_a_settled_feature_is_still_a_candidate(stuck_os):
    """The other half: once the feature cannot message it, nothing will wake it"""
    from jarvis.project_store import FO_TERMINAL_STATUSES

    for fo_status in FO_TERMINAL_STATUSES:
        store = stuck_os["store"]
        manager = _manager(store, fo_status=fo_status)
        rows = {r["wo"]["id"]: r for r in _scan(store, stuck_only=True)[1]}
        assert manager["id"] in rows, fo_status
        assert rows[manager["id"]]["verdict"].excluded == ""
        assert rows[manager["id"]]["verdict"].stuck


def test_the_carve_out_does_not_widen_past_idle_managers(stuck_os):
    """Neither an idle ORDINARY order nor a manager that is not idle: both are judged"""
    store = stuck_os["store"]
    plain = _manager(store, kind="worker")
    busy = _manager(store, status="running")

    rows = {r["wo"]["id"]: r for r in _scan(store)[1]}
    for wo in (plain, busy):
        assert rows[wo["id"]]["verdict"].excluded == ""
        assert rows[wo["id"]]["verdict"].stuck


def test_manager_awaiting_feature_needs_a_manager_and_a_live_feature(stuck_os):
    store = stuck_os["store"]
    fo_id = _feature(store, carrier=False)

    orphan = store.create_work_order("no feature at all", kind="manager")
    assert not ops.manager_awaiting_feature(store, orphan)

    worker = store.create_work_order("not a manager", parent_id=fo_id)
    assert not ops.manager_awaiting_feature(store, worker)

    manager = store.create_work_order("manage it", parent_id=fo_id, kind="manager")
    assert ops.manager_awaiting_feature(store, manager)
    # scheduled_actor passes now= to every predicate: accepted, changes nothing
    assert ops.manager_awaiting_feature(store, manager, now=1234.5) is (
        ops.manager_awaiting_feature(store, manager)
    )

    # A feature deleted out from under its manager: KeyError, not a candidate
    assert not ops.manager_awaiting_feature(
        store, {**manager, "parent_id": "fo-does-not-exist"})


# -- the free re-assertion: a stale look at a unit the OS can already explain


def _blocked(store, title="the blocked one", **fields) -> str:
    """A work order whose stillness the OS can already explain: an unmet dependency"""
    dep = store.create_work_order("the thing it waits on", origin="injected",
                                  status="running")["id"]
    return _wo(store, title, status="pending", depends_on=[dep], **fields)


def test_a_stale_unchanged_finding_on_a_blocked_order_is_re_asserted_for_free(
        started, catalog_file, fake_claude, store, clock):
    """THE DEFECT ITSELF: the second look learns nothing the fingerprint did not already say"""
    _enable(catalog_file)
    wo_id = _blocked(store)
    daemon = started()

    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == 1
    (paid,) = _reviews(store, subject_id=wo_id)
    assert paid["outcome"] == "findings"

    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 1, "the restatement must be free"
    paid, free = _reviews(store, subject_id=wo_id)
    assert free["trigger"] == "re-assert"
    assert free["fingerprint"] == paid["fingerprint"]
    assert [free[k] for k in ("outcome", "findings", "detail")] == \
           [paid[k] for k in ("outcome", "findings", "detail")], (
        "a re-assertion that records nothing is a silent drop")


def test_a_prior_clear_still_buys_the_stale_call(
        started, catalog_file, fake_claude, store, clock):
    """A clear verdict found nothing, so there is no prior judgement to copy forward"""
    _enable(catalog_file)
    wo_id = _blocked(store, description="FORCE_HEALTH_CLEAR")
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in _reviews(store, subject_id=wo_id)] == \
           ["first-look", "stale"]


def test_an_unexplained_still_order_still_buys_the_stale_call(
        started, catalog_file, fake_claude, store, clock):
    """The regression that would hide a genuinely unexplained stall: no dependency"""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in _reviews(store, subject_id=wo_id)] == \
           ["first-look", "stale"]


def test_a_changed_fingerprint_always_buys_a_call(
        started, catalog_file, fake_claude, store, clock):
    """The gate sits INSIDE the stale clause: a unit that moved is judged afresh however well the OS can..."""
    _enable(catalog_file)
    wo_id = _blocked(store)
    daemon = started()

    _sweep(daemon, clock)
    store.add_event(wo_id, "note", {"n": 1})
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in _reviews(store, subject_id=wo_id)] == \
           ["first-look", "changed"]


def test_a_re_assertion_raises_no_alarm_and_puts_no_flag_back(
        started, catalog_file, fake_claude, store, clock):
    """§6.3's wallpaper failure, arriving through the free path: the flag must not come straight back"""
    _enable(catalog_file)
    wo_id = _blocked(store)
    daemon = started()

    _sweep(daemon, clock)
    findings_before = _findings(store)
    fp = _reviews(store, subject_id=wo_id)[0]["fingerprint"]
    reported_before = store.probes_reported_at("work_order", wo_id, fp)
    store.clear_attention(wo_id)
    finding_events = len(store.events_of_kind(wo_id, "health_finding"))
    reviewed_events = len(store.events_of_kind(wo_id, "health_reviewed"))

    _sweep(daemon, clock)

    # BY ID, never by row: the cost-review drain claims and decides an alarm of its own accord
    assert [a["id"] for a in _findings(store)] == [a["id"] for a in findings_before]
    assert store.get_work_order(wo_id)["needs_attention"] == 0
    assert len(store.events_of_kind(wo_id, "health_finding")) == finding_events
    assert len(store.events_of_kind(wo_id, "health_reviewed")) == reviewed_events, (
        "no judge looked, so nothing says one did")
    assert store.probes_reported_at("work_order", wo_id, fp) == reported_before


def test_each_blocker_id_comes_from_its_canonical_source(store, monkeypatch):
    """EVERY ID IS REUSED, NOT RE-DERIVED — which is how two surfaces come to disagree about one order"""
    from jarvis import db as db_mod, health, invariants, ops as ops_mod, worker_session

    cfg = catalog.SupervisorConfig()

    def subject(wo_id):
        return {"kind": "work_order", "row": store.get_work_order(wo_id)}

    plain = _wo(store, "plain", status="running")
    assert health.blocker(store, subject(plain), cfg) is None, (
        "a running order with nothing known about it is unexplained and stays paid")

    # 1
    with monkeypatch.context() as m:
        m.setattr(invariants, "true_blockers",
                  lambda s, wo, now=None: ["3 assumptions pending your review"])
        assert health.blocker(store, subject(plain), cfg) == "user"

    # 2
    blocked = _blocked(store)
    assert health.blocker(store, subject(blocked), cfg) == "dependency"
    with monkeypatch.context() as m:
        m.setattr(ops_mod, "blocked_by", lambda s, wo: [])
        assert health.blocker(store, subject(blocked), cfg) is None

    # 3
    parked = _wo(store, "parked", status="waiting_pr_merge")
    store.update_work_order(parked, pr_url="https://example.invalid/pr/1")
    assert health.blocker(store, subject(parked), cfg) == "pull-request"
    store.update_work_order(parked, pr_url="")
    assert health.blocker(store, subject(parked), cfg) is None

    # 4
    from jarvis import holds

    assert set(health.transport_blockers().values()) == holds.TRANSPORT, (
        "a fourth transport cause fails here, not as an unexplained stall")
    for cause, expected in ((worker_session.PAUSE_USAGE_LIMIT, "usage-limit"),
                            (worker_session.PAUSE_TRANSIENT, "api-outage"),
                            (worker_session.PAUSE_AUTH, "expired-login")):
        held = _wo(store, f"held by {cause}", status="running")
        with monkeypatch.context() as m:
            m.setattr(db_mod, "now", lambda: time.time() - 600)
            store.add_event(held, "turn_paused", {"seq": 1, "reason": cause})
        assert health.blocker(store, subject(held), cfg) == expected
        narrowed = catalog.SupervisorConfig(health_reassert_blockers=("usage-limit",))
        assert health.blocker(store, subject(held), narrowed) == (
            expected if expected == "usage-limit" else None), (
            "a catalog naming only the usage limit has said nothing about the others")

    # 5
    asking = _wo(store, "asking", status="running")
    store.add_assumption(asking, "I assumed the console is the surface")
    with monkeypatch.context() as m:
        m.setattr(invariants, "true_blockers", lambda s, wo, now=None: [])
        assert health.blocker(store, subject(asking), cfg) == "assumptions"


def test_a_narrowed_blocker_set_buys_the_stale_call_again(
        started, catalog_file, fake_claude, store, clock):
    """The set is the CATALOG'S to narrow: a project that does not count a dependency as an explanation..."""
    from jarvis import health

    _enable(catalog_file,
            health_reassert_blockers=[b for b in health.BLOCKERS if b != "dependency"])
    wo_id = _blocked(store)
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in _reviews(store, subject_id=wo_id)] == \
           ["first-look", "stale"]


def test_narrowing_to_the_usage_limit_still_pays_for_an_expired_login(
        started, catalog_file, fake_claude, store, clock):
    """WHY THE TRANSPORT BLOCKER IS THREE IDS (Neo q1241): a project that counts a spent window as an..."""
    from jarvis import health, worker_session

    _enable(catalog_file, health_reassert_blockers=["usage-limit"])
    wo_id = _wo(store, "signed out", status="running")
    with _MovedNow(db.now() - 600):
        store.add_event(wo_id, "turn_paused",
                        {"seq": 1, "reason": worker_session.PAUSE_AUTH})
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in _reviews(store, subject_id=wo_id)] == \
           ["first-look", "stale"]

    # The other half of the narrowing: the cause the catalog DID name goes free
    spent = _wo(store, "window spent", status="running")
    with _MovedNow(db.now() - 600):
        store.add_event(spent, "turn_paused",
                        {"seq": 1, "reason": worker_session.PAUSE_USAGE_LIMIT})
    _sweep(daemon, clock)
    _sweep(daemon, clock)
    assert [r["trigger"] for r in _reviews(store, subject_id=spent)] == \
           ["first-look", health.REASSERT]


def test_a_re_assertion_does_not_spend_a_paid_slot(
        started, catalog_file, fake_claude, store, clock):
    """The cap bounds SPEND"""
    _enable(catalog_file, health_max_units_per_tick=1)
    blocked_id = _blocked(store)
    daemon = started()

    _sweep(daemon, clock)   # the blocked order alone: one paid first look
    assert len(_health_calls(fake_claude)) == 1
    moving_id = _wo(store, "the moving one", status="running")

    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2, "one call: the unit nothing explains"
    assert _reviews(store, subject_id=blocked_id)[-1]["trigger"] == "re-assert"
    assert _reviews(store, subject_id=moving_id)[-1]["trigger"] == "first-look"


def _blocked_feature(store, children: list[dict]) -> str:
    """A feature and its children, each entry the `create_work_order` fields for one"""
    fo_id = _feature(store)
    for i, child in enumerate(children):
        store.create_work_order(f"child {i}", parent_id=fo_id, origin="injected",
                                **child)
    return fo_id


def test_a_feature_whose_every_child_is_blocked_is_re_asserted_for_free(
        started, catalog_file, fake_claude, store, clock):
    """fo-ac00376e's triple repeat of "Nothing is moving": every non-settled child has a re-derivable..."""
    _enable(catalog_file)
    dep = store.create_work_order("the thing they wait on", origin="injected",
                                  status="running")["id"]
    fo_id = _blocked_feature(store, [{"status": "pending", "depends_on": [dep]},
                                     {"status": "completed"}])
    daemon = started()

    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == 1

    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 1
    reviews = _reviews(store, kind="feature_order", subject_id=fo_id)
    assert [r["trigger"] for r in reviews] == ["first-look", "re-assert"]
    assert reviews[1]["outcome"] == reviews[0]["outcome"]


@pytest.mark.parametrize("child", [{"status": "running"},      # one child is moving
                                   {"status": "pending"}])     # one is unexplained
def test_a_feature_with_a_running_or_unexplained_child_still_buys_the_call(
        started, catalog_file, fake_claude, store, clock, child):
    """IT FAILS CLOSED: one child moving"""
    _enable(catalog_file)
    dep = store.create_work_order("the thing it waits on", origin="injected",
                                  status="running")["id"]
    fo_id = _blocked_feature(store, [{"status": "pending", "depends_on": [dep]}, child])
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in
            _reviews(store, kind="feature_order", subject_id=fo_id)] == \
           ["first-look", "stale"]


def test_a_feature_with_no_children_at_all_stays_paid(
        started, catalog_file, fake_claude, store, clock):
    """Nothing is derived about a feature with nothing under it, so there is nothing to re-assert"""
    _enable(catalog_file)
    fo_id = _blocked_feature(store, [])
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    assert len(_health_calls(fake_claude)) == 2
    assert [r["trigger"] for r in
            _reviews(store, kind="feature_order", subject_id=fo_id)] == \
           ["first-look", "stale"]


def test_an_all_parked_project_keeps_the_os_sweep_invariant_green(
        started, catalog_file, fake_claude, store, clock, monkeypatch):
    """(d) IS DECIDED BEHAVIOUR: a re-assertion carries a copied `outcome`"""
    from pathlib import Path

    from jarvis import invariants, schedule

    _enable(catalog_file)
    _blocked(store)
    daemon = started()
    monkeypatch.setattr(schedule, "__file__",
                        str(Path(store.project_path) / "src" / "jarvis" / "schedule.py"))

    _sweep(daemon, clock)
    clock.advance(invariants.OS_HEALTH_SWEEP_DARK_MINUTES + STEP_MINUTES)
    _sweep(daemon, clock)

    assert [r["trigger"] for r in store.recent_health_reviews(1)] == ["re-assert"]
    assert list(invariants.check_os_health_sweep_alive(store)) == [], (
        "a free row is a judgement for this query, by design")

    _force_failures(store, invariants.HEALTH_SWEEP_FAILURE_RUN)
    assert list(invariants.check_health_sweep_produces_judgements(store)), (
        "the transport canary must still see a broken sweep")


# -- a merge held by a red base is not stuck


def red_base_catalog(tmp_path, *, auto_merge: bool = True, enabled: bool = True,
                     project_path: str = "", name: str = "red-base-catalog.json"):
    """A registered catalog whose proj_a opted into auto-merge — §3's gate"""
    return stuck_catalog(
        tmp_path,
        [{"name": "proj_a", "path": project_path, "description": "test project",
          "validation": {"enabled": enabled, "auto_merge": auto_merge}}],
        name=name)


@pytest.fixture()
def red_base_os(jarvis_home, project, tmp_path):
    cat = red_base_catalog(tmp_path, project_path=str(project))
    store = ProjectStore(project)
    try:
        yield {"daemon": Daemon(load_catalog(cat)), "store": store, "path": cat}
    finally:
        store.close()


def base_fact(red: bool = True, age: float = 0.0, project: str = "proj_a") -> float:
    """The project's stored base-health reading, `age` seconds old"""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    checked_at = time.time() - age
    try:
        central.set_base_health(project, {"red": red, "base": "main", "workflow": "ci",
                                          "run_url": "https://x/1", "head_sha": "abc",
                                          "wo_id": "", "checked_at": checked_at})
        central.conn.commit()
    finally:
        central.close()
    return checked_at


def red_episodes(pairs, project: str = "proj_a") -> None:
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.set_base_red_episodes(project, pairs)
        central.conn.commit()
    finally:
        central.close()


def _parked(store, hours: float = 4.9, status: str = "waiting_pr_merge",
            title: str = "parked behind a red main") -> tuple[dict, float]:
    wo = ops.create_work_order("proj_a", title)
    return wo, park_order(store, wo["id"], status, hours=hours)


def test_a_merge_held_by_a_red_base_is_excluded_with_its_own_sentence(red_base_os):
    """§5(1), wo-05e0a06d's own number"""
    from jarvis import stuck

    store = red_base_os["store"]
    now = time.time()
    wo, _at = _parked(store)
    base_fact(red=True)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["stuck"] is False, row["reason"]
    assert row["excluded"] == stuck.BASE_RED_CAUSE
    assert row["reason"] == stuck.EXCLUSION_REASON[stuck.BASE_RED_CAUSE]
    assert row["reason"] != stuck.DEFAULT_EXCLUSION_REASON.format(
        cause=stuck.BASE_RED_CAUSE)


def test_needs_review_beside_the_same_red_base_is_still_judged(red_base_os):
    """§5(2), kn-6e836793's scoping: the OS is not holding this one, the reviewer is"""
    store = red_base_os["store"]
    now = time.time()
    wo, _at = _parked(store, status="needs_review", title="the user owes this review")
    base_fact(red=True)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == ""
    assert row["stuck"] is True, row["reason"]


@pytest.mark.parametrize("off", [{"auto_merge": False}, {"enabled": False}])
def test_with_auto_merge_off_the_parked_order_stays_judged(jarvis_home, project,
                                                           tmp_path, off):
    """§3: with auto-merge off `automerge.decide` is never consulted"""
    red_base_catalog(tmp_path, project_path=str(project), **off)
    store = ProjectStore(project)
    try:
        now = time.time()
        wo, at = _parked(store)
        base_fact(red=True)
        red_episodes([[at, None]])

        row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

        assert row["excluded"] == ""
        assert row["stuck"] is True, row["reason"]
        assert row["discounted_seconds"] == 0.0
    finally:
        store.close()


def test_the_base_going_green_leaves_the_closed_episode_discounted(red_base_os):
    """§5(4), kn-6e30d561 and the whole reason for §2: THE DISCOUNT MUST OUTLIVE THE EXCLUSION BY ONE..."""
    store = red_base_os["store"]
    now = time.time()
    wo, at = _parked(store)
    base_fact(red=False)
    red_episodes([[at, at + 4.5 * HOUR]])

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == ""
    assert abs(row["discounted_seconds"] - 4.5 * HOUR) < 5, row["discounted_seconds"]
    assert abs(row["active_seconds"] - 0.4 * HOUR) < 5, row["active_seconds"]
    assert row["stuck"] is False, row["reason"]


def test_a_stale_fact_excludes_nothing_and_stops_discounting_at_the_horizon(red_base_os):
    """§5(5): a `gh` that stopped answering must not grow the discount on a fact nobody can confirm"""
    from jarvis.central_store import BASE_HEALTH_FRESH_SECONDS

    store = red_base_os["store"]
    now = time.time()
    wo, _at = _parked(store)
    checked_at = base_fact(red=True, age=BASE_HEALTH_FRESH_SECONDS + 1)
    opened = now - 20 * 60
    red_episodes([[opened, None]])

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == "", "a fact nobody can confirm excluded the order"
    assert row["stuck"] is True, row["reason"]
    horizon = checked_at + BASE_HEALTH_FRESH_SECONDS
    assert abs(row["discounted_seconds"] - (horizon - opened)) < 2, \
        row["discounted_seconds"]
    assert row["discounted_seconds"] < now - opened, "the open pair ran to `now`"


def test_a_usage_hold_inside_the_red_episode_is_discounted_once(red_base_os):
    """§5(6): the UNION and never the sum"""
    store = red_base_os["store"]
    now = time.time()
    wo, at = _parked(store)
    base_fact(red=False)
    red_episodes([[at, at + 2 * HOUR]])
    usage_hold(store, wo["id"], started=at + 0.5 * HOUR, ended=at + 1 * HOUR)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert abs(row["discounted_seconds"] - 2 * HOUR) < 5, row["discounted_seconds"]
    assert row["discounted_seconds"] < 2.5 * HOUR, "the overlap was counted twice"


def test_the_held_order_is_a_visible_row_and_never_a_continue(red_base_os):
    """§5(7), kn-143a46df and kn-e91ff3a5: a `continue` has no unit test over the pure predicate and..."""
    from jarvis import stuck

    store = red_base_os["store"]
    now = time.time()
    wo, _at = _parked(store)
    base_fact(red=True)

    scanned, rows = ops.stuck_scan(store, "proj_a", catalog.FleetHealthConfig(), now,
                                   base_red=True, stuck_only=False)
    held = [r for r in rows if r["wo"]["id"] == wo["id"]]

    assert scanned == 1 and len(held) == 1, "the held row left the sweep's output"
    assert held[0]["verdict"].excluded == stuck.BASE_RED_CAUSE
    assert next(r for r in ops.stuck_report("proj_a", now)
                if r["id"] == wo["id"])["excluded"] == stuck.BASE_RED_CAUSE


@pytest.mark.parametrize("auto_merge, excluded", [(True, True), (False, False)])
def test_both_callers_derive_the_same_bool(jarvis_home, project, tmp_path, auto_merge,
                                           excluded):
    """§7's ONE derivation, and the test that would have caught a bool hoisted in one caller only:..."""
    from jarvis import stuck

    cat = red_base_catalog(tmp_path, project_path=str(project), auto_merge=auto_merge)
    store = ProjectStore(project)
    try:
        now = time.time()
        wo, _at = _parked(store)
        base_fact(red=True)
        spec = load_catalog(cat).projects[0]
        run: dict = {"scanned": 0}

        candidates = [c["wo"]["id"] for c in
                      Daemon(load_catalog(cat))._stuck_candidates(spec, None, now, run)]
        row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

        assert (wo["id"] not in candidates) is excluded, candidates
        assert (row["excluded"] == stuck.BASE_RED_CAUSE) is excluded
        assert row["stuck"] is not excluded
    finally:
        store.close()


# -- a merge held for running checks is not stuck

JUDGED_SHA = "7662d08b70aa0000000000000000000000000000"
RUNNING_REASON = "CI has not finished on this commit — 1 check(s) still running"


def am_event(store, wo_id: str, kind: str, at: float, **payload) -> None:
    """One `ops.AUTOMERGE_EVENTS` row at `at`"""
    store.add_event(wo_id, kind, payload)
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)", (at,))
    store.conn.commit()


def checks_hold(daemon, store, wo_id: str, at: float, *, code: str = "checks_running",
                round_n: int = 1, reason: str = RUNNING_REASON) -> None:
    """One `automerge_held` event, written by THE DAEMON'S OWN WRITER"""
    from jarvis import automerge

    daemon._note_automerge_held(store, wo_id, automerge.Decision(
        armed=False, code=code, reason=reason, judged_sha=JUDGED_SHA,
        head_sha=JUDGED_SHA, round_n=round_n))
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)", (at,))
    store.conn.commit()


def base_health_without_checked_at(project: str = "proj_a") -> None:
    """A stored fact carrying NO `checked_at`"""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.set_base_health(project, {"red": False, "base": "main", "workflow": "ci",
                                          "run_url": "https://x/1", "head_sha": "abc",
                                          "wo_id": ""})
        central.conn.commit()
    finally:
        central.close()


def _held_for_checks(os_, hours: float = 61 / 60, status: str = "waiting_pr_merge",
                     fact_age: float = 0.0) -> tuple[dict, float]:
    """A parked order held for the checks, beside a base-health fact `fact_age` old"""
    wo, at = _parked(os_["store"], hours=hours, status=status,
                     title="parked while GitHub runs the checks")
    checks_hold(os_["daemon"], os_["store"], wo["id"], at + 60.0)
    base_fact(red=False, age=fact_age)
    return wo, at


def test_a_merge_held_for_running_checks_is_excluded_with_its_own_sentence(red_base_os):
    """§7(1), wo-afcea414's own number"""
    from jarvis import stuck

    now = time.time()
    wo, _at = _held_for_checks(red_base_os)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["stuck"] is False, row["reason"]
    assert row["excluded"] == stuck.AUTOMERGE_CHECKS_CAUSE
    assert row["reason"] == stuck.EXCLUSION_REASON[stuck.AUTOMERGE_CHECKS_CAUSE]
    assert row["reason"] != stuck.DEFAULT_EXCLUSION_REASON.format(
        cause=stuck.AUTOMERGE_CHECKS_CAUSE)


def test_the_blocker_column_names_the_hold_the_record_already_carries(red_base_os):
    """§7(2), the §5 regression guard asserted THROUGH the sweep: this column read "nothing the record can..."""
    from jarvis import invariants

    now = time.time()
    wo, _at = _held_for_checks(red_base_os)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["blocker"] == invariants.AUTOMERGE_CHECKS_NOTE.format(
        sha=JUDGED_SHA[:10], reason=RUNNING_REASON)
    assert row["blocker"] != "nothing the record can name"


@pytest.mark.parametrize("code", ["checks_none", "checks_failed",
                                  "merge_state_unclean", "sha_moved"])
def test_every_other_hold_code_is_still_judged(red_base_os, code):
    """§7(3) and §3's boundary — the part a later change will widen by accident"""
    store = red_base_os["store"]
    now = time.time()
    wo, at = _parked(store, hours=61 / 60, title="held on something somebody owes")
    checks_hold(red_base_os["daemon"], store, wo["id"], at + 60.0, code=code)
    base_fact(red=False)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == ""
    assert row["stuck"] is True, row["reason"]


def test_a_stale_hold_is_no_hold(red_base_os):
    """§7(4): `ops.automerge_state` marks a hold from an overtaken round stale"""
    store = red_base_os["store"]
    now = time.time()
    wo, at = _parked(store, hours=4.9, title="a hold its own round overtook")
    checks_hold(red_base_os["daemon"], store, wo["id"], at + 4.8 * 3600)
    base_fact(red=False)
    for _ in range(2):
        row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
        store.close_validation_round(row["id"], "passed", "")

    judged = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert judged["excluded"] == ""
    assert judged["stuck"] is True, judged["reason"]


def test_needs_review_beside_the_same_pull_request_is_still_judged(red_base_os):
    """§7(5), kn-6e836793's scoping: a `needs_review` order is a wait the USER owes"""
    now = time.time()
    wo, _at = _held_for_checks(red_base_os, hours=4.9, status="needs_review")

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == ""
    assert row["stuck"] is True, row["reason"]


def test_a_red_base_outranks_the_running_checks(red_base_os):
    """§7(6) and §2: the two CAN co-occur, and a fleet fact outranks a fact about one order"""
    from jarvis import stuck

    now = time.time()
    wo, _at = _held_for_checks(red_base_os)
    base_fact(red=True)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == stuck.BASE_RED_CAUSE
    assert row["stuck"] is False, row["reason"]


def test_the_checks_reporting_leaves_the_closed_span_discounted(red_base_os):
    """§7(7), kn-6e30d561 and the whole reason for §4: THE DISCOUNT MUST OUTLIVE THE EXCLUSION BY ONE..."""
    store = red_base_os["store"]
    now = time.time()
    wo, at = _parked(store, hours=70 / 60, title="the checks have since reported")
    checks_hold(red_base_os["daemon"], store, wo["id"], at + 2 * 60.0)
    am_event(store, wo["id"], "automerge_proposed", at + 65 * 60.0, approval_id=1)
    base_fact(red=False)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == ""
    assert abs(row["discounted_seconds"] - 63 * 60.0) < 5, row["discounted_seconds"]
    assert abs(row["active_seconds"] - 7 * 60.0) < 5, row["active_seconds"]
    assert row["stuck"] is False, row["reason"]


@pytest.mark.parametrize("fact_age, label", [
    (90 * 60.0, "a gh outage that began before the hold"),
    (None, "no checked_at at all, or a project not opted into auto-merge"),
])
def test_a_stale_base_health_fact_un_excludes_and_stops_discounting(red_base_os,
                                                                   fact_age, label):
    """§7(8), and the test review round 1 asked for BY NAME. kn-2579843f's one rule has two halves"""
    from jarvis import invariants

    store = red_base_os["store"]
    now = time.time()
    wo, _at = _held_for_checks(red_base_os, fact_age=fact_age or 0.0)
    if fact_age is None:
        base_health_without_checked_at()

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == "", label
    assert row["stuck"] is True, row["reason"]
    assert row["discounted_seconds"] == 0.0, row["discounted_seconds"]
    assert row["blocker"] == invariants.AUTOMERGE_CHECKS_NOTE.format(
        sha=JUDGED_SHA[:10], reason=RUNNING_REASON)


def test_an_open_hold_is_discounted_only_as_far_as_the_horizon(red_base_os):
    """§7(9) and §4's outage sentence: the fact was last confirmed 20m ago"""
    store = red_base_os["store"]
    now = time.time()
    wo, _at = _held_for_checks(red_base_os, fact_age=20 * 60.0)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert abs(row["discounted_seconds"] - 55 * 60.0) < 30, row["discounted_seconds"]
    assert abs(row["active_seconds"] - 6 * 60.0) < 30, row["active_seconds"]


def test_a_usage_hold_inside_the_checks_span_is_discounted_once(red_base_os):
    """§7(10): the UNION and never the sum"""
    store = red_base_os["store"]
    now = time.time()
    wo, at = _parked(store, hours=70 / 60, title="a window shut while the checks ran")
    checks_hold(red_base_os["daemon"], store, wo["id"], at + 2 * 60.0)
    am_event(store, wo["id"], "automerge_proposed", at + 65 * 60.0, approval_id=1)
    usage_hold(store, wo["id"], started=at + 10 * 60.0, ended=at + 20 * 60.0)
    base_fact(red=False)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert abs(row["discounted_seconds"] - 63 * 60.0) < 5, row["discounted_seconds"]
    assert row["discounted_seconds"] < 73 * 60.0, "the overlap was counted twice"


def test_the_checks_held_order_is_a_visible_row_and_never_a_continue(red_base_os):
    """§7(11), kn-143a46df and kn-e91ff3a5: a `continue` has no unit test over the pure predicate and..."""
    from jarvis import stuck

    store = red_base_os["store"]
    now = time.time()
    wo, _at = _held_for_checks(red_base_os)

    scanned, rows = ops.stuck_scan(store, "proj_a", catalog.FleetHealthConfig(), now,
                                   base_red_until=now, stuck_only=False)
    held = [r for r in rows if r["wo"]["id"] == wo["id"]]

    assert scanned == 1 and len(held) == 1, "the held row left the sweep's output"
    assert held[0]["verdict"].excluded == stuck.AUTOMERGE_CHECKS_CAUSE
    assert next(r for r in ops.stuck_report("proj_a", now)
                if r["id"] == wo["id"])["excluded"] == stuck.AUTOMERGE_CHECKS_CAUSE


@pytest.mark.parametrize("code, fact_age, excluded", [
    ("checks_running", 0.0, True),
    ("checks_failed", 0.0, False),
    # Case 8 is here because the horizon is hoisted SEPARATELY in each caller
    ("checks_running", 90 * 60.0, False),
])
def test_both_callers_agree_about_the_running_checks(jarvis_home, project, tmp_path,
                                                     code, fact_age, excluded):
    """§7(12), the 2026-09-30 spec's ONE derivation: `Daemon._stuck_candidates` and `ops.stuck_report`..."""
    from jarvis import stuck

    cat = red_base_catalog(tmp_path, project_path=str(project))
    store = ProjectStore(project)
    try:
        now = time.time()
        daemon = Daemon(load_catalog(cat))
        wo, at = _parked(store, hours=61 / 60, title="both callers read this one")
        checks_hold(daemon, store, wo["id"], at + 60.0, code=code)
        base_fact(red=False, age=fact_age)
        spec = load_catalog(cat).projects[0]
        run: dict = {"scanned": 0}

        candidates = [c["wo"]["id"] for c in
                      Daemon(load_catalog(cat))._stuck_candidates(spec, None, now, run)]
        row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

        assert (wo["id"] not in candidates) is excluded, candidates
        assert (row["excluded"] == stuck.AUTOMERGE_CHECKS_CAUSE) is excluded
        assert row["stuck"] is not excluded
    finally:
        store.close()


# -- a work order at its budget cap


def _spent(store, hours: float = 2.0, status: str | None = None,
           status_ago: float = 0.0, resumed_after: float | None = None,
           assumptions: int = 0,
           title: str = "parked at its cap") -> tuple[dict, float]:
    """An order whose budget ran out `hours` ago, with the park's own event"""
    from jarvis import budget

    wo = ops.create_work_order("proj_a", title)
    for i in range(assumptions):
        store.add_assumption(wo["id"], f"assumption {i}")
    at = park_order(store, wo["id"], budget.EXHAUSTED, hours=hours)
    store.add_event(wo["id"], "budget_exhausted",
                    {"cap_usd": 50.0, "spent_usd": 50.16, "source": "work_order"}, ts=at)
    if resumed_after is not None:
        store.add_event(wo["id"], "budget_resumed", {"cap_usd": 75.0},
                        ts=at + resumed_after)
    if status is not None:
        store.set_status(wo["id"], status)
        moved = time.time() - status_ago
        store.conn.execute(
            "UPDATE wo_state_spans SET ts=? WHERE id="
            "(SELECT id FROM wo_state_spans WHERE order_id=? AND order_kind='wo' "
            "ORDER BY ts DESC, id DESC LIMIT 1)", (moved, wo["id"]))
        store.conn.execute(
            "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events)",
            (moved,))
    store.conn.commit()
    return store.get_work_order(wo["id"]), at


def test_assess_names_the_raise_and_not_a_hold_that_clears_itself():
    """§1: `DEFAULT_EXCLUSION_REASON` is FALSE here"""
    from jarvis import stuck

    judged = stuck.assess(
        "budget_exhausted", 5 * HOUR, 5 * HOUR, 5 * HOUR, {}, 0.5 * HOUR,
        excluded_cause=stuck.BUDGET_CAP_CAUSE)

    assert judged.stuck is False
    assert judged.excluded == stuck.BUDGET_CAP_CAUSE
    assert judged.reason == stuck.EXCLUSION_REASON[stuck.BUDGET_CAP_CAUSE]
    assert judged.reason != stuck.DEFAULT_EXCLUSION_REASON.format(
        cause=stuck.BUDGET_CAP_CAUSE)
    assert "jarvis wo budget" in judged.reason
    # The same wall clock with nothing holding it IS stuck: the hold is the difference
    assert stuck.assess("budget_exhausted", 5 * HOUR, 5 * HOUR, 0.0, {},
                        0.5 * HOUR).stuck


def test_a_parked_order_is_excluded_with_the_named_cause(stuck_os):
    """§7(1): wo-4091ba42's own case — $50.16 of a $50.00 cap bought inv-392bc511"""
    from jarvis import stuck

    store = stuck_os["store"]
    now = time.time()
    wo, _at = _spent(store)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["stuck"] is False, row["reason"]
    assert row["excluded"] == stuck.BUDGET_CAP_CAUSE
    assert row["reason"] == stuck.EXCLUSION_REASON[stuck.BUDGET_CAP_CAUSE]
    assert row["reason"] != stuck.DEFAULT_EXCLUSION_REASON.format(
        cause=stuck.BUDGET_CAP_CAUSE)
    # The discount is the other half
    assert abs(row["discounted_seconds"] - 2 * HOUR) < 5, row["discounted_seconds"]
    assert row["active_seconds"] < 5, row["active_seconds"]


def test_the_status_is_the_key_and_never_the_pending_assumptions(stuck_os):
    """§7(2), kn-fe8f7d5b's regression guard: an assumption owed in any other status is a wait the user..."""
    from jarvis import stuck

    store = stuck_os["store"]
    now = time.time()
    bare, _at = _spent(store, title="parked, nothing owed")
    owed, _ = _spent(store, status="needs_review", status_ago=2 * HOUR, assumptions=2,
                     title="reviewed, not parked")

    rows = {r["id"]: r for r in ops.stuck_report("proj_a", now)}

    assert rows[bare["id"]]["excluded"] == stuck.BUDGET_CAP_CAUSE
    assert rows[owed["id"]]["excluded"] == ""
    assert rows[owed["id"]]["stuck"] is True, rows[owed["id"]]["reason"]


def test_the_discount_stops_when_the_status_moves_on(stuck_os):
    """§7(3): no `budget_resumed`, so the `holds.BUDGET` episode is still OPEN"""
    store = stuck_os["store"]
    now = time.time()
    wo, _at = _spent(store, hours=5.0, status="pending", status_ago=4 * HOUR,
                     title="raised, never resumed")

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == ""
    assert row["discounted_seconds"] < 5, row["discounted_seconds"]
    assert row["stuck"] is True, row["reason"]


def test_a_usage_window_inside_the_park_is_discounted_once(stuck_os):
    """§7(4): the UNION and never the sum (`holds.union_seconds`' own docstring)"""
    store = stuck_os["store"]
    now = time.time()
    wo, at = _spent(store, hours=2.0)
    usage_hold(store, wo["id"], started=at + 0.5 * HOUR, ended=at + 1 * HOUR)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert abs(row["discounted_seconds"] - 2 * HOUR) < 5, row["discounted_seconds"]
    assert row["discounted_seconds"] < 2.5 * HOUR, "the overlap was counted twice"


def test_a_fleet_usage_window_outranks_the_budget_cap(stuck_os):
    """§3: a fact about the ACCOUNT outranks a fact about one order's money"""
    from jarvis import stuck
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    store = stuck_os["store"]
    now = time.time()
    wo, at = _spent(store, hours=2.0)
    usage_hold(store, wo["id"], started=at)

    row = next(r for r in ops.stuck_report("proj_a", now) if r["id"] == wo["id"])

    assert row["excluded"] == PAUSE_USAGE_LIMIT
    assert row["excluded"] != stuck.BUDGET_CAP_CAUSE
    assert row["stuck"] is False, row["reason"]


def test_the_parked_order_is_a_visible_row_and_never_a_continue(stuck_os):
    """§7(5), kn-143a46df: a `continue` produces no `Verdict`"""
    from jarvis import stuck

    store = stuck_os["store"]
    now = time.time()
    wo, _at = _spent(store)

    scanned, rows = ops.stuck_scan(store, "proj_a", catalog.FleetHealthConfig(), now,
                                   stuck_only=False)
    parked = [r for r in rows if r["wo"]["id"] == wo["id"]]

    assert scanned == 1 and len(parked) == 1, "the parked row left the sweep's output"
    assert parked[0]["verdict"].excluded == stuck.BUDGET_CAP_CAUSE
    assert next(r for r in ops.stuck_report("proj_a", now)
                if r["id"] == wo["id"])["excluded"] == stuck.BUDGET_CAP_CAUSE


def test_the_parked_order_buys_no_investigation_and_no_new_write(stuck_os):
    """§7(8), kn-1e65d22d rule 2: the debt is already surfaced"""
    store, daemon = stuck_os["store"], stuck_os["daemon"]
    wo, _at = _spent(store, hours=5.0)
    def notified() -> int:
        return int(store.conn.execute(
            "SELECT COUNT(*) FROM notifications").fetchone()[0])

    before = (store.count_events(wo["id"]), notified())

    daemon.stuck_tick(None)

    assert ops.list_investigation_orders("proj_a", include_settled=True) == []
    assert (store.count_events(wo["id"]), notified()) == before


# Written for the opt-in OS (os.autonomy.self_dispatch on, stuck sweep on)
pytestmark = pytest.mark.usefixtures("self_dispatch_on")
