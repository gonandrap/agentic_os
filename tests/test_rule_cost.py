"""Per-rule cost: what each detector's armed fires caused (wo-85906675, Neo q2519)"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops, remedies, rules
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

COND = {"all": [{"field": "status", "op": "eq", "value": "needs_review"}]}


@pytest.fixture()
def fleet(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    central = CentralStore()
    yield central
    central.close()


def _bill(project: str, wo_id: str, usd: float) -> None:
    """One settled worker turn costing `usd`"""
    store = ProjectStore(ops.registered_project_paths()[project])
    try:
        turn = store.create_turn(wo_id, kind="message", prompt="work")
        store.finish_turn(turn["id"], "done", result="done", cost_usd=usd,
                          usage_json=json.dumps({"total_cost_usd": usd}))
    finally:
        store.close()


def _rule(central: CentralStore) -> tuple[dict, dict]:
    det = central.add_detector("gap-under-test", COND, project="proj_a")
    rm = central.add_remedy_rule(det["id"], "file_work_order", params={},
                                 argument="fix the root cause")
    return det, rm


def _fire(central, det, rm, *, alarm_id="al-1", question_id=None):
    return central.record_rule_fire(
        detector_id=det["id"], project="proj_a", order_id="wo-subject",
        order_kind="work_order", fingerprint="fp", mode=rules.ARMED,
        outcome=rules.PROPOSED, remedy_rule_id=rm["id"], alarm_id=alarm_id,
        question_id=question_id)


def test_a_rule_is_charged_its_neo_review_its_filed_order_and_that_orders_retry(fleet):
    det, rm = _rule(fleet)
    _fire(fleet, det, rm, question_id=4242)
    # The gate review, and Neo re-asked after a failure: same question, two calls
    for usd in (0.25, 0.15):
        fleet.add_agent_call("neo", project="proj_a", wo_id="wo-subject",
                             question_id=4242, usage={"total_cost_usd": usd})
    fix = ops.create_work_order("proj_a", "fix the root cause")
    assert fleet.note_fire_orders("al-1", [fix["id"]]) == 1
    _bill("proj_a", fix["id"], 1.00)
    # A `wo retry` relaunches under the same id
    _bill("proj_a", fix["id"], 0.50)
    fleet.conn.commit()

    cost = ops.rules_show(det["id"])["cost"]
    assert cost["total_usd"] == pytest.approx(1.90)
    assert cost["neo_usd"] == pytest.approx(0.40)
    assert cost["orders_usd"] == pytest.approx(1.50)
    assert cost["by_remedy"] == {rm["id"]: pytest.approx(1.90)}
    assert cost["orders"] == [{"id": fix["id"], "project": "proj_a"}]
    assert "$1.90" in cost["note"]

    listed = next(r for r in ops.rules_list()["rules"] if r["id"] == det["id"])
    assert listed["cost"]["total_usd"] == pytest.approx(1.90)


def test_an_order_named_by_two_fires_is_charged_once(fleet):
    det, rm = _rule(fleet)
    _fire(fleet, det, rm, alarm_id="al-1")
    _fire(fleet, det, rm, alarm_id="al-2")
    fix = ops.create_work_order("proj_a", "fix")
    fleet.note_fire_orders("al-1", [fix["id"]])
    fleet.note_fire_orders("al-2", [fix["id"]])
    _bill("proj_a", fix["id"], 2.00)
    fleet.conn.commit()
    assert ops.rules_show(det["id"])["cost"]["total_usd"] == pytest.approx(2.00)


def test_a_rule_that_never_spent_says_so_and_prints_no_dollar_figure(fleet, capsys):
    det, _rm = _rule(fleet)
    fleet.conn.commit()
    data = ops.rules_show(det["id"])
    assert data["cost"]["total_usd"] is None
    cli._print_rules_show(data)
    out = capsys.readouterr().out
    assert "caused no attributable spend" in out
    assert "$0.00" not in out
    assert ops.RULE_COST_UNATTRIBUTED in out


def test_the_cli_list_prints_each_rules_cost(fleet, capsys):
    det, rm = _rule(fleet)
    _fire(fleet, det, rm, question_id=7)
    fleet.add_agent_call("neo", question_id=7, usage={"total_cost_usd": 0.30})
    fleet.conn.commit()
    cli._print_rules_list(ops.rules_list())
    out = capsys.readouterr().out
    assert "cost: $0.30" in out and "1 gate review by Neo" in out


def test_the_file_work_order_remedy_attributes_what_it_filed(fleet):
    det, rm = _rule(fleet)
    fire = _fire(fleet, det, rm, alarm_id="al-9")
    intent = remedies.Intent(remedy="file_work_order", subject_id="wo-subject",
                             carrier_id="wo-subject", argument="x", reason="y",
                             alarm_id="al-9")
    remedies._attribute(fleet, intent, "wo-filed")
    remedies._attribute(fleet, intent, "wo-ship")
    # §11's user path has no alarm and no fire: nothing to attribute
    remedies._attribute(fleet, remedies.Intent(
        remedy="file_work_order", subject_id="wo-subject", carrier_id="wo-subject",
        argument="x", reason="y"), "wo-user")
    got = fleet.get_rule_fire(int(fire["id"]))
    assert json.loads(got["filed_orders"]) == ["wo-filed", "wo-ship"]
