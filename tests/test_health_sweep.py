"""The health sweep: what makes the OS look at an open unit, and what it raises.

§4 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.

WHAT THESE TESTS ARE BUILT TO AVOID, and it is worse here than for the cost review.
The sweep ships DOUBLY disabled, so a test that reaches the daemon without turning both
switches on exercises nothing and still passes — every assertion is therefore on CALL
COUNTS and ROWS. And a sweep shares the supervisor's persona, so
`test_supervisor.py::_supervisor_calls` counts sweeps as cost reviews: `_health_calls`
below keys on the CHECKLIST, which only a sweep's system prompt carries, and every
call-count assertion in this file would otherwise mean nothing.
"""

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

#: How the fake `claude` tells a sweep from a cost review, and how this file does.
CHECKLIST = probes_mod.render_checklist(probes_mod.DEFAULT_PROBES).splitlines()[0]


#: The floors, shrunk to their legal minimum. `catalog._parse_supervisor` refuses any
#: supervisor integer below 1, so a sweep test CANNOT make time vanish — it moves the
#: clock instead, which is also the only way to reach the `stale` trigger at all.
FLOOR_MINUTES = 1
STEP_MINUTES = 2 * FLOOR_MINUTES


def _enable(catalog_file, **overrides) -> None:
    """Turn BOTH switches on. Never a fixture default — the disabled pin below depends
    on reaching the daemon without this call exercising anything."""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True, "health_enabled": True,
                                "health_min_interval_minutes": FLOOR_MINUTES,
                                "health_stale_minutes": FLOOR_MINUTES, **overrides}
    catalog_file.write_text(json.dumps(data))


class _Clock:
    """A hand-advanced `db.now`, installed for the whole test.

    Every trigger is measured in minutes, so a sweep that ran on the real clock would
    be `first-look` once and then silent for ever. Advancing this instead is what makes
    the second and third looks in the dedupe test genuinely happen — and it is the
    fixture-safe shape: a bare `monkeypatch.undo()` reverts `jarvis_home` and
    `catalog_file` too, and every surface then renders as an empty OS with no error
    anywhere.
    """

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
    out = []
    for call in fake_claude.calls:
        argv = call.get("argv") or []
        if "--append-system-prompt" in argv:
            if CHECKLIST in argv[argv.index("--append-system-prompt") + 1]:
                out.append(call)
    return out


def _agent_calls(kind: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        return [dict(r) for r in central.conn.execute(
            "SELECT * FROM agent_calls WHERE kind=?", (kind,)).fetchall()]
    finally:
        central.close()


def _sweep(daemon, clock, timeout: float = 20.0) -> None:
    """One sweep tick, waited out on the daemon's OWN guard rather than on a sleep.

    The clock moves first, past the floors: nothing is due at the instant it is created.
    `tick_count` is set because the cadence gate is `tick_count % health_every_ticks
    == 1` and the shipped cadence is every twentieth tick — a test that left it at 0
    would sweep nothing and pass.
    """
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
    """One feature order and, by default, a SETTLED carrier for it.

    The carrier is `completed` on purpose: an open one is a work-order candidate in its
    own right, and every "exactly one call" assertion about a feature would then be
    counting two.
    """
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


# -- `due`: the pure spend decision ----------------------------------------------------


def test_a_unit_younger_than_the_floor_is_not_looked_at_at_all():
    """The floor applies to the FIRST look too: a unit created moments ago has nothing
    to say yet, and the sweep is the standing cost of watching."""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30)
    minute = 60.0
    assert due(None, "fp", cfg, now=29 * minute, created=0.0) is None
    assert due(None, "fp", cfg, now=31 * minute, created=0.0) == "first-look"


def test_a_moved_fingerprint_is_changed_and_a_still_one_is_stale_and_neither_is_early():
    """The three triggers and the two floors, in one test, because each of the three
    is green on its own against a function that returns a constant."""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30,
                                   health_stale_minutes=720)
    minute = 60.0
    old = {"ts": 0.0, "fingerprint": "before"}

    assert due(old, "after", cfg, now=29 * minute, created=0.0) is None
    assert due(old, "after", cfg, now=31 * minute, created=0.0) == "changed"
    # Unchanged and young: nothing has happened, and nothing has been still long enough
    # for the stillness to be the news.
    assert due(old, "before", cfg, now=31 * minute, created=0.0) is None
    assert due(old, "before", cfg, now=721 * minute, created=0.0) == "stale"


def test_the_stale_clause_is_what_earns_the_feature():
    """A pure delta trigger never fires on "nothing has changed for three days", which
    is the most valuable health signal this OS has."""
    cfg = catalog.SupervisorConfig(health_min_interval_minutes=30,
                                   health_stale_minutes=720)
    three_days = 3 * 24 * 60 * 60.0
    assert due({"ts": 0.0, "fingerprint": "frozen"}, "frozen", cfg,
               now=three_days, created=0.0) == "stale"


# -- `fingerprint`: cheap, deterministic, and it moves when the unit does ---------------


def test_the_fingerprint_moves_for_every_thing_a_work_order_can_do(store):
    """Each component asserted by MUTATING it, because a fingerprint that ignored one
    of them would still be perfectly stable and perfectly deterministic."""
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

    # And it is a FUNCTION of the state, not of the clock: re-reading unchanged state
    # gives the same value, which is the whole basis of the dedupe.
    assert moved() == moved()


def test_the_os_watching_a_unit_does_not_count_as_the_unit_moving(store):
    """The subtlest rule in the section, pinned on its own because the dedupe test that
    also catches it takes four sweeps to say so.

    A raise writes `health_finding`, `health_reviewed` and — through `flag_attention` —
    an `attention` event onto the very unit being fingerprinted. Count any of them and
    the fingerprint moves because it was looked at, and the dedupe can never engage.
    """
    from jarvis.project_store import ALARM_EVENT_KINDS

    wo_id = _wo(store)
    subject = {"kind": "work_order", "row": store.get_work_order(wo_id)}
    before = fingerprint(store, subject)

    for kind in ALARM_EVENT_KINDS:
        store.add_event(wo_id, kind, {})
    store.flag_attention(wo_id, "look at me")
    after = {"kind": "work_order", "row": store.get_work_order(wo_id)}
    assert fingerprint(store, after) == before

    # The positive partner, in the same test: a fingerprint that ignored EVERY event
    # would pass the assertion above and be useless.
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


# -- it ships off, and the pin needs its sibling ---------------------------------------


def test_with_the_catalog_untouched_the_sweep_does_nothing_at_all(
        started, fake_claude, store, clock):
    """UNFALSIFIABLE ON ITS OWN — green on an empty diff and green on a working one.
    Its partner is the test directly below, over the same fixture with both switches on;
    only the PAIR says anything."""
    _wo(store, status="running")
    _feature(store)

    _sweep(started(), clock)

    assert _health_calls(fake_claude) == []
    assert _agent_calls("health") == []
    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 0
    assert store.alarms_across() == []


def test_with_both_switches_on_the_same_fixture_produces_all_four(
        started, catalog_file, fake_claude, store, clock):
    """The sibling that makes the pin above falsifiable."""
    _enable(catalog_file)
    _wo(store, status="running")

    _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1
    assert len(_agent_calls("health")) == 1
    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 1
    assert len(_findings(store)) == 1


def test_the_cost_review_switch_alone_arms_nothing(started, catalog_file, fake_claude,
                                                   store, clock):
    """`health_enabled` sits ON TOP of `enabled`: a project may want a reviewer for its
    cost alarms without also paying the standing cost of watching."""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True,
                                "health_min_interval_minutes": FLOOR_MINUTES}
    catalog_file.write_text(json.dumps(data))
    _wo(store, status="running")

    _sweep(started(), clock)

    assert _health_calls(fake_claude) == []
    assert _findings(store) == []


# -- the done condition ----------------------------------------------------------------


def test_one_tick_makes_one_call_and_leaves_one_finding_on_the_record(
        started, catalog_file, fake_claude, store, clock):
    """ONE MODEL CALL PER UNIT, NOT ONE PER PROBE — with three probes armed for a work
    order, the call count is what proves it, and nothing else can."""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")

    _sweep(started(), clock)

    (call,) = _health_calls(fake_claude)
    system = call["argv"][call["argv"].index("--append-system-prompt") + 1]
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
    # `alarm_status` is the ROW's; `status` is the work order's. Both are published and
    # `alarms_across` spells them out precisely so a caller cannot confuse them.
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
    """A SEPARATE `agent_calls.kind`, because the sweep is the standing cost of watching
    and the review is the per-alarm cost of judging. Folded together, `jarvis cost`
    cannot answer "what does watching cost" — the first question anyone asks."""
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
    """A hallucinated id is ONE BAD FINDING, not a bad reply. The positive partner is in
    the same test: the reply is otherwise well formed and still writes its review row."""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")
    with monkeypatch.context() as m:
        m.setenv("FAKE_HEALTH_PROBE", "the-vibes-are-off")
        _sweep(started(), clock)

    assert len(_health_calls(fake_claude)) == 1
    assert _findings(store) == []
    (review,) = _reviews(store, subject_id=wo_id)
    assert review["outcome"] == "clear", "nothing survived the drop, so nothing was found"


# -- the three failure shapes ----------------------------------------------------------


@pytest.mark.parametrize("token", ["FORCE_HEALTH_GARBAGE",   # not JSON at all
                                   "FORCE_HEALTH_FAIL",      # ClaudeCliError
                                   "FORCE_HEALTH_NO_FINDINGS"])  # JSON, no `findings`
def test_a_failure_never_becomes_a_judgement(started, catalog_file, store, clock, token):
    """THE FAIL-SAFE POLARITY IS INVERTED HERE. `supervisor.review`'s fallback escalates,
    because output nobody can read must not become an ack; this one finds nothing,
    because output nobody can read must not become an attention item the user cannot
    trace to anything.

    Three shapes because `structured.request`'s `on_invalid` covers only two of them —
    `ClaudeCliError` propagates untouched by design (kn-9b18a8eb).
    """
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
    """How "the fingerprint was not recorded as reviewed" becomes OBSERVABLE rather than
    asserted about an implementation detail: a second tick at the unchanged fingerprint
    makes a second call and writes a second row."""
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


# -- the dedupe, which is the trap ------------------------------------------------------


def test_a_still_true_symptom_is_not_re_raised_until_the_unit_moves(
        started, catalog_file, fake_claude, store, clock):
    """THE FOUR ASSERTIONS THE SPEC SPECIFIES, and the obvious two-tick version of this
    test passes with a completely broken dedupe.

    A health finding has NO TURN, so `check_burning_turns`' `(kind, seq)` key does not
    exist here: "this order has made no progress for two days" is true on every sweep for
    as long as it is true. Keyed off `health_reviews.fingerprint`, never off alarm status.
    """
    _enable(catalog_file)
    wo_id = _wo(store, status="running")
    daemon = started()

    _sweep(daemon, clock)
    _sweep(daemon, clock)

    # (1) two sweeps GENUINELY RAN — without this the rest is true of a sweep that never
    # reached a model call.
    assert len(_health_calls(fake_claude)) == 2
    # (2) two looks at the same state, one finding.
    reviews = _reviews(store, subject_id=wo_id)
    assert len(reviews) == 2
    assert reviews[0]["fingerprint"] == reviews[1]["fingerprint"]
    assert len(_findings(store)) == 1

    # (3) STATUS MUST NOT ENTER THE PREDICATE. Keying on "no open alarm for this probe"
    # re-raises the moment the supervisor acks one — which is every time it works.
    (alarm,) = _findings(store)
    store.update_alarm(alarm["id"], status="acked")
    store.clear_attention(wo_id)
    _sweep(daemon, clock)
    assert len(_health_calls(fake_claude)) == 3
    assert len(_findings(store)) == 1, "an acked finding is not a licence to re-raise"
    assert store.get_work_order(wo_id)["needs_attention"] == 0

    # (4) and when the state genuinely moves, a still-true symptom is worth re-stating.
    store.add_event(wo_id, "note", {"n": 1})
    _sweep(daemon, clock)
    reviews = _reviews(store, subject_id=wo_id)
    assert reviews[-1]["fingerprint"] != reviews[0]["fingerprint"]
    assert reviews[-1]["trigger"] == "changed"
    assert len(_findings(store)) == 2


# -- what gets swept, and how many ------------------------------------------------------


def test_a_settled_feature_is_not_swept_and_an_open_one_with_a_carrier_is(
        started, catalog_file, fake_claude, store, clock):
    """One test with a total call count of exactly 1: two features, one candidate."""
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
    # The flag lands on the CARRIER, which is not itself in trouble, so it has to name
    # the record to open.
    carrier = store.get_work_order(alarm["wo_id"])
    assert carrier["needs_attention"] == 1
    assert carrier["attention_reason"].startswith(f"{open_fo}: ")


def test_a_feature_with_no_carrier_is_never_offered_to_the_sweep(
        started, catalog_file, fake_claude, store, clock):
    """A feature nobody has planned has no session at all and nothing to record a
    finding on — and it must cost no call to establish that."""
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
    """`health_max_units_per_tick` bounds the spend — a fleet with forty open units must
    not fire forty calls in one tick — and LONGEST-UNREVIEWED FIRST is what makes the cap
    a rotation. Without the ordering the same four are swept for ever."""
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


# -- the ledger's own housekeeping -------------------------------------------------------


def test_deleting_a_work_order_takes_its_health_reviews_and_leaves_the_others(store):
    """`health_reviews` has no foreign key — the subject may be either kind — so the
    cascade is by hand. And the counts dict stays at SIX keys:
    `test_wo_hide_delete.py::test_delete_work_order_cascades` asserts it with `==`.
    """
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
    """Writing "we looked and found nothing" into `wo_alarms` would fill
    `alarms_across`, `/alarms`' "On the record" half and `jarvis wo show`'s alarm line
    with noise, and filtering it out at every surface is the permanent union read §1
    argued against."""
    _enable(catalog_file)
    _wo(store, status="running", description="FORCE_HEALTH_CLEAR")

    _sweep(started(), clock)

    assert store.conn.execute("SELECT COUNT(*) FROM health_reviews").fetchone()[0] == 1
    assert store.alarms_across() == []


def test_the_sweeps_own_looking_stays_off_the_default_timeline(
        started, catalog_file, store, clock):
    """`health_reviewed` fires on every sweep including the clear ones — dozens a day on
    a long-running order. `health_finding` is a signal and stays one."""
    from jarvis import timeline

    _enable(catalog_file)
    wo_id = _wo(store, status="running")

    _sweep(started(), clock)

    assert timeline.event_level("health_reviewed") == "debug"
    assert timeline.event_level("health_finding") != "debug"
    kinds = [e["kind"] for e in timeline.build_timeline(store.get_work_order(wo_id), store.list_events(wo_id), store.list_messages(wo_id))]
    assert "health_reviewed" not in kinds
    assert "health_finding" in kinds


# -- the seams this section must not have moved -----------------------------------------


def test_the_sweeps_packet_carries_the_trigger_and_never_an_alarm_that_does_not_exist(
        started, catalog_file, fake_claude, store, clock):
    """`build_evidence(alarm=None)` omits the alarm section rather than synthesising one:
    a fabricated `kind` and `reason` in the judge's prompt is a fact the OS never
    recorded, and the judge cannot tell an invented one from a real one (Neo, q223)."""
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
    """The checklist is APPENDED, so a review's cached prefix is byte-identical to what
    it was before this feature existed — and the sweep's prompt EXTENDS that prefix
    instead of forking it."""
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
    """WHY `_health_calls` EXISTS. The sweep shares `SUPERVISOR_PERSONA`, so
    `test_supervisor.py::_supervisor_calls` — which keys on its first line — counts every
    sweep as a cost review. This asserts the overlap rather than assuming it: if the two
    ever stop sharing a persona, this fails and that helper can be simplified.
    """
    from jarvis.supervisor import SUPERVISOR_PERSONA

    _enable(catalog_file)
    _wo(store, status="running")

    _sweep(started(), clock)

    (call,) = _health_calls(fake_claude)
    system = call["argv"][call["argv"].index("--append-system-prompt") + 1]
    assert SUPERVISOR_PERSONA.splitlines()[0] in system
    assert CHECKLIST in system, "and the checklist is the only thing that separates them"


def test_the_sweep_gets_its_own_pool_and_its_own_guard(started):
    """NOT the supervisor's. The supervisor answers a turn that is burning money right
    now; a fleet sweep queued in front of it would delay the thing the whole mechanism
    was built for."""
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
    """`supervisor.py` RAISES; the existing review path judges it unchanged. No message
    reaches the session, no status moves, no remedy is applied."""
    _enable(catalog_file)
    wo_id = _wo(store, status="running")

    _sweep(started(), clock)

    assert len(_findings(store)) == 1
    assert store.get_work_order(wo_id)["status"] == "running"
    assert store.queued_messages(wo_id) == []
    assert store.list_messages(wo_id) == []


def test_health_reviews_outcomes_are_a_declared_vocabulary(store):
    from jarvis.project_store import HEALTH_OUTCOMES

    assert set(HEALTH_OUTCOMES) == {"clear", "findings", "failed"}
    with pytest.raises(AssertionError):
        store.record_health_review("work_order", "wo-1", fingerprint="fp",
                                   trigger="first-look", outcome="fine-i-guess")


def test_count_events_is_unbounded_where_list_events_is_capped(store):
    """`list_events` caps at 200, which is fine for a page and wrong for a fingerprint:
    a busy order's count would freeze at the cap and read as motionless for ever."""
    wo_id = _wo(store)
    for n in range(205):
        store.add_event(wo_id, "note", {"n": n})
    assert len(store.list_events(wo_id)) == 200
    assert store.count_events(wo_id) == 206, "and the `created` event is one of them"


def test_a_records_own_clock_is_what_ages_it(store, monkeypatch):
    """`due` reads `db.now`, so a row created in the past is genuinely in the past —
    patched while CREATING it, inside a `monkeypatch.context`, and never with a bare
    `undo()` (which reverts the `jarvis_home` and `catalog_file` fixtures and renders
    every surface as an empty OS with no error anywhere)."""
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
