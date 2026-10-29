"""The steward: one gate over what the OS may spend on itself (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md)"""

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
    """The 10-generation chain of 2026-10-20: the sweep investigating its own fix orders"""
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


def test_a_neo_cleanup_is_one_sighting_enough_a_panel_medium_is_not():
    """Neo raises a cleanup once, in one verdict: its own threshold, not `medium_min_refs`"""
    assert CFG.min_refs_by_kind == {"neo_rider": 1}
    assert outcome(cand(origin_kind="neo_rider", severity="medium")) == ("dispatch",
                                                                         "medium")
    assert outcome(cand(origin_kind="neo_rider", severity="medium", refs=0)) == (
        "queue", "recurrence")
    assert outcome(cand(origin_kind="panel", severity="medium")) == ("queue", "recurrence")


def test_a_per_kind_threshold_keeps_every_rule_before_recurrence():
    rider = cand(origin_kind="neo_rider", severity="medium")
    assert outcome(rider, steward.Ledger(evolution_usd=CFG.weekly_usd)) == ("queue",
                                                                            "ledger")
    assert outcome(rider, steward.Ledger(fleet_usd=CFG.yield_above_usd + 1)) == (
        "queue", "yield")
    assert outcome(rider, throttled=True) == ("queue", "low-precision")
    assert outcome(rider, generation=2) == ("queue", "max-depth")
    assert outcome(rider, parent_kind="neo_rider") == ("queue", "self-reference")
    assert outcome(cand(origin_kind="neo_rider", severity="minor")) == ("drop",
                                                                        "low-severity")


def test_a_per_kind_threshold_overrides_major_and_medium_alike():
    cfg = EvolutionConfig(min_refs_by_kind={"bug_agent": 4})
    assert outcome(cand(severity="major", refs=3), cfg=cfg) == ("queue", "recurrence")
    assert outcome(cand(severity="medium", refs=4), cfg=cfg) == ("dispatch", "medium")
    assert outcome(cand(origin_kind="panel", severity="major", refs=1), cfg=cfg) == (
        "dispatch", "major")


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


def test_a_per_kind_threshold_merges_over_the_default_and_can_raise_it_back():
    def parse(raw, projects=()):
        return parse_catalog({"os": {"evolution": raw}, "projects": list(projects)})
    assert parse({"min_refs_by_kind": {"panel": 2}}).os.evolution.min_refs_by_kind == {
        "neo_rider": 1, "panel": 2}
    assert parse({"min_refs_by_kind": {"neo_rider": 3}}).os.evolution.min_refs_by_kind == {
        "neo_rider": 3}


@pytest.mark.parametrize("bad", [{"mode": "loud"}, {"weekly_usd": -1},
                                 {"min_precision": 2},
                                 {"min_refs_by_kind": []},
                                 {"min_refs_by_kind": {"nope": 1}},
                                 {"min_refs_by_kind": {"neo_rider": -1}}])
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


def test_an_ask_reaches_the_inbox_with_a_link_and_the_command_that_acts(fleet):
    make, store = fleet
    cat = make(mode="enforce", critical_weekly_usd=0)
    d = steward.gate(cand(origin_kind="morpheus", severity="critical", parent_wo_id="wo-p1",
                          payload={"morpheus_item": "mv-1"}), catalog=cat, store=store)
    assert d.outcome == "ask"
    central = CentralStore()
    try:
        [item] = central.new_inbox()
        [held] = central.steward_asks(0)
    finally:
        central.close()
    assert (item["level"], item["wo_id"], held["id"]) == ("critical", "wo-p1", d.decision_id)
    assert "jarvis morpheus confirm mv-1 --fix" in item["body"]
    assert f"/steward#decision-{d.decision_id}" in item["body"]
    assert "does not re-run this one" in item["body"]


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


def test_the_ledger_counts_a_stamped_improvement_order_and_its_analyst(fleet):
    """Decision #59 of 2026-10-22: an allowed worker io never drew the ledger down"""
    make, store = fleet
    cat = make(mode="enforce", weekly_usd=100)
    d = steward.gate(cand(origin_kind="worker_io"), catalog=cat, store=store)
    io = store.create_feature_order("slow first turns", kind="improvement",
                                    metadata=steward.stamped({}, d), budget_usd=20.0)
    analyst = store.create_work_order("analyse it", parent_id=io["id"], kind="analyst")
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"])
    _spend(store, analyst["id"], 5.0)
    book = steward.ledger(cat)
    # 5 spent by the analyst + 15 still reserved on the open io
    assert book.evolution_usd == pytest.approx(20.0)


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
    """A red `main` is re-asked every tick; the table is read by a person"""
    make, store = fleet
    first = steward.gate(cand(severity="minor"), catalog=make(), store=store)
    again = steward.gate(cand(severity="minor"), catalog=make(), store=store)
    assert first.decision_id == again.decision_id
    central = CentralStore()
    try:
        assert len(central.steward_decisions()) == 1
    finally:
        central.close()


# ---------------------------------------------------------------- dedupe (§3.2)


#: Decisions #23, #24 and #25 of 2026-10-22, verbatim: one mechanism, three wordings
PANEL_DUPES = [
    "uilog._last_success: a recovery that scrolls out of the 64KiB access tail "
    "re-raises the healed error",
    "uilog._last_success: a recovery that scrolls out of the 64KiB access tail "
    "re-lights the error",
    "uilog.recent_errors: a healed error comes back once its success scrolls out of "
    "the 64KiB access tail",
]


#: One defect in three wordings, least to most severe: each is a restatement of the first
EDGE = "a healed error comes back once its success scrolls out of the access tail"
LOSS = "a healed error comes back and the access tail loses the success"
EVERY_RUN = "a healed error comes back on every run: the access tail loses each success"


def test_the_bucket_is_the_origin_unit_and_the_file(fleet):
    assert steward.dedupe_key(cand(parent_wo_id="wo-1", file="SRC/Jarvis/UiLog.py")) == (
        "proj_a|bug_agent|wo-1|src/jarvis/uilog.py")
    # Nothing to collapse by: a candidate that names no code, or none that caused it
    assert steward.dedupe_key(cand(parent_wo_id="wo-1")) == ""
    assert steward.dedupe_key(cand(file="src/jarvis/uilog.py")) == ""


def test_one_defect_worded_by_three_seats_is_one_queued_decision(fleet):
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("the order the panel reviewed")
    out = [steward.gate(cand(origin_kind="panel", severity="medium", subject=subject,
                             parent_wo_id=parent["id"], file="src/jarvis/uilog.py"),
                        catalog=cat, store=store)
           for subject in PANEL_DUPES]
    assert [(d.outcome, d.rule) for d in out] == [
        ("queue", "recurrence"), ("drop", "duplicate"), ("drop", "duplicate")]
    assert {d.duplicate_of for d in out[1:]} == {out[0].decision_id}
    assert not any(d.allowed for d in out)
    central = CentralStore()
    try:
        rows = central.steward_decisions()
    finally:
        central.close()
    # RECORDED against the one decision they restate, not dropped silently
    assert len(rows) == 3
    assert [r["duplicate_of"] for r in rows] == [out[0].decision_id,
                                                 out[0].decision_id, 0]


def test_another_file_under_the_same_order_is_its_own_decision(fleet):
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("one order, two files")
    first, second = (steward.gate(
        cand(origin_kind="panel", severity="medium", subject=f"defect in {path}",
             parent_wo_id=parent["id"], file=path), catalog=cat, store=store)
        for path in ("src/jarvis/ops.py", "src/jarvis/uilog.py"))
    assert (first.rule, second.rule) == ("recurrence", "recurrence")
    assert second.duplicate_of == 0


def test_a_dropped_wording_does_not_swallow_a_severer_one(fleet):
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("minor first, critical after")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    low = steward.gate(cand(severity="minor", subject="a nit", **where),
                       catalog=cat, store=store)
    high = steward.gate(cand(severity="critical", subject=LOSS, **where),
                        catalog=cat, store=store)
    assert (low.outcome, low.rule) == ("drop", "low-severity")
    assert (high.outcome, high.rule, high.duplicate_of) == ("dispatch", "urgent", 0)


def test_a_severer_wording_re_decides_the_one_decision_at_its_severity(fleet):
    """Decisions #37-#39 of 2026-10-22: the critical wording was dropped as a duplicate"""
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("medium first, critical after")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    first = steward.gate(cand(severity="medium", subject=EDGE, **where),
                         catalog=cat, store=store)
    second = steward.gate(cand(severity="critical", subject=LOSS, **where),
                          catalog=cat, store=store)
    assert (first.outcome, first.rule, first.allowed) == ("queue", "recurrence", False)
    assert (second.outcome, second.rule, second.allowed) == ("dispatch", "urgent", True)
    # ONE decision per defect, now at the max severity
    assert (second.decision_id, second.duplicate_of) == (first.decision_id, 0)
    central = CentralStore()
    try:
        [row] = central.steward_decisions()
    finally:
        central.close()
    assert (row["severity"], row["outcome"], row["rule"]) == ("critical", "dispatch",
                                                              "urgent")
    assert "escalated from `medium`" in row["reason"] and row["applied"] == 1


def test_a_lower_wording_after_a_severer_one_stays_a_duplicate(fleet):
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("critical first, medium after")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    first = steward.gate(cand(severity="critical", subject=LOSS, **where),
                         catalog=cat, store=store)
    second = steward.gate(cand(severity="medium", subject=EDGE, **where),
                          catalog=cat, store=store)
    assert (first.outcome, first.rule) == ("dispatch", "urgent")
    assert (second.outcome, second.rule, second.duplicate_of) == (
        "drop", "duplicate", first.decision_id)


def test_a_severer_wording_of_a_dispatched_defect_tells_that_order(fleet):
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("the order the panel reviewed")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    first = steward.gate(cand(severity="critical", subject=LOSS, **where),
                         catalog=cat, store=store)
    fix = store.create_work_order("fix the data loss")
    steward.linked(first, fix["id"])
    second = steward.gate(cand(severity="blocker", subject=EVERY_RUN,
                               **where), catalog=cat, store=store)
    # No second order for a defect already being worked, and the severity is not lost
    assert (second.outcome, second.rule, second.allowed) == (
        "drop", "duplicate-of-dispatched", False)
    assert second.duplicate_of == first.decision_id
    central = CentralStore()
    try:
        [raised] = [r for r in central.steward_decisions()
                    if r["id"] == first.decision_id]
    finally:
        central.close()
    assert raised["severity"] == "blocker"
    [event] = [e for e in store.conn.execute(
        "SELECT * FROM wo_events WHERE wo_id=? AND kind='steward_severity_raised'",
        (fix["id"],)).fetchall()]
    assert json.loads(event["payload"])["severity"] == "blocker"


def _row(decision_id):
    central = CentralStore()
    try:
        return central.steward_decision(decision_id)
    finally:
        central.close()


#: Decisions #54 and #55 of 2026-10-22: two DIFFERENT defects one panel found in one file
UNLINKED = ("steward.gate: the severity_raised event is lost when the prior decision is "
            "applied but not yet linked")
OVERWRITE = ("steward.gate: escalation overwrites a different defect's queued decision "
             "in the same file")


def test_same_defect_tells_rewordings_from_a_different_defect():
    reworded = ("_worsened silently skips the steward_severity_raised event while the "
                "decision is applied but unlinked")
    file = "src/jarvis/steward.py"
    assert steward.same_defect(UNLINKED, reworded, file=file)
    assert not steward.same_defect(UNLINKED, OVERWRITE, file=file)
    # Decisions #23-#25's three seats, naming different symbols, are still one defect
    assert steward.same_defect(PANEL_DUPES[0], PANEL_DUPES[2], file="src/jarvis/uilog.py")
    assert steward.same_defect(EDGE, EVERY_RUN, file="src/jarvis/uilog.py")


def test_a_different_defect_in_the_same_file_is_its_own_decision(fleet):
    """Decision #55 of 2026-10-22: an escalation rewrote the wrong defect's decision"""
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("the order the panel reviewed")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/steward.py")
    first = steward.gate(cand(severity="medium", subject=UNLINKED, **where),
                         catalog=cat, store=store)
    second = steward.gate(cand(severity="medium", subject=OVERWRITE, **where),
                          catalog=cat, store=store)
    assert second.duplicate_of == 0 and second.decision_id != first.decision_id
    # A severer wording of the SECOND defect re-decides the second, never the first
    worse = steward.gate(cand(severity="critical", subject=OVERWRITE + ", every time",
                              **where), catalog=cat, store=store)
    assert worse.decision_id == second.decision_id and worse.outcome == "dispatch"
    kept = _row(first.decision_id)
    assert (kept["severity"], kept["outcome"], kept["subject"]) == ("medium", "queue",
                                                                    UNLINKED)


def test_an_escalated_decision_carries_the_severer_wording(fleet):
    """Decision #57 of 2026-10-22: the escalated row kept the first wording's subject"""
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("medium first, critical after")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    first = steward.gate(cand(severity="medium", subject=EDGE, **where),
                         catalog=cat, store=store)
    steward.gate(cand(severity="critical", subject=LOSS, **where), catalog=cat,
                 store=store)
    row = _row(first.decision_id)
    assert (row["subject"], row["severity"]) == (LOSS, "critical")
    candidate = json.loads(row["candidate_json"])
    assert candidate["subject"] == LOSS and candidate["also_worded"] == [EDGE]


def test_a_severer_wording_reaches_an_applied_order_once_it_is_linked(fleet):
    """Decisions #54/#56/#58 of 2026-10-22: dispatched, not yet linked, and the raise was lost"""
    make, store = fleet
    cat = make(mode="enforce")
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(store.project_path))
    finally:
        central.close()
    parent = store.create_work_order("the order the panel reviewed")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    first = steward.gate(cand(severity="critical", subject=LOSS, **where),
                         catalog=cat, store=store)
    assert first.allowed
    # The caller has not created (or linked) its order yet when the severer wording lands
    second = steward.gate(cand(severity="blocker", subject=EVERY_RUN, **where),
                          catalog=cat, store=store)
    assert second.rule == "duplicate-of-dispatched"
    fix = store.create_work_order("fix the data loss")
    steward.linked(first, fix["id"])
    [event] = store.conn.execute(
        "SELECT * FROM wo_events WHERE wo_id=? AND kind='steward_severity_raised'",
        (fix["id"],)).fetchall()
    assert json.loads(event["payload"])["severity"] == "blocker"
    # Delivered once: a second link does not say it again
    steward.linked(first, fix["id"])
    assert len(store.conn.execute(
        "SELECT * FROM wo_events WHERE wo_id=? AND kind='steward_severity_raised'",
        (fix["id"],)).fetchall()) == 1


def test_a_candidate_that_names_no_code_is_never_deduped(fleet):
    """A red `main` keeps its own rule — the hourly repeat check, not the dedupe bucket"""
    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("no file named")
    first = steward.gate(cand(origin_kind="red_main", severity="critical",
                              parent_wo_id=parent["id"]), catalog=cat, store=store)
    assert first.duplicate_of == 0 and first.allowed is True


# ---------------------------------------------------------------- the queue (#1181)


@pytest.fixture()
def queue(fleet):
    """`fleet`, registered (the drain finds stores by the registry), plus a recording dispatcher"""
    make, store = fleet
    central = CentralStore()
    central.upsert_project("proj_a", str(store.project_path))
    calls = []

    def dispatch(row, c, decision):
        calls.append((row["id"], c.subject, decision.budget_usd))
        return ""
    yield make, store, central, calls, {"worker_io": dispatch, "panel": dispatch}
    central.close()


def _queued(store, cat, subject, severity="major", **kw):
    parent = store.create_work_order(f"the order behind {subject}")
    return steward.gate(cand(origin_kind="worker_io", severity=severity, subject=subject,
                             parent_wo_id=parent["id"], payload={"title": subject}, **kw),
                        catalog=cat, store=store)


def test_a_queue_without_a_way_to_dispatch_it_later_does_not_wait(queue):
    make, store, central, _calls, _d = queue
    d = steward.gate(cand(severity="major", refs=0), catalog=make(mode="enforce"),
                     store=store)
    assert d.outcome == "queue"
    assert central.steward_decision(d.decision_id)["queue_state"] == ""


def test_the_drain_offers_the_severest_then_the_oldest_up_to_its_limit(queue):
    make, store, central, calls, dispatchers = queue
    held = make(mode="enforce", weekly_usd=0)
    medium = _queued(store, held, "a medium", severity="medium", refs=5)
    old = _queued(store, held, "an old major")
    new = _queued(store, held, "a new major")
    assert {medium.outcome, old.outcome, new.outcome} == {"queue"}
    roomy = make(mode="enforce", weekly_usd=100, order_budget_usd=10)
    out = steward.drain(roomy, central, dispatchers=dispatchers, limit=2)
    assert out == {"waiting": 1, "dispatched": 2, "expired": 0, "failed": 0}
    assert [c[1] for c in calls] == ["an old major", "a new major"]
    assert calls[0][2] == 10.0
    row = central.steward_decision(old.decision_id)
    assert (row["queue_state"], row["outcome"], row["applied"]) == ("reoffered",
                                                                    "dispatch", 1)
    assert central.steward_decision(medium.decision_id)["queue_state"] == "waiting"


def test_a_failed_re_offer_stays_queued_for_the_next_pass(queue):
    make, store, central, _calls, _d = queue
    d = _queued(store, make(mode="enforce", weekly_usd=0), "a major")

    def boom(row, c, decision):
        raise RuntimeError("tracker down")
    out = steward.drain(make(mode="enforce"), central, dispatchers={"worker_io": boom})
    assert out["failed"] == 1
    row = central.steward_decision(d.decision_id)
    assert row["queue_state"] == "waiting" and "tracker down" in row["queue_note"]


def test_shadow_and_self_dispatch_off_re_offer_nothing(queue):
    make, store, central, calls, dispatchers = queue
    _queued(store, make(mode="enforce", weekly_usd=0), "a major")
    assert steward.drain(make(), central, dispatchers=dispatchers)["waiting"] == 1
    off = make(mode="enforce")
    off.os.autonomy.self_dispatch = False
    assert steward.drain(off, central, dispatchers=dispatchers)["waiting"] == 1
    assert calls == []


def test_a_queued_defect_dispatched_by_another_decision_expires(queue):
    make, store, central, calls, dispatchers = queue
    cat = make(mode="enforce")
    parent = store.create_work_order("the order the panel reviewed")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    queued = steward.gate(cand(severity="medium", subject=EDGE, payload={"x": 1}, **where),
                          catalog=cat, store=store)
    assert central.steward_decision(queued.decision_id)["queue_state"] == "waiting"
    # Another head for the same defect (as an older bucket could hold) is already dispatched
    other = central.record_steward_decision({
        "project": "proj_a", "origin_kind": "panel", "subject": LOSS,
        "severity": "critical", "outcome": "dispatch", "rule": "urgent", "mode": "enforce",
        "applied": 1, "dedupe_key": steward.dedupe_key(cand(**where))})
    assert steward.drain(cat, central, dispatchers=dispatchers)["expired"] == 1
    assert f"#{other}" in central.steward_decision(queued.decision_id)["queue_note"]
    assert calls == []


def test_decisions_and_the_digest_say_how_many_are_waiting(queue, capsys):
    from jarvis import cli

    make, store, central, _calls, _d = queue
    cat = make(mode="enforce", weekly_usd=0)
    _queued(store, cat, "a major")
    assert "queued: 1 waiting for room" in steward.render_digest(steward.digest(cat, central))
    assert cli.main(["steward", "decisions"]) == 0
    out = capsys.readouterr().out
    assert "queued: 1 waiting for room" in out and "queue waiting" in out


def test_the_daemon_drains_nothing_while_the_fleet_is_paused(monkeypatch):
    from jarvis import daemon, fleet as fleet_mod

    seen = []
    monkeypatch.setattr(steward, "drain", lambda *a, **k: seen.append(1) or {})
    monkeypatch.setattr(fleet_mod, "load_pause", lambda central: object())
    stub = type("D", (), {"central": None, "catalog": None})()
    daemon.Daemon.steward_drain(stub)
    assert seen == []


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
    """`decisions` reads only the record, so it needs no catalog"""
    from jarvis import cli

    make, store = fleet
    steward.gate(cand(severity="cosmetic", subject="rename it"), catalog=make(),
                 store=store)
    assert cli.main(["steward", "decisions"]) == 0
    out = capsys.readouterr().out
    assert "[drop/shadow]" in out and "low-severity" in out and "rename it" in out


def test_the_cli_says_which_decision_was_escalated_and_which_was_held(fleet, capsys):
    from jarvis import cli

    make, store = fleet
    cat = make(mode="enforce")
    parent = store.create_work_order("medium first, critical after")
    where = dict(origin_kind="panel", parent_wo_id=parent["id"],
                 file="src/jarvis/uilog.py")
    steward.gate(cand(severity="medium", subject=EDGE, **where),
                 catalog=cat, store=store)
    first = steward.gate(cand(severity="critical", subject=LOSS, **where),
                         catalog=cat, store=store)
    fix = store.create_work_order("fix the data loss")
    steward.linked(first, fix["id"])
    steward.gate(cand(severity="blocker", subject=EVERY_RUN, **where),
                 catalog=cat, store=store)
    assert cli.main(["steward", "decisions"]) == 0
    out = capsys.readouterr().out
    assert "escalated from `medium`" in out
    assert "duplicate-of-dispatched" in out and "to `blocker`" in out
    assert cli.main(["steward", "why", fix["id"]]) == 0
    assert "blocker" in capsys.readouterr().out
