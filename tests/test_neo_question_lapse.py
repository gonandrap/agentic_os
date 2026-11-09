"""An open worker question lapses when its order ends; it is never answered for anyone (io-d5e62d4d)"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops, timeline
from jarvis.invariants import check_project
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def neo(jarvis_home):
    n = NeoStore()
    yield n
    n.close()


def escalated(neo, wo_id: str, kind: str = "question") -> int:
    q = neo.ask("proj_a", wo_id, "may I?", kind=kind)
    neo.mark(q["id"], "escalated", reason="the user must rule", cause="high-stakes")
    return q["id"]


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_closing_an_order_lapses_its_question_with_no_answer(store, neo, status):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])

    store.set_status(wo["id"], status)

    q = neo.get(qid)
    assert (q["status"], q["answer"], q["answered_by"], q["answered_at"]) \
        == ("lapsed", None, None, None)
    assert status in q["lapsed_reason"]
    assert q["answer_reason"] == "the user must rule", "why Neo escalated stays on the record"
    assert [e["payload"]["question_id"] if isinstance(e["payload"], dict)
            else json.loads(e["payload"])["question_id"]
            for e in store.events_of_kind(wo["id"], "question_lapsed")] == [qid]
    assert ops._neo_attention()[1] == []


def test_a_failed_order_keeps_its_question_open(store, neo):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])

    store.set_status(wo["id"], "failed")

    violations = check_project(store, repair=True)

    assert neo.get(qid)["status"] == "escalated"
    assert [v for v in violations if v.invariant == "INV-NEO-QUESTION-LAPSED"] == []


def test_the_sweep_lapses_an_alarm_on_an_already_failed_order(store, neo):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])
    alarm = escalated(neo, wo["id"], kind="alarm")
    store.update_work_order(wo["id"], status="failed")

    repaired = [v for v in check_project(store, repair=True)
                if v.invariant == "INV-NEO-QUESTION-LAPSED"]

    assert [v.context["question_id"] for v in repaired] == [alarm]
    assert neo.get(alarm)["status"] == "lapsed"
    assert neo.get(qid)["status"] == "escalated"


def test_a_late_answer_is_refused_plainly_and_teaches_nothing(store, neo):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])
    store.set_status(wo["id"], "completed")

    with pytest.raises(ops.OpsError, match="has lapsed, so it takes no answer"):
        ops.neo_answer_escalated(qid, "too late")
    with pytest.raises(ops.OpsError, match="is lapsed, not answered"):
        ops.neo_review(qid, approved=False, feedback="wrong")

    assert neo.get(qid)["answer"] is None
    assert neo.learnings("proj_a") == []


def test_kinds_with_their_own_subject_stay_open(store, neo):
    wo = store.create_work_order(title="t")
    triage = escalated(neo, "", kind="triage")
    validation = escalated(neo, "", kind="validation")
    unattached = escalated(neo, "")
    empty_alarm = escalated(neo, "", kind="alarm")

    store.set_status(wo["id"], "completed")
    violations = check_project(store, repair=True)

    assert [v for v in violations if v.invariant == "INV-NEO-QUESTION-LAPSED"] == []
    assert neo.get(triage)["status"] == neo.get(validation)["status"] == "escalated"
    assert neo.get(unattached)["status"] == neo.get(empty_alarm)["status"] == "escalated"


@pytest.mark.parametrize("status", ["completed", "cancelled", "failed"])
def test_an_alarm_about_an_ended_order_lapses_on_close(store, neo, status):
    wo = store.create_work_order(title="t")
    alarm = escalated(neo, wo["id"], kind="alarm")

    store.set_status(wo["id"], status)

    q = neo.get(alarm)
    assert (q["status"], q["answer"], q["answered_by"]) == ("lapsed", None, None)
    assert status in q["lapsed_reason"]
    assert neo.learnings("proj_a") == []


def test_a_stale_alarm_on_an_already_closed_order_lapses_on_the_next_sweep(store, neo):
    """The Q1997 shape: an alarm escalated, its order completed before this release"""
    wo = store.create_work_order(title="t")
    alarm = escalated(neo, wo["id"], kind="alarm")
    store.update_work_order(wo["id"], status="completed")

    repaired = [v for v in check_project(store, repair=True)
                if v.invariant == "INV-NEO-QUESTION-LAPSED"]

    assert [v.context["question_id"] for v in repaired] == [alarm]
    q = neo.get(alarm)
    assert (q["status"], q["answer"]) == ("lapsed", None)
    assert wo["id"] in q["lapsed_reason"]


def test_a_row_neo_is_answering_is_left_until_neo_settles_it(store, neo):
    wo = store.create_work_order(title="t")
    q = neo.ask("proj_a", wo["id"], "may I?")
    assert neo.claim_next()["id"] == q["id"]

    store.set_status(wo["id"], "completed")
    assert neo.get(q["id"])["status"] == "answering"

    neo.mark(q["id"], "escalated", reason="the user must rule")
    check_project(store, repair=True)
    assert neo.get(q["id"])["status"] == "lapsed"


def test_the_invariant_repairs_a_row_that_was_already_closed(store, neo):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])
    store.update_work_order(wo["id"], status="completed")  # as every row before this release

    reported = [v for v in check_project(store, repair=False)
                if v.invariant == "INV-NEO-QUESTION-LAPSED"]
    assert [v.context["question_id"] for v in reported] == [qid]
    assert neo.get(qid)["status"] == "escalated", "reporting mode writes nothing"

    repaired = [v for v in check_project(store, repair=True)
                if v.invariant == "INV-NEO-QUESTION-LAPSED"]
    assert [v.context["question_id"] for v in repaired] == [qid]
    q = neo.get(qid)
    assert (q["status"], q["answer"]) == ("lapsed", None)
    assert [v for v in check_project(store, repair=True)
            if v.invariant == "INV-NEO-QUESTION-LAPSED"] == []


def test_a_close_survives_neo_being_unavailable(store, neo, monkeypatch):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])

    def boom(*_a, **_k):
        raise RuntimeError("neo.db is locked")

    with monkeypatch.context() as down:
        down.setattr(NeoStore, "lapse_worker_questions", boom)
        store.set_status(wo["id"], "completed")
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert neo.get(qid)["status"] == "escalated"

    check_project(store, repair=True)
    assert neo.get(qid)["status"] == "lapsed"


def test_the_timeline_says_it_lapsed(store, neo):
    wo = store.create_work_order(title="t")
    qid = escalated(neo, wo["id"])
    store.set_status(wo["id"], "completed")

    entries = timeline.build_timeline(store.get_work_order(wo["id"]),
                                      store.events_of_kind(wo["id"], "question_lapsed"), [])

    assert f"Neo question {qid} lapsed" in str(entries)


def test_neo_list_reaches_an_escalation_older_than_the_newest_200(neo, capsys):
    old = escalated(neo, "wo-old")
    for _ in range(200):
        q = neo.ask("proj_a", "wo-new", "routine")
        neo.record_answer(q["id"], "yes")
        neo.review(q["id"], approved=True)

    assert cli.main(["neo", "list", "--status", "escalated", "--json"]) == 0
    assert [q["id"] for q in json.loads(capsys.readouterr().out)] == [old]
    assert cli.main(["neo", "list", "--json"]) == 0
    assert old in [q["id"] for q in json.loads(capsys.readouterr().out)]
