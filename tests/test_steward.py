"""The steward: one gate over what the OS may spend on itself.

docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md.
The rules are pinned one per test against `decide` (pure); the ledger, the precision
throttle and shadow-versus-enforce are pinned through `gate` against real stores.
"""

from __future__ import annotations

import json

import pytest

from jarvis import db, steward
from jarvis.catalog import CatalogError, EvolutionConfig, parse_catalog
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore


def cand(**kw) -> steward.Candidate:
    base = dict(origin_kind="bug_agent", severity="major", project="proj_a",
                subject="a defect", evidence=("alarm al-1",), refs=1)
    return steward.Candidate(**{**base, **kw})


CFG = EvolutionConfig()
ROOM = steward.Ledger()


def outcome(c, ledger=ROOM, cfg=CFG, generation=1, **kw):
    return steward.decide(c, cfg, ledger, generation=generation, **kw)[:2]


# ---------------------------------------------------------------- §3.0 the scale


@pytest.mark.parametrize("given,want", [("low", "minor"), ("high", "major"),
                                        ("critical", "critical"), ("nonsense", "medium"),
                                        ("COSMETIC", "cosmetic")])
def test_bug_priorities_and_unknown_words_land_on_the_one_scale(given, want):
    assert steward.normalise(given) == want


def test_a_major_without_evidence_is_lowered_one_level():
    assert steward.apply_evidence("major", ()) == "medium"
    assert steward.apply_evidence("blocker", ("  ",)) == "critical"
    assert steward.apply_evidence("major", ("tests/test_x.py::test_y fails",)) == "major"
    assert steward.apply_evidence("medium", ()) == "medium"


# ---------------------------------------------------------------- §3.2 decision 1


def test_minor_and_cosmetic_are_dropped():
    assert outcome(cand(severity="minor")) == ("drop", "low-severity")
    assert outcome(cand(severity="cosmetic")) == ("drop", "low-severity")


def test_past_max_depth_is_queued_never_dispatched():
    assert outcome(cand(severity="critical"), generation=2) == ("queue", "max-depth")


def test_a_detector_chasing_its_own_work_is_queued():
    """The 10-generation chain of 2026-10-20: the sweep investigating its own fix orders."""
    c = cand(origin_kind="investigation", severity="critical")
    assert outcome(c, parent_kind="investigation") == ("queue", "self-reference")


def test_critical_with_evidence_dispatches_now():
    assert outcome(cand(severity="critical")) == ("dispatch", "urgent")


def test_critical_without_evidence_is_only_a_major():
    assert outcome(cand(severity="critical", evidence=()))[0] == "dispatch"
    assert steward.decide(cand(severity="critical", evidence=()), CFG, ROOM,
                          generation=1)[3] == "major"


def test_critical_past_its_ceiling_asks_the_user():
    spent = steward.Ledger(evolution_usd=60, critical_usd=60)
    assert outcome(cand(severity="blocker"), spent) == ("ask", "critical-ceiling")


def test_a_major_waits_while_feature_work_is_spending():
    busy = steward.Ledger(fleet_usd=CFG.yield_above_usd + 1)
    assert outcome(cand(), busy) == ("queue", "yield")


def test_a_spent_ledger_queues_a_major():
    assert outcome(cand(), steward.Ledger(evolution_usd=CFG.weekly_usd)) == ("queue",
                                                                              "ledger")


def test_a_medium_needs_recurrence_a_major_needs_less():
    assert outcome(cand(severity="medium", refs=2)) == ("queue", "recurrence")
    assert outcome(cand(severity="medium", refs=3)) == ("dispatch", "medium")
    assert outcome(cand(severity="major", refs=1)) == ("dispatch", "major")
    assert outcome(cand(severity="major", refs=0)) == ("queue", "recurrence")


def test_a_throttled_detector_queues_but_urgent_still_goes():
    assert outcome(cand(), throttled=True) == ("queue", "low-precision")
    assert outcome(cand(severity="critical"), throttled=True) == ("dispatch", "urgent")


def test_a_release_ships_whatever_the_ledger_says():
    spent = steward.Ledger(evolution_usd=10_000, fleet_usd=10_000)
    assert outcome(cand(origin_kind="release"), spent) == ("dispatch", "release")


# ---------------------------------------------------------------- config


def test_the_config_ships_in_shadow_with_the_spec_defaults():
    cfg = parse_catalog({"projects": []}).os.evolution
    assert (cfg.mode, cfg.weekly_usd, cfg.max_depth, cfg.medium_min_refs) == (
        "shadow", 150.0, 1, 3)


@pytest.mark.parametrize("bad", [{"mode": "loud"}, {"weekly_usd": -1},
                                 {"min_precision": 2}])
def test_a_bad_evolution_setting_is_refused(bad):
    with pytest.raises(CatalogError):
        parse_catalog({"os": {"evolution": bad}, "projects": []})


# ---------------------------------------------------------------- the gate


@pytest.fixture()
def fleet(project):
    def make(**evolution):
        return parse_catalog({
            "os": {"evolution": evolution, "autonomy": {"self_dispatch": True}},
            "projects": [{"name": "proj_a", "path": str(project)}]})
    store = ProjectStore(project)
    yield make, store
    store.close()


def _spend(store, wo_id, usd):
    store.conn.execute(
        "INSERT INTO wo_turns (wo_id, seq, kind, prompt, state, started_at, cost_usd) "
        "VALUES (?, 1, 'dispatch', 'p', 'done', ?, ?)", (wo_id, db.now(), usd))
    store.conn.commit()


def test_shadow_records_the_decision_and_changes_nothing(fleet):
    make, store = fleet
    d = steward.gate(cand(severity="minor"), catalog=make(), store=store)
    assert (d.outcome, d.mode, d.allowed) == ("drop", "shadow", True)
    central = CentralStore()
    try:
        [row] = central.steward_decisions()
    finally:
        central.close()
    assert (row["outcome"], row["rule"], row["applied"]) == ("drop", "low-severity", 0)


def test_enforce_applies_the_decision_and_sets_a_budget(fleet):
    make, store = fleet
    cat = make(mode="enforce", weekly_usd=30, order_budget_usd=25)
    assert steward.gate(cand(severity="minor"), catalog=cat, store=store).allowed is False
    d = steward.gate(cand(), catalog=cat, store=store)
    assert (d.allowed, d.budget_usd) == (True, 25.0)
    assert d.stamp["generation"] == 1 and d.stamp["origin_kind"] == "bug_agent"


def test_enforce_never_dispatches_with_self_dispatch_off(fleet):
    make, store = fleet
    d = steward.gate(cand(severity="critical"), catalog=make(mode="enforce"),
                     store=store, self_dispatch=False)
    assert (d.outcome, d.allowed) == ("dispatch", False)


def test_the_ledger_counts_stamped_spend_and_what_is_still_reserved(fleet):
    make, store = fleet
    cat = make(mode="enforce", weekly_usd=100)
    d = steward.gate(cand(), catalog=cat, store=store)
    wo = store.create_work_order("fix", metadata=steward.stamped({}, d),
                                 budget_usd=d.budget_usd)
    _spend(store, wo["id"], 10.0)
    user = store.create_work_order("the user's own")
    _spend(store, user["id"], 40.0)
    book = steward.ledger(cat)
    # 10 spent + 15 still reserved on the open order; the user's 40 is fleet spend only
    assert book.evolution_usd == pytest.approx(25.0)
    assert book.fleet_usd == pytest.approx(50.0)


def test_a_child_of_steward_work_is_one_generation_deeper(fleet):
    make, store = fleet
    cat = make(mode="enforce")
    d = steward.gate(cand(), catalog=cat, store=store)
    parent = store.create_work_order("fix", metadata=steward.stamped({}, d))
    child = steward.gate(cand(origin_kind="remedy", parent_wo_id=parent["id"]),
                         catalog=cat, store=store)
    assert (child.generation, child.outcome, child.rule) == (2, "queue", "max-depth")


def test_a_detector_nobody_picks_up_is_throttled(fleet):
    make, store = fleet
    cat = make(mode="enforce", weekly_usd=10_000, order_budget_usd=1)
    ids = []
    for _ in range(steward.PRECISION_MIN_SAMPLES):
        d = steward.gate(cand(), catalog=cat, store=store)
        ids.append(store.create_work_order("fix", metadata=steward.stamped({}, d))["id"])
    assert steward.gate(cand(), catalog=cat, store=store).rule == "low-precision"
    store.add_event(ids[0], "reviewed", {})
    assert steward.gate(cand(), catalog=cat, store=store).rule == "major"


def test_a_stamp_survives_the_metadata_round_trip(fleet):
    _make, store = fleet
    wo = store.create_work_order("x", metadata={steward.STAMP_KEY: {"generation": 3}})
    assert steward.stamp_of(store.get_work_order(wo["id"])) == {"generation": 3}
    assert json.loads(store.get_work_order(wo["id"])["metadata"])


def test_an_identical_decision_within_the_hour_is_one_row(fleet):
    """A red `main` is re-asked every tick; the table is read by a person."""
    make, store = fleet
    first = steward.gate(cand(severity="minor"), catalog=make(), store=store)
    again = steward.gate(cand(severity="minor"), catalog=make(), store=store)
    assert first.decision_id == again.decision_id
    central = CentralStore()
    try:
        assert len(central.steward_decisions()) == 1
    finally:
        central.close()


# ---------------------------------------------------------------- the weekly digest


def test_the_digest_goes_out_once_a_week_and_says_what_was_held(fleet):
    make, store = fleet
    cat = make()
    steward.gate(cand(severity="minor", subject="a typo"), catalog=cat, store=store)
    central = CentralStore()
    try:
        assert steward.digest_due(central)
        assert steward.send_digest(cat, central)
        assert not steward.digest_due(central)
        [item] = [i for i in central.conn.execute("SELECT * FROM inbox").fetchall()
                  if i["title"].startswith("steward")]
    finally:
        central.close()
    assert "bug_agent" in item["body"] and "drop 1" in item["body"]


def test_a_week_with_no_decisions_sends_nothing(fleet):
    make, _store = fleet
    central = CentralStore()
    try:
        assert steward.send_digest(make(), central) == 0
    finally:
        central.close()


def test_the_cli_shows_each_decision_with_its_rule(fleet, capsys):
    """`decisions` reads only the record, so it needs no catalog."""
    from jarvis import cli

    make, store = fleet
    steward.gate(cand(severity="cosmetic", subject="rename it"), catalog=make(),
                 store=store)
    assert cli.main(["steward", "decisions"]) == 0
    out = capsys.readouterr().out
    assert "[drop/shadow]" in out and "low-severity" in out and "rename it" in out
