"""An alarm's attention flag and `true_blockers` agree in every status (io-d5e62d4d)"""

from __future__ import annotations

import pytest

from jarvis import ops, supervisor
from jarvis.daemon import METER_RESIDUAL_ALARM
from jarvis.invariants import TERMINAL_STATUSES, check_project, true_blockers
from jarvis.neo_store import NeoStore
from jarvis.project_store import OPEN_STATUSES, TERMINAL_STATUSES as SETTLED_STATUSES, ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

EVERY_STATUS = OPEN_STATUSES + SETTLED_STATUSES


@pytest.fixture()
def store(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    opened = ProjectStore(project)
    yield opened
    opened.close()


def _order(store: ProjectStore, status: str) -> str:
    wo_id = store.create_work_order("carrier")["id"]
    store.set_status(wo_id, status)
    return wo_id


def _agree(store: ProjectStore, wo_id: str) -> None:
    wo = store.get_work_order(wo_id)
    assert bool(wo["needs_attention"]) == bool(true_blockers(store, wo))


@pytest.mark.parametrize("alarm_status", ["escalated", "failed"])
@pytest.mark.parametrize("status", EVERY_STATUS)
def test_flag_and_true_blockers_agree_through_a_review(store, status, alarm_status):
    wo_id = _order(store, status)
    alarm = store.add_alarm(wo_id, "long-turn", 1, "a long turn", status=alarm_status)
    blocker = supervisor.ALARM_BLOCKER.format(alarm_id=alarm["id"])

    check_project(store, repair=True)

    assert blocker in true_blockers(store, store.get_work_order(wo_id))
    assert store.get_work_order(wo_id)["needs_attention"] == 1
    _agree(store, wo_id)

    ops.review_alarm(alarm["id"], approved=True)

    assert blocker not in true_blockers(store, store.get_work_order(wo_id))
    _agree(store, wo_id)


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_the_sweep_keeps_the_flag_on_a_clean_settled_carrier(store, status):
    """The wipe the finding predicted from code reading: INV-ATTENTION-PHANTOM"""
    wo_id = _order(store, status)
    alarm = store.add_alarm(wo_id, "long-turn", 1, "a long turn", status="escalated")
    blocker = supervisor.ALARM_BLOCKER.format(alarm_id=alarm["id"])
    store.flag_attention(wo_id, blocker)

    assert check_project(store, repair=True) == []

    assert store.get_work_order(wo_id)["attention_reason"] == blocker


def test_the_sweep_does_not_relabel_an_alarm_flag_to_assumptions(store):
    """The observed defect: wo-033d60db, al-86afc8b4"""
    wo_id = _order(store, "needs_review")
    store.add_assumption(wo_id, "an assumption")
    alarm = store.add_alarm(wo_id, "long-turn", 1, "a long turn", status="escalated")
    blocker = supervisor.ALARM_BLOCKER.format(alarm_id=alarm["id"])
    store.flag_attention(wo_id, blocker)

    check_project(store, repair=True)

    assert store.get_work_order(wo_id)["attention_reason"] == blocker


@pytest.mark.parametrize("status", EVERY_STATUS)
def test_a_meter_alarm_never_blocks_its_carrier(store, status):
    """PR 1548: the carrier is innocent"""
    wo_id = _order(store, status)
    alarm = store.add_alarm(wo_id, METER_RESIDUAL_ALARM, 0, "gap", status="escalated")

    assert not any(alarm["id"] in b for b in true_blockers(store, store.get_work_order(wo_id)))


def test_an_alarm_neo_is_still_deciding_is_not_the_users(store, project):
    wo_id = _order(store, "running")
    neo = NeoStore()
    try:
        question = neo.ask(project.name, wo_id, "is it stuck?", context="", kind="alarm")
    finally:
        neo.close()
    alarm = store.add_alarm(wo_id, "long-turn", 1, "a long turn", status="escalated")
    store.update_alarm(alarm["id"], neo_question_id=question["id"])

    assert not any(alarm["id"] in b for b in true_blockers(store, store.get_work_order(wo_id)))


def test_an_acknowledged_alarm_is_not_raised_again_on_a_settled_order(store):
    wo_id = _order(store, "completed")
    alarm = store.add_alarm(wo_id, "long-turn", 1, "a long turn", status="escalated")
    store.ack_attention(wo_id, [supervisor.ALARM_BLOCKER.format(alarm_id=alarm["id"])])

    check_project(store, repair=True)

    assert store.get_work_order(wo_id)["needs_attention"] == 0


def test_a_settled_order_gains_a_flag_for_its_alarm_only(store):
    """Pending assumptions on a settled order stay unraised: the complaint behind io-d5e62d4d"""
    wo_id = _order(store, "completed")
    store.add_assumption(wo_id, "an assumption")
    alarm = store.add_alarm(wo_id, "long-turn", 1, "a long turn", status="escalated")

    check_project(store, repair=True)

    assert store.get_work_order(wo_id)["attention_reason"] == supervisor.ALARM_BLOCKER.format(
        alarm_id=alarm["id"])

    ops.review_alarm(alarm["id"], approved=True)

    assert store.get_work_order(wo_id)["needs_attention"] == 0
    check_project(store, repair=True)
    assert store.get_work_order(wo_id)["needs_attention"] == 0


def test_an_alarm_free_settled_order_is_not_raised_for_its_assumptions(store):
    wo_id = _order(store, "completed")
    store.add_assumption(wo_id, "an assumption")

    check_project(store, repair=True)

    assert store.get_work_order(wo_id)["needs_attention"] == 0


def test_reviewing_one_of_two_alarms_on_a_settled_order_hands_the_flag_over(store):
    wo_id = _order(store, "completed")
    alarms = [store.add_alarm(wo_id, "long-turn", seq, "a long turn", status="escalated")
              for seq in (1, 2)]

    check_project(store, repair=True)

    named, other = (alarms if store.get_work_order(wo_id)["attention_reason"]
                    == supervisor.ALARM_BLOCKER.format(alarm_id=alarms[0]["id"])
                    else reversed(alarms))
    ops.review_alarm(named["id"], approved=True)

    assert store.get_work_order(wo_id)["attention_reason"] == supervisor.ALARM_BLOCKER.format(
        alarm_id=other["id"])

    ops.review_alarm(other["id"], approved=True)

    assert store.get_work_order(wo_id)["needs_attention"] == 0
