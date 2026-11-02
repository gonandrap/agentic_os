"""Neo decides improvement-order findings for the user (docs/superpowers/specs/2026-09-23-improvement-orders.md §5.5)"""

from __future__ import annotations

import json

import pytest

from jarvis import db, findings, invariants, ops, steward
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import a_finding, a_report

#: A symptom carrying the fake Neo's switch, long enough for the validator's floor
def symptom(token: str) -> str:
    return (f"Three work orders in a row opened with the same eight file reads before "
            f"touching the dispatch path. {token}")


@pytest.fixture()
def boot(jarvis_home, fake_claude, catalog_file, project):
    """Start the OS with `os` overrides; returns the daemon"""
    def make(**os_overrides):
        data = json.loads(catalog_file.read_text())
        for key, value in os_overrides.items():
            data["os"][key] = {**(data["os"].get(key) or {}), **value}
        catalog_file.write_text(json.dumps(data))
        ops.start_os(str(catalog_file), foreground=True)
        return Daemon(load_catalog(catalog_file))
    return make


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def central(jarvis_home):
    c = CentralStore()
    yield c
    c.close()


def submitted(store, io, report):
    """The analyst reports: `io` in `plan_review`, every finding asked of Neo"""
    analyst = store.create_work_order("analyse it", description="the observation",
                                      kind="planner", status="running")
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"], status="planning")
    out = ops.submit_findings(io["id"], report)
    return analyst, out


def one_finding(token: str, **overrides):
    return a_report(findings=[a_finding("first-turn-reads", symptom=symptom(token),
                                        **overrides)])


def finding(store, io_id, key="first-turn-reads"):
    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    return next(f for f in report["findings"] if f["key"] == key)


def neo_questions(kind="findings"):
    neo = NeoStore()
    try:
        return [q for q in neo.list_questions() if q["kind"] == kind]
    finally:
        neo.close()


def filed_orders(store):
    return [wo for wo in store.list_work_orders() if wo["kind"] == "worker"]


# ---------------------------------------------------------------- asking


def test_the_report_goes_to_neo_one_atomic_question_per_finding(boot, store,
                                                               improvement_order):
    boot()
    analyst, out = submitted(store, improvement_order, a_report())

    qs = neo_questions()
    assert len(qs) == 2 and {q["wo_id"] for q in qs} == {analyst["id"]}
    # Atomic: each question carries ONE finding, never the whole report
    assert sum("first-turn-reads" in q["question"] for q in qs) == 1
    assert all("Would the user accept it?" in q["question"] for q in qs)
    assert all(len(q["question"]) + len(q["context"]) < 12000 for q in qs)
    assert "Neo's decision" in out["note"]
    # Nothing is owed to the user while Neo decides: no attention item
    row = store.get_feature_order(improvement_order["id"])
    assert not row["needs_attention"]
    report = db.from_json(row["plan"], None)
    assert "2 with Neo" in findings.review_headline(row, report)
    assert "awaiting you" not in findings.review_headline(row, report)
    assert finding(store, improvement_order["id"])["neo_question_id"] in {q["id"] for q in qs}


def test_switched_off_the_report_waits_for_the_user_as_before(boot, store,
                                                              improvement_order):
    boot(neo={"review_findings": False})
    submitted(store, improvement_order, a_report())

    assert neo_questions() == []
    assert store.get_feature_order(improvement_order["id"])["needs_attention"]


# ---------------------------------------------------------------- Neo's ruling


def test_neo_rejecting_a_finding_decides_it_without_teaching_neo(boot, store,
                                                                 improvement_order):
    daemon = boot()
    analyst, _ = submitted(store, improvement_order, one_finding("FORCE_FINDING_REJECT"))
    neo = NeoStore()
    try:
        learnings_before = len(neo.learnings("proj_a", limit=500))
    finally:
        neo.close()

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert (f["status"], f["decided_by"]) == ("rejected", "neo")
    assert f["neo_verdict"]["verdict"] == "reject"
    assert store.get_feature_order(improvement_order["id"])["status"] == "completed"
    neo = NeoStore()
    try:
        # Neo learning from its own ruling would be a loop: only the user's refusal teaches it
        assert len(neo.learnings("proj_a", limit=500)) == learnings_before
    finally:
        neo.close()
    kinds = [e["kind"] for e in store.list_events(analyst["id"])]
    # The OS deciding is NOT the user touching it: the steward's precision counts only the user
    assert "findings_decided" in kinds and "findings_reviewed" not in kinds
    assert not steward.touched(store, analyst["id"])


def test_a_finding_with_no_orders_is_accepted_without_asking_the_steward(
        boot, store, improvement_order, central):
    daemon = boot()
    submitted(store, improvement_order,
              one_finding("FORCE_FINDING_ACCEPT_MAJOR", proposed_orders=[]))

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert (f["status"], f["decided_by"]) == ("accepted", "neo")
    assert f["knowledge_id"]
    assert central.steward_decisions() == []


def test_with_self_dispatch_off_neo_accepts_and_the_user_says_yes_to_the_spend(
        boot, store, improvement_order, central):
    """The master switch holds: the OS files nothing nobody typed"""
    daemon = boot()
    submitted(store, improvement_order, one_finding("FORCE_FINDING_ACCEPT_MAJOR"))

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert f["status"] == "pending" and f["neo_verdict"]["verdict"] == "accept"
    assert f["steward"]["decision_id"]
    assert filed_orders(store) == []
    row = store.get_feature_order(improvement_order["id"])
    assert row["needs_attention"] and "self_dispatch" in row["attention_reason"]
    assert any("needs your decision" in r["title"] for r in central.unacked_inbox())

    # One command from the user: `--accept-all` takes what Neo left them
    out = ops.review_findings(improvement_order["id"], accept_all=True)
    assert out["status"] == "completed" and len(out["created"]) == 1
    # The user accepted it, so it is the user's work (A8): never stamped
    assert not steward.stamp_of(filed_orders(store)[0])


def test_with_self_dispatch_on_the_steward_files_the_orders_as_os_work(
        boot, store, improvement_order, central, self_dispatch_on):
    daemon = boot()
    submitted(store, improvement_order, one_finding("FORCE_FINDING_ACCEPT_MAJOR"))

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert (f["status"], f["decided_by"]) == ("accepted", "neo")
    [wo] = filed_orders(store)
    stamp = steward.stamp_of(wo)
    assert (stamp["origin_kind"], stamp["severity"]) == ("io_finding", "major")
    [row] = central.steward_decisions()
    assert row["wo_id"] == wo["id"]
    assert store.get_feature_order(improvement_order["id"])["status"] == "completed"
    assert not store.get_feature_order(improvement_order["id"])["needs_attention"]


def test_enforced_a_spent_follow_up_ledger_never_holds_a_finding(
        boot, store, improvement_order, central, self_dispatch_on):
    """wo-5b96c9da: an io finding is a reported bug, on its own ledger"""
    daemon = boot(evolution={"mode": "enforce", "weekly_usd": 0.0, "order_budget_usd": 10.0})
    submitted(store, improvement_order, one_finding("FORCE_FINDING_ACCEPT_MAJOR"))

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert f["status"] == "accepted" and "orders_held" not in f
    [wo] = filed_orders(store)
    assert steward.stamp_of(wo)["origin_kind"] == "io_finding"
    assert steward.stamp_of(wo)["track"] == "bug"
    [row] = central.steward_decisions()
    assert (row["outcome"], row["rule"]) == ("dispatch", "reported-bug")
    assert steward.drain(ops.resolve_catalog(), central)["dispatched"] == 0


def test_enforced_a_minor_finding_is_dropped_by_the_steward(
        boot, store, improvement_order, central, self_dispatch_on):
    daemon = boot(evolution={"mode": "enforce"})
    submitted(store, improvement_order, one_finding("FORCE_FINDING_ACCEPT_MINOR"))

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert (f["status"], f["decided_by"]) == ("rejected", "steward")
    assert "never worth an order" in f["feedback"]
    assert filed_orders(store) == []


def test_an_escalated_finding_is_the_users_with_neos_reason(boot, store,
                                                            improvement_order, central):
    daemon = boot()
    submitted(store, improvement_order, one_finding("no switch: the fake escalates"))

    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert f["status"] == "pending" and f["neo_verdict"]["escalate"]
    row = store.get_feature_order(improvement_order["id"])
    assert row["needs_attention"] and "Neo escalated it" in row["attention_reason"]
    assert f"jarvis io review {improvement_order['id']}" in row["attention_reason"]
    assert findings.status_label(f) == "pending"


def test_neo_unreachable_hands_the_finding_to_the_user(boot, store, improvement_order):
    daemon = boot()
    submitted(store, improvement_order, one_finding("FORCE_FINDING_ACCEPT_MAJOR"))
    [q] = neo_questions()

    daemon._note_question_unreachable(CentralStore(), q, "rc=1")

    f = finding(store, improvement_order["id"])
    assert f["status"] == "pending" and "could not decide" in f["neo_verdict"]["reason"]
    assert store.get_feature_order(improvement_order["id"])["needs_attention"]


# ---------------------------------------------------------------- the user first


def test_the_user_deciding_first_closes_neos_question_and_its_late_verdict_is_dropped(
        boot, store, improvement_order):
    daemon = boot()
    submitted(store, improvement_order, one_finding("FORCE_FINDING_REJECT"))
    [q] = neo_questions()

    ops.review_findings(improvement_order["id"], accept=["first-turn-reads"])

    neo = NeoStore()
    try:
        assert neo.get(q["id"])["status"] == "answered"
    finally:
        neo.close()
    out = ops.apply_findings_verdict(q, {"escalate": False, "approve": False,
                                         "reason": "late", "severity": "medium"})
    assert out["decided"] is False
    assert finding(store, improvement_order["id"])["decided_by"] == "user"
    daemon._neo_drain()  # nothing queued any more


def test_an_escalated_question_is_closed_once_the_user_decides_the_finding(
        boot, store, improvement_order):
    daemon = boot()
    submitted(store, improvement_order, one_finding("no switch: the fake escalates"))
    daemon._neo_drain()
    [q] = neo_questions()
    assert q["status"] == "escalated"

    ops.review_findings(improvement_order["id"], reject={"first-turn-reads": "not so"})

    violations = list(invariants.check_neo_escalations_are_live(store))
    assert [v.context["question_id"] for v in violations] == [q["id"]]
    neo = NeoStore()
    try:
        assert neo.get(q["id"])["status"] == "answered"
    finally:
        neo.close()


def test_answering_a_findings_question_names_the_finding_to_decide(boot, store,
                                                                   improvement_order):
    daemon = boot()
    submitted(store, improvement_order, one_finding("no switch: the fake escalates"))
    daemon._neo_drain()
    [q] = neo_questions()

    with pytest.raises(ops.OpsError) as e:
        ops.neo_answer_escalated(q["id"], "yes")

    assert (f"jarvis io review {improvement_order['id']} --accept first-turn-reads"
            in str(e.value))


def test_a_resubmission_closes_the_questions_about_the_report_it_replaces(
        boot, store, improvement_order):
    boot()
    submitted(store, improvement_order, a_report())
    first = {q["id"] for q in neo_questions()}

    ops.submit_findings(improvement_order["id"], a_report(summary="A second reading."))

    neo = NeoStore()
    try:
        assert all(neo.get(qid)["status"] == "answered" for qid in first)
    finally:
        neo.close()
    assert len([q for q in neo_questions() if q["status"] == "queued"]) == 2


# ---------------------------------------------------------------- reports Neo was never asked about


def legacy(store, io, report, monkeypatch):
    """A report submitted before Neo decided findings: every finding the user's, none asked"""
    with monkeypatch.context() as m:
        m.setattr(ops, "findings_reviewed_by_neo", lambda: False)
        submitted(store, io, report)
    assert neo_questions() == []
    assert store.get_feature_order(io["id"])["needs_attention"]


def test_a_report_filed_before_neo_decided_findings_goes_to_neo_once(
        boot, store, improvement_order, monkeypatch):
    daemon = boot()
    legacy(store, improvement_order, a_report(), monkeypatch)

    daemon.offer_findings_to_neo(daemon.catalog.projects[0])

    qs = neo_questions()
    assert len(qs) == 2
    row = store.get_feature_order(improvement_order["id"])
    # Every finding is Neo's now, so nothing is owed to the user
    assert not row["needs_attention"]
    assert finding(store, improvement_order["id"])["neo_question_id"] in {q["id"] for q in qs}
    assert "findings_offered_to_neo" in [e["kind"] for e in store.list_events(row["plan_wo_id"])]

    # Once: a second sweep asks nothing
    assert ops.offer_unasked_findings("proj_a", daemon.catalog.projects[0].path) == []
    assert len(neo_questions()) == 2


def test_neo_decides_a_legacy_finding_like_any_other(boot, store, improvement_order,
                                                     monkeypatch):
    daemon = boot()
    legacy(store, improvement_order, one_finding("FORCE_FINDING_REJECT"), monkeypatch)

    daemon.offer_findings_to_neo(daemon.catalog.projects[0])
    daemon._neo_drain()

    f = finding(store, improvement_order["id"])
    assert (f["status"], f["decided_by"]) == ("rejected", "neo")
    assert store.get_feature_order(improvement_order["id"])["status"] == "completed"


def test_a_finding_the_user_already_decided_is_not_offered(boot, store, improvement_order,
                                                           monkeypatch):
    daemon = boot()
    legacy(store, improvement_order, a_report(), monkeypatch)
    ops.review_findings(improvement_order["id"], accept=["first-turn-reads"])

    daemon.offer_findings_to_neo(daemon.catalog.projects[0])

    [q] = neo_questions()
    assert "first-turn-reads" not in q["question"]
    assert not finding(store, improvement_order["id"]).get("neo_question_id")


def test_a_finding_too_large_to_ask_stays_the_users_and_is_not_retried(
        boot, store, improvement_order, monkeypatch):
    from jarvis.neo_store import QuestionTooLargeError

    daemon = boot()
    legacy(store, improvement_order, one_finding("ANY"), monkeypatch)
    calls = []

    def refuse(self, *a, **kw):
        calls.append(1)
        raise QuestionTooLargeError("too large", question_chars=90000, context_chars=0, ceiling=60000)
    monkeypatch.setattr(NeoStore, "ask", refuse)

    daemon.offer_findings_to_neo(daemon.catalog.projects[0])
    daemon.offer_findings_to_neo(daemon.catalog.projects[0])

    assert len(calls) == 1
    assert finding(store, improvement_order["id"])["neo_unasked"]
    assert store.get_feature_order(improvement_order["id"])["needs_attention"]


def test_the_brake_holds_the_offer(boot, store, improvement_order, monkeypatch):
    from jarvis import fleet

    daemon = boot()
    legacy(store, improvement_order, a_report(), monkeypatch)
    monkeypatch.setattr(fleet, "load_pause", lambda central: object())

    daemon.offer_findings_to_neo(daemon.catalog.projects[0])

    assert neo_questions() == []


def test_switched_off_nothing_is_offered(boot, store, improvement_order):
    daemon = boot(neo={"review_findings": False})
    submitted(store, improvement_order, a_report())

    daemon.offer_findings_to_neo(daemon.catalog.projects[0])

    assert neo_questions() == []


# ---------------------------------------------------------------- `jarvis io ask-neo`


def test_ask_neo_puts_a_legacy_report_to_neo_by_hand(boot, store, improvement_order,
                                                     monkeypatch):
    boot()
    legacy(store, improvement_order, a_report(), monkeypatch)

    out = ops.ask_neo_about_findings(improvement_order["id"])

    assert len(out["asked"]) == 2 and out["not_asked"] == []
    assert len(neo_questions()) == 2
    row = store.get_feature_order(improvement_order["id"])
    assert not row["needs_attention"]
    [ev] = [e for e in store.list_events(row["plan_wo_id"])
            if e["kind"] == "findings_offered_to_neo"]
    assert db.from_json(ev["payload"], {})["by"] == "user"


def test_ask_neo_re_asks_a_finding_neo_escalated(boot, store, improvement_order):
    daemon = boot()
    submitted(store, improvement_order, one_finding("NO_SWITCH"))
    daemon._neo_drain()
    first = finding(store, improvement_order["id"])
    assert first["neo_verdict"]["escalate"] and first["status"] == "pending"

    out = ops.ask_neo_about_findings(improvement_order["id"])

    f = finding(store, improvement_order["id"])
    assert out["asked"] == ["first-turn-reads"]
    assert f["neo_question_id"] != first["neo_question_id"] and "neo_verdict" not in f
    assert findings.with_neo(f)
    # The new question is the one Neo's ruling lands on
    daemon._neo_drain()
    assert finding(store, improvement_order["id"])["neo_verdict"]["question_id"] == \
        f["neo_question_id"]


def test_ask_neo_skips_a_finding_neo_accepted(boot, store, improvement_order):
    """Its spend waits on the user's yes; asking Neo again changes nothing"""
    daemon = boot()
    submitted(store, improvement_order, one_finding("FORCE_FINDING_ACCEPT_MAJOR"))
    daemon._neo_drain()
    before = len(neo_questions())

    out = ops.ask_neo_about_findings(improvement_order["id"])

    assert out["asked"] == [] and "nothing to ask" in out["note"]
    assert len(neo_questions()) == before


def test_ask_neo_refuses_a_settled_report_and_a_switched_off_neo(boot, store,
                                                                 improvement_order):
    boot(neo={"review_findings": False})
    with pytest.raises(ops.OpsError, match="plan_review"):
        ops.ask_neo_about_findings(improvement_order["id"])
    submitted(store, improvement_order, a_report())
    with pytest.raises(ops.OpsError, match="review_findings"):
        ops.ask_neo_about_findings(improvement_order["id"])
