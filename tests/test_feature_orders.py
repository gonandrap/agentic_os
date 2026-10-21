"""Feature orders: the planned unit of work above the work order"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

import pytest

from jarvis import db, ops, plans, specs
from jarvis import neo as neo_mod
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import (
    FIXTURE_DESIGN_DOC,
    FIXTURE_DESIGN_DOC_BODY,
    fixture_spec_section,
)


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def child(key: str, needs: list[str] | None = None, extra: str = "") -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} half of the exporter: add the module, wire it into the "
            f"command that calls it, and cover both paths with tests in the existing "
            f"suite. Do not change the public interface of the caller. {extra}"
        ),
        "needs": needs or [],
        "spec_section": fixture_spec_section(key),
    }


def a_plan(*children: dict, **extra) -> dict:
    # The fake Neo reads the plan-review QUESTION, which is a skeleton without the briefs (wo-e4a359cb)
    markers = " ".join(sorted({m for c in children
                               for m in ("FORCE_APPROVE", "FORCE_REJECT")
                               if m in c["description"]}))
    return {"summary": f"an exporter {markers}".strip(),
            # Every plan must stand on a design document
            "design_doc": FIXTURE_DESIGN_DOC,
            "children": list(children), **extra}


ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


@pytest.fixture()
def planning(started, store):
    """A feature order whose planner has been opened and dispatched"""
    daemon = started
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    return daemon, store.get_feature_order(fo["id"])


def backlog_item(central, item_id: str) -> dict:
    item = central.get_backlog(item_id)
    assert item is not None, item_id
    return item


def planner_of(store, fo_id: str) -> dict:
    return store.get_work_order(store.get_feature_order(fo_id)["plan_wo_id"])


# -- creation


def test_a_feature_order_starts_pending_and_plans_nothing_by_itself(started, store):
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)

    assert fo["status"] == "pending"
    assert fo["plan_wo_id"] is None
    assert store.list_work_orders() == []


def test_a_feature_order_without_a_description_is_refused(started):
    """A work order can survive a bare title because a human reads it"""
    with pytest.raises(ops.OpsError, match="needs a description"):
        ops.create_feature_order("proj_a", "CSV export")


# -- the planner


def test_the_daemon_opens_exactly_one_planner(planning, store):
    _, fo = planning

    assert fo["status"] == "planning"
    planner = planner_of(store, fo["id"])
    assert planner["kind"] == "planner"
    assert planner["parent_id"] == fo["id"]
    assert planner["status"] in ("dispatching", "running")
    # ONE child, not a fan-out
    assert len(store.list_work_orders()) == 1


def test_the_planner_carries_the_ask_verbatim_and_a_planner_briefing(planning, store,
                                                                     fake_claude):
    _, fo = planning
    planner = planner_of(store, fo["id"])

    # On the record: what the user actually asked for
    assert planner["description"] == ASK
    # In the briefing: the planner contract, not the worker one
    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"], count=1)[0]
    prompt = call["argv"][-1]
    assert "You are the PLANNER" in prompt
    assert f"jarvis fo plan {fo['id']} --from-file" in prompt
    assert "Do not build the feature" in prompt
    # The rule the whole plan lives or dies on has to be in there
    assert "sees its own description plus its section, and nothing else" in prompt
    # And `fo plan` is its finish — `jarvis wo finish` is only the re-delivery after a refusal...
    assert "IS your finish" in prompt


def test_a_planner_runs_on_ordinary_worker_permissions(planning, store, project):
    """A DELIBERATE choice, pinned here so changing it has to be deliberate too"""
    _, fo = planning
    planner_id = fo["plan_wo_id"]

    settings = json.loads(
        (project / ".jarvis" / "worker-settings" / f"{planner_id}.json").read_text())

    allow = settings["permissions"]["allow"]
    assert any(r.startswith("Write(") and planner_id in r for r in allow)
    # The only denies are the schema-trimming defaults (worker.denied_tools)
    from jarvis.catalog import DEFAULT_WORKER_DENIED_TOOLS
    assert set(settings["permissions"].get("deny", [])) <= set(DEFAULT_WORKER_DENIED_TOOLS)


def test_a_planner_is_not_created_twice(planning, store):
    """Idempotence is by status, not by a flag: the feature order leaves `pending` in the same call that..."""
    daemon, fo = planning
    daemon.tick()
    daemon.tick()

    assert len([w for w in store.list_work_orders() if w["kind"] == "planner"]) == 1


def test_an_ordinary_work_order_is_untouched_by_any_of_this(started, store):
    """The migration is additive"""
    wo = ops.create_work_order("proj_a", "an ordinary job")

    row = store.get_work_order(wo["id"])
    assert row["parent_id"] is None
    assert row["kind"] == "worker"
    assert store.claim_next_pending()["id"] == wo["id"]


# -- plan submission


def test_submitting_a_plan_parks_it_for_review_and_settles_the_planner(planning, store):
    _, fo = planning

    out = ops.submit_plan(fo["id"], a_plan(child("reader"), child("writer")))

    assert out["status"] == "plan_review"
    assert store.get_feature_order(fo["id"])["status"] == "plan_review"
    # `jarvis fo plan` IS the planner's `jarvis wo finish` — nothing is left running
    assert planner_of(store, fo["id"])["result_summary"]
    # Still nothing created: the plan is a proposal until it is released
    assert [w for w in store.list_work_orders() if w["kind"] == "worker"] == []


def test_a_planner_whose_merge_was_gated_settles_completed_and_not_into_the_queue(
        planning, store):
    """The gate records the pull request; ROUTING still needs a submitter's `--pr` (#742)"""
    from jarvis import gates

    _, fo = planning
    planner = planner_of(store, fo["id"])
    pr = "https://github.com/acme/proj/pull/735"
    approval = store.add_approval(planner["id"], gates.PR_MERGE,
                                  f"gh pr merge {pr} --squash")
    gates.apply_decision(store, approval["id"], verdict="approved",
                         reason="the plan's own docs PR", decided_by="neo")
    assert store.get_work_order(planner["id"])["pr_url"] == pr

    out = ops.submit_plan(fo["id"], a_plan(child("reader"), child("writer")))

    assert out["status"] == "plan_review"
    settled = store.get_work_order(planner["id"])
    assert settled["status"] == "completed"
    assert settled["pr_url"] == pr
    assert ops.validation_rounds(store, wo_id=planner["id"]) == []


def test_an_invalid_plan_creates_nothing_and_names_every_problem(planning, store):
    _, fo = planning

    with pytest.raises(ops.OpsError) as e:
        ops.submit_plan(fo["id"], a_plan(child("a", needs=["ghost"]),
                                         {"key": "b", "title": "B", "description": "x"}))

    assert "ghost" in str(e.value)
    assert "under the" in str(e.value)  # the short description, reported in the same go
    assert store.get_feature_order(fo["id"])["status"] == "planning"
    assert store.get_feature_order(fo["id"])["plan"] is None


def test_a_plan_cannot_be_submitted_to_a_feature_order_that_is_not_planning(started,
                                                                            store):
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)

    with pytest.raises(ops.OpsError, match="not waiting for a plan"):
        ops.submit_plan(fo["id"], a_plan(child("a")))


# -- Neo's review


def released(daemon, store, fo_id: str) -> dict:
    daemon._neo_drain()
    return store.get_feature_order(fo_id)


def test_neo_releasing_a_plan_creates_the_children_with_their_edges(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "executing"
    children = store.feature_children(fo["id"], kinds=("worker",))
    assert [c["title"] for c in children] == ["Build schema", "Build api"]
    assert all(c["parent_id"] == fo["id"] and c["kind"] == "worker" for c in children)
    # The plan's local keys became real work-order ids on a real Phase 1 edge
    assert store.dependencies(children[1]) == [children[0]["id"]]
    assert store.dependencies(children[0]) == []


def test_the_children_are_dispatched_in_dependency_order(planning, store):
    """No new scheduler: the claim-time filter Phase 1 shipped does all of it"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    released(daemon, store, fo["id"])
    schema, api = store.feature_children(fo["id"], kinds=("worker",))

    assert store.claim_next_pending()["id"] == schema["id"]
    assert store.claim_next_pending() is None  # `api` is blocked, and stays pending

    assert store.get_work_order(api["id"])["status"] == "pending"


def test_neo_sending_a_plan_back_returns_it_to_planning_with_the_reason(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_REJECT")))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "planning"
    assert store.feature_children(fo["id"], kinds=("worker",)) == []
    # The reason reaches the planner as a message
    queued = store.list_messages(fo["plan_wo_id"])
    assert any("sent back" in m["content"] and "child two needs more context"
               in m["content"] for m in queued)


def test_a_rejected_plan_can_be_resubmitted_from_plan_review(planning, store):
    """The feature order is `planning` after a rejection"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("first")))

    out = ops.submit_plan(fo["id"], a_plan(child("second")))

    assert out["children"] == 1
    plan = db.from_json(store.get_feature_order(fo["id"])["plan"], {})
    assert plan["children"][0]["key"] == "second"


def test_neo_escalating_puts_the_feature_order_in_front_of_the_user(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))  # no FORCE_: the fake escalates

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "plan_review"          # still undecided
    assert fo["needs_attention"] == 1
    assert "plan needs your review" in fo["attention_reason"]
    assert store.feature_children(fo["id"], kinds=("worker",)) == []


def test_a_plan_at_the_cap_goes_to_the_user_even_when_neo_releases_it(planning, store):
    """The cap is one of the two backstops the Neo-reviews-plans default rests on"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(
        child("c0", extra="FORCE_APPROVE"),
        *[child(f"c{i}") for i in range(1, plans.CHILD_CAP)],
    ))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "plan_review"
    assert fo["needs_attention"] == 1
    assert f"over the cap of {plans.CHILD_CAP}" in fo["attention_reason"]
    assert "approve" in fo["attention_reason"]  # Neo's reading, attached
    assert store.feature_children(fo["id"], kinds=("worker",)) == []
    # WHO decided, on the row `jarvis neo stats` groups by: the cap did, not Neo (docs/superpowers/specs/2026-10-01-neo-observability.md §1.)
    assert question(fo["plan_question_id"])["escalation_cause"] == "scope-over-cap"


def test_a_plan_under_the_cap_is_released_by_neo_alone(planning, store):
    """The control for the test above: the cap must not be so eager that the ordinary case still costs the..."""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(
        child("c0", extra="FORCE_APPROVE"),
        *[child(f"c{i}") for i in range(1, plans.CHILD_CAP - 1)],
    ))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "executing"
    assert fo["needs_attention"] == 0


# -- the user's own decision


def test_the_user_can_release_an_escalated_plan(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema"), child("api", needs=["schema"])))
    released(daemon, store, fo["id"])

    out = ops.review_plan(fo["id"], accept=True, feedback="looks right", decided_by="user")

    assert out["status"] == "executing"
    assert store.get_feature_order(fo["id"])["needs_attention"] == 0
    assert len(store.feature_children(fo["id"], kinds=("worker",))) == 2


def test_the_user_rejecting_needs_a_reason(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))
    released(daemon, store, fo["id"])

    with pytest.raises(ops.OpsError, match="rejection needs feedback"):
        ops.review_plan(fo["id"], accept=False, decided_by="user")


def test_neos_verdict_is_dropped_if_the_user_got_there_first(planning, store):
    """Both deciders share one function"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE")))
    ops.review_plan(fo["id"], accept=False, feedback="wrong shape", decided_by="user")

    daemon._neo_drain()

    assert store.get_feature_order(fo["id"])["status"] == "planning"
    assert store.feature_children(fo["id"], kinds=("worker",)) == []


# -- settling: how a feature order ends


@pytest.fixture()
def executing(planning, store):
    """A released feature order with two children, `schema` then `api`"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    released(daemon, store, fo["id"])
    schema, api = store.feature_children(fo["id"], kinds=("worker",))
    return daemon, store.get_feature_order(fo["id"]), schema, api


def test_a_feature_order_completes_only_when_every_child_has(executing, store):
    daemon, fo, schema, api = executing

    store.set_status(schema["id"], "completed")
    daemon.tick()
    assert store.get_feature_order(fo["id"])["status"] == "executing"

    store.set_status(api["id"], "completed")
    daemon.tick()

    assert store.get_feature_order(fo["id"])["status"] == "completed"
    assert store.get_feature_order(fo["id"])["needs_attention"] == 0


def test_a_child_waiting_for_its_pr_to_merge_does_not_count_as_done(executing, store):
    """The strict rule Phase 1 shipped for dependency edges"""
    daemon, fo, schema, api = executing
    store.set_status(schema["id"], "completed")
    store.set_status(api["id"], "waiting_pr_merge")

    daemon.tick()

    assert store.get_feature_order(fo["id"])["status"] == "executing"


def test_one_failed_child_fails_the_feature_and_says_which(executing, store):
    daemon, fo, schema, api = executing

    store.set_status(schema["id"], "failed")
    daemon.tick()

    fo = store.get_feature_order(fo["id"])
    assert fo["status"] == "failed"
    assert fo["needs_attention"] == 1
    assert schema["id"] in fo["attention_reason"]
    assert "failed" in fo["attention_reason"]


def test_a_cancelled_child_also_fails_the_feature_but_words_it_as_cancellation(
        executing, store):
    """It did not settle successfully, so `completed` would be a lie"""
    daemon, fo, schema, api = executing

    store.set_status(schema["id"], "cancelled")
    daemon.tick()

    fo = store.get_feature_order(fo["id"])
    assert fo["status"] == "failed"
    assert "was cancelled" in fo["attention_reason"]
    assert "failed —" not in fo["attention_reason"]


def test_a_failed_feature_flags_once_and_not_every_tick(executing, store):
    """Flag-once is true by construction"""
    daemon, fo, schema, api = executing
    store.set_status(schema["id"], "failed")
    daemon.tick()
    store.clear_feature_attention(fo["id"])  # the user acknowledges it

    daemon.tick()
    daemon.tick()

    assert store.get_feature_order(fo["id"])["needs_attention"] == 0


def test_a_feature_order_still_planning_is_never_settled(planning, store):
    """Only `executing` is looked at"""
    daemon, fo = planning

    daemon.tick()

    assert store.get_feature_order(fo["id"])["status"] == "planning"


def test_a_completed_feature_closes_the_backlog_item_it_came_from(started, store):
    """A feature order has no `finish`"""
    from jarvis.central_store import CentralStore

    daemon = started
    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "CSV export", description=ASK)
        out = ops.promote_backlog(item["id"], as_feature=True)
        daemon.tick()
        ops.submit_plan(out["fo_id"], a_plan(child("only", extra="FORCE_APPROVE")))
        released(daemon, store, out["fo_id"])
        store.set_status(store.feature_children(out["fo_id"], kinds=("worker",))[0]["id"], "completed")

        daemon.tick()

        assert store.get_feature_order(out["fo_id"])["status"] == "completed"
        assert backlog_item(central, item["id"])["status"] == "done"
    finally:
        central.close()


def test_promoting_a_backlog_item_as_a_feature_plans_it_instead_of_dispatching_it(
        started, store):
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "CSV export", description=ASK)

        out = ops.promote_backlog(item["id"], as_feature=True)

        assert out["fo_id"].startswith("fo-")
        assert backlog_item(central, item["id"])["promoted_wo_id"] == out["fo_id"]
        # Nothing dispatched: a feature order is planned before anything runs
        assert store.list_work_orders() == []
    finally:
        central.close()


# -- cancellation


def test_cancelling_a_feature_order_stops_everything_it_owns(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    released(daemon, store, fo["id"])

    out = ops.cancel_feature_order(fo["id"])

    assert store.get_feature_order(fo["id"])["status"] == "cancelled"
    # A feature order that stopped while its children kept running would be a label
    assert {c["status"] for c in store.feature_children(fo["id"], kinds=("worker",))} == {"cancelled"}
    assert fo["plan_wo_id"] not in out["cancelled_work_orders"]  # already settled


def test_a_settled_feature_order_cannot_be_cancelled_again(planning, store):
    daemon, fo = planning
    ops.cancel_feature_order(fo["id"])

    with pytest.raises(ops.OpsError, match="already cancelled"):
        ops.cancel_feature_order(fo["id"])


# -- surfaces


def test_show_renders_the_tree_and_the_plan(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    released(daemon, store, fo["id"])

    detail = ops.show_feature_order(fo["id"])

    assert detail["progress"] == {**detail["progress"], "children": 2, "done": 0}
    assert detail["planner"]["id"] == fo["plan_wo_id"]
    assert "Build schema" in detail["plan_text"]
    api = detail["children"][1]
    assert api["depends_on"] == [detail["children"][0]["id"]]
    # The derived label, so the tree does not claim a blocked child is about to start
    assert api["status_label"].startswith("pending — blocked by")


def test_an_escalated_plan_is_one_attention_line_naming_the_feature_order(planning,
                                                                          store):
    """One line per feature order, never one per child: the strip that names everything is the strip that..."""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))
    released(daemon, store, fo["id"])

    status = ops.os_status()

    items = [a for a in status["attention"] if a.get("fo_id") == fo["id"]]
    assert len(items) == 1
    assert items[0]["decide"] == f"jarvis fo show {fo['id']}"
    # And NOT as a raw Neo escalation telling the user to `jarvis neo answer`
    assert not [a for a in status["attention"] if a["status"] == "neo_escalated"]


def test_the_cli_round_trips_a_plan_through_a_file(planning, store, tmp_path, capsys):
    """`--from-file`, not an argument: a plan is a long string full of repo paths"""
    from jarvis import cli

    daemon, fo = planning
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(a_plan(child("schema", extra="FORCE_APPROVE"))))

    assert cli.main(["fo", "plan", fo["id"], "--from-file", str(path)]) == 0
    released(daemon, store, fo["id"])
    assert cli.main(["fo", "show", fo["id"]]) == 0

    out = capsys.readouterr().out
    assert "Build schema" in out


def test_the_cli_refuses_a_plan_file_that_is_not_json(planning, tmp_path, capsys):
    """A planner reads this message and nothing else"""
    from jarvis import cli

    _, fo = planning
    path = tmp_path / "plan.json"
    path.write_text("not json")

    assert cli.main(["fo", "plan", fo["id"], "--from-file", str(path)]) == 1

    assert "not valid JSON" in capsys.readouterr().err


# -- a planner assumption holds its feature


def a_released_feature(daemon, store, *keys: str) -> dict:
    """A feature whose children exist, are independent, and have not been dispatched"""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(*[child(k, extra="FORCE_APPROVE") for k in keys]))
    return released(daemon, store, fo["id"])


def a_bare_feature(store, *, status: str = "pending", assumptions: int = 1,
                   planner_status: str = "completed") -> tuple[dict, dict, dict]:
    """`(feature, planner, child)` built from rows — no planner session, no plan"""
    fo = store.create_feature_order("CSV export", description=ASK)
    planner = store.create_work_order("plan it", kind="planner", parent_id=fo["id"],
                                      status=planner_status)
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"], status="executing")
    kid = store.create_work_order("build it", parent_id=fo["id"], status=status)
    for i in range(assumptions):
        store.add_assumption(planner["id"], f"the exporter writes CSV, not JSON ({i})")
    return fo, planner, kid


def test_a_planner_assumption_holds_its_features_children(started, store):
    fo = a_released_feature(started, store, "schema")
    store.add_assumption(fo["plan_wo_id"], "the export table gets a new column")

    assert store.claim_next_pending() is None
    kid = store.feature_children(fo["id"], kinds=("worker",))[0]
    hold = store.plan_hold(store.get_work_order(kid["id"]))
    assert hold == {"fo_id": fo["id"], "planner_id": fo["plan_wo_id"], "n": 1}


def test_a_held_child_does_not_starve_the_queue(started, store):
    """The property §2.2 rests on: the filter is in the ROW SELECTION"""
    held = a_released_feature(started, store, "schema")
    store.add_assumption(held["plan_wo_id"], "the export table gets a new column")
    clear = a_released_feature(started, store, "api")
    other = ops.create_work_order("proj_a", "unrelated", description="something else")

    claimed = [store.claim_next_pending(), store.claim_next_pending()]

    assert [c["id"] for c in claimed] == [store.feature_children(clear["id"], kinds=("worker",))[0]["id"],
                                          other["id"]]
    assert store.claim_next_pending() is None
    assert store.feature_children(held["id"], kinds=("worker",))[0]["status"] == "pending"


def test_the_planner_itself_is_never_held_by_its_own_assumptions(started, store):
    """Holding it would deadlock the feature for ever: the planner is dispatched before the plan exists"""
    _, planner, _ = a_bare_feature(store, planner_status="pending")

    assert store.claim_next_pending()["id"] == planner["id"]
    assert store.plan_hold(store.get_work_order(planner["id"])) is None


def test_a_running_child_is_untouched_by_a_new_plan_hold(started, store):
    """The hold is on STARTING and on LANDING"""
    _, planner, kid = a_bare_feature(store, status="running", assumptions=0)
    store.update_work_order(kid["id"], session_id="sess-1")
    store.add_assumption(planner["id"], "the export table gets a new column")

    started.dispatch_pending(started.catalog.project("proj_a"), store)

    row = store.get_work_order(kid["id"])
    assert row["status"] == "running" and row["session_id"] == "sess-1"
    assert store.queued_messages(kid["id"]) == []
    assert store.plan_hold(row) is not None


def test_plan_hold_and_the_claim_query_agree(started, store):
    """The guard on §2.1's one duplication: the claim filter cannot call `plan_hold` (it must stay inside..."""
    _, _, held_kid = a_bare_feature(store)
    _, _, clear_kid = a_bare_feature(store, assumptions=0)
    _, planner, _ = a_bare_feature(store, planner_status="pending")
    loose = ops.create_work_order("proj_a", "unrelated", description="something else")
    matrix = [("held child", held_kid), ("clear child", clear_kid),
              ("the planner", planner), ("no feature", loose)]

    for label, wo in matrix:
        for _, other in matrix:
            store.update_work_order(
                other["id"], status="pending" if other is wo else "completed")
        claimed = store.claim_next_pending()
        row = store.get_work_order(wo["id"])
        assert (store.plan_hold(row) is None) == (claimed is not None), label


def test_a_planner_with_no_pending_assumptions_holds_nothing(started, store):
    """NEGATIVE"""
    fo = a_released_feature(started, store, "schema")
    store.add_assumption(fo["plan_wo_id"], "the export table gets a new column")
    ops.review_work_order(fo["plan_wo_id"])

    kid = store.feature_children(fo["id"], kinds=("worker",))[0]
    assert store.plan_hold(store.get_work_order(kid["id"])) is None
    assert store.claim_next_pending()["id"] == kid["id"]


def test_a_work_order_with_no_feature_holds_nothing(started, store):
    """NEGATIVE"""
    a_bare_feature(store)
    loose = ops.create_work_order("proj_a", "an ordinary job")

    assert store.plan_hold(store.get_work_order(loose["id"])) is None
    assert store.claim_next_pending()["id"] == loose["id"]


def test_ruling_the_last_assumption_dispatches_on_the_next_tick(started, store):
    """§2.5: nothing pokes anything"""
    fo = a_released_feature(started, store, "schema")
    store.add_assumption(fo["plan_wo_id"], "the export table gets a new column")
    started.tick()
    assert store.feature_children(fo["id"], kinds=("worker",))[0]["status"] == "pending"

    ops.review_work_order(fo["plan_wo_id"])
    started.tick()

    assert store.feature_children(fo["id"], kinds=("worker",))[0]["status"] != "pending"


def test_a_held_child_raises_no_attention(started, store):
    """Waiting is not an attention item — the rule a dependency edge and a slot already follow"""
    from jarvis import invariants

    fo = a_released_feature(started, store, "schema")
    store.add_assumption(fo["plan_wo_id"], "the export table gets a new column")

    kid = store.get_work_order(store.feature_children(fo["id"], kinds=("worker",))[0]["id"])
    assert invariants.true_blockers(store, kid) == []
    assert [v for v in invariants.check_blocked_work_is_surfaced(store)
            if kid["id"] in str(v)] == []
    assert not kid["needs_attention"]


def test_the_hold_reads_as_blocked_by_the_plan(started, store):
    """Ranked above the dependency and slot labels: of the three true sentences it is the one naming a..."""
    from jarvis import invariants

    fo = a_released_feature(started, store, "schema")
    store.add_assumption(fo["plan_wo_id"], "the export table gets a new column")
    store.add_assumption(fo["plan_wo_id"], "the command takes --format")

    kid = store.get_work_order(store.feature_children(fo["id"], kinds=("worker",))[0]["id"])
    label = invariants.status_label(store, kid)

    assert label.startswith("pending — blocked by ")
    assert fo["id"] in label and "2 assumptions" in label
    assert f"jarvis wo review {fo['plan_wo_id']}" in label


# -- the note: children already merged (§2.6)


def test_an_overtaken_assumption_says_children_already_merged(started, store):
    """The fact the user needs in order to rule — never a ruling"""
    fo = a_released_feature(started, store, "schema", "api", "docs")
    planner_id = fo["plan_wo_id"]
    store.add_assumption(planner_id, "the export table gets a new column")
    merged = store.feature_children(fo["id"], kinds=("worker",))[0]
    store.add_event(merged["id"], "pr_merged", {})

    row = ops.assumptions_with_rulings(store, planner_id)[0]

    assert row["overtaken"] == {"merged": 1, "of": 3}
    assert ops.overtaken_line(row) == (
        "1 of 3 children have already merged — rejecting this now means a follow-up "
        "fix, not an unwind")
    assert "already merged" in ops.assumption_line(row)
    assert store.all_assumptions(planner_id)[0]["status"] == "pending"


def test_no_overtaken_note_when_no_child_has_merged(started, store):
    """NEGATIVE, both ways: nothing landed, and a work order that is not a planner"""
    fo = a_released_feature(started, store, "schema", "api")
    store.add_assumption(fo["plan_wo_id"], "the export table gets a new column")
    plain = ops.create_work_order("proj_a", "an ordinary job")
    store.add_assumption(plain["id"], "the exporter writes CSV")

    row = ops.assumptions_with_rulings(store, fo["plan_wo_id"])[0]
    assert row["overtaken"] is None
    assert ops.overtaken_line(row) == ""

    assert ops.assumptions_with_rulings(store, plain["id"])[0]["overtaken"] is None


# -- the spec the OS holds travels with the question (issue #746)


#: A revision visible in the OUTLINE the review now carries (Neo ruling Q1880): a changed sentence is...
REVISED = (FIXTURE_DESIGN_DOC_BODY.replace("Rows are dicts", "Rows are frozen records")
           + "\n## Frozen records\n\nNew in the revision.\n")


def git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    *args], check=True, capture_output=True)


def commit_spec(repo, text: str, message: str = "spec: revise") -> None:
    (repo / FIXTURE_DESIGN_DOC).write_text(text)
    git(repo, "add", "--", FIXTURE_DESIGN_DOC)
    git(repo, "commit", "-qm", message)


def planner_worktree(project, store, fo_id: str, branch: str = "feat/plan"):
    """The planner's worktree, as a dispatched planner would have created it"""
    planner = planner_of(store, fo_id)
    path = project / ".claude" / "worktrees" / (planner["worktree"] or planner["id"])
    git(project, "worktree", "add", "-q", "-b", branch, str(path))
    return path


def question(qid: int) -> dict:
    neo = NeoStore()
    try:
        return neo.get(qid)
    finally:
        neo.close()


def plan_questions() -> list[dict]:
    neo = NeoStore()
    try:
        return [dict(r) for r in neo.conn.execute(
            "SELECT * FROM questions WHERE kind='plan' ORDER BY id")]
    finally:
        neo.close()


def test_the_question_carries_the_spec_under_its_own_label(planning, store):
    """1"""
    _, fo = planning

    out = ops.submit_plan(fo["id"], a_plan(child("reader")))

    q = question(out["neo_question_id"])
    assert "## 3. Failure handling" in q["context"]
    assert "An empty result set writes the header" not in q["context"]
    assert "Do NOT open this path on disk" in q["context"]
    assert "Spec under review:" in neo_mod.build_question_prompt(q)


def test_a_spec_merged_after_submission_is_what_the_review_reads(planning, store,
                                                                 project):
    """2 (#746)"""
    daemon, fo = planning
    worktree = planner_worktree(project, store, fo["id"])
    out = ops.submit_plan(fo["id"], a_plan(child("reader")))
    old_qid = out["neo_question_id"]

    # The squash merge: the revision lands on the default branch and the planner's worktree is reclaimed
    commit_spec(project, REVISED)
    git(project, "worktree", "remove", "--force", str(worktree))

    daemon.tick()

    refreshed = store.get_feature_order(fo["id"])
    assert question(old_qid)["answer"] == f"SUPERSEDED by question {old_qid + 1}"
    assert refreshed["plan_question_id"] == old_qid + 1
    new = question(refreshed["plan_question_id"])
    assert "## Frozen records" in new["context"]
    assert re.search(r"as committed on main @ [0-9a-f]{7}", new["context"])
    assert json.loads(refreshed["plan"])["design_doc_content"] == REVISED


def test_an_unmerged_revision_is_read_from_the_branch_never_from_main(planning, store,
                                                                     project):
    """3"""
    daemon, fo = planning
    worktree = planner_worktree(project, store, fo["id"])
    ops.submit_plan(fo["id"], a_plan(child("reader")))

    commit_spec(worktree, REVISED)      # on the planner's branch only

    daemon.tick()

    new = question(store.get_feature_order(fo["id"])["plan_question_id"])
    assert "## Frozen records" in new["context"]      # the branch's copy
    assert "## Frozen records" in REVISED and "## Frozen records" \
        not in FIXTURE_DESIGN_DOC_BODY                # ...and main's older copy lacks it
    assert "as committed on branch feat/plan @ " in new["context"]


def test_an_uncommitted_edit_is_invisible(planning, store, project):
    """4"""
    daemon, fo = planning
    worktree = planner_worktree(project, store, fo["id"])
    out = ops.submit_plan(fo["id"], a_plan(child("reader")))

    (worktree / FIXTURE_DESIGN_DOC).write_text(REVISED)

    daemon.tick()

    assert store.get_feature_order(fo["id"])["plan_question_id"] == out["neo_question_id"]
    assert len(plan_questions()) == 1


def test_a_spec_with_no_committed_copy_is_refused_and_names_the_commit(planning, store,
                                                                      project):
    """5. §11: the refusal is the ruling applied at its source, with the fix named"""
    _, fo = planning
    doc = a_plan(child("reader"))
    doc["design_doc"] = "docs/superpowers/specs/uncommitted.md"
    (project / doc["design_doc"]).write_text(FIXTURE_DESIGN_DOC_BODY)

    with pytest.raises(ops.OpsError) as e:
        ops.submit_plan(fo["id"], doc)

    assert "no COMMITTED copy" in str(e.value)
    assert "git add docs/superpowers/specs/uncommitted.md && git commit" in str(e.value)
    assert store.get_feature_order(fo["id"])["status"] == "planning"


def test_an_untouched_spec_costs_no_neo_call_however_many_ticks(planning, store):
    """6"""
    daemon, fo = planning
    out = ops.submit_plan(fo["id"], a_plan(child("reader")))

    for _ in range(3):
        daemon.tick()

    assert len(plan_questions()) == 1
    assert store.get_feature_order(fo["id"])["plan_question_id"] == out["neo_question_id"]


def test_a_question_the_user_holds_is_told_about_the_revision_not_replaced(planning,
                                                                          store,
                                                                          project):
    """7"""
    daemon, fo = planning
    out = ops.submit_plan(fo["id"], a_plan(child("reader")))  # no FORCE_: it escalates
    daemon._neo_drain()
    assert question(out["neo_question_id"])["status"] == "escalated"

    commit_spec(project, REVISED)
    daemon.tick()

    after = store.get_feature_order(fo["id"])
    assert after["plan_question_id"] == out["neo_question_id"]
    assert json.loads(after["plan"])["design_doc_content"] == FIXTURE_DESIGN_DOC_BODY
    assert "the spec has been revised" in after["attention_reason"]


def test_a_verdict_in_flight_cannot_land_on_a_refreshed_plan(planning, store, project,
                                                             monkeypatch):
    """8"""
    daemon, fo = planning
    out = ops.submit_plan(fo["id"], a_plan(child("reader")))
    old_qid = out["neo_question_id"]
    neo = NeoStore()
    try:
        neo.mark(old_qid, "answering")
    finally:
        neo.close()

    commit_spec(project, REVISED)
    daemon.tick()
    assert store.get_feature_order(fo["id"])["plan_question_id"] != old_qid

    def boom(*a, **k):
        raise AssertionError("a verdict judged against superseded text was applied")

    monkeypatch.setattr(ops, "review_plan", boom)
    neo = NeoStore()
    try:
        daemon._deliver_plan_verdict(CentralStore(), neo, store, question(old_qid),
                                     {"escalate": False, "verdict": "approved",
                                      "reason": "judged against the old text"})
    finally:
        neo.close()

    assert store.get_feature_order(fo["id"])["status"] == "plan_review"


def test_nothing_readable_leaves_the_review_exactly_as_it_was(planning, store, project,
                                                              caplog):
    """9"""
    daemon, fo = planning
    out = ops.submit_plan(fo["id"], a_plan(child("reader")))
    git(project, "rm", "-q", "--", FIXTURE_DESIGN_DOC)
    git(project, "commit", "-qm", "spec: gone")

    with caplog.at_level(logging.WARNING, logger="jarvis.ops"):
        daemon.tick()

    after = store.get_feature_order(fo["id"])
    assert after["plan_question_id"] == out["neo_question_id"]
    assert after["status"] == "plan_review"
    assert json.loads(after["plan"])["design_doc_content"] == FIXTURE_DESIGN_DOC_BODY
    assert "no committed copy" in caplog.text


def test_a_revision_the_plan_no_longer_fits_goes_back_to_the_planner(planning, store,
                                                                     project):
    """10. §9 step 5: never refresh under a plan the new text no longer satisfies"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("reader")))
    section = fixture_spec_section("reader")
    broken = "\n".join(line for line in FIXTURE_DESIGN_DOC_BODY.splitlines()
                       if not line.startswith(f"## {section}."))

    commit_spec(project, broken)
    daemon.tick()

    after = store.get_feature_order(fo["id"])
    assert after["status"] == "planning"
    assert json.loads(after["plan"])["design_doc_content"] == FIXTURE_DESIGN_DOC_BODY
    reviewed = store.events_of_kind(fo["plan_wo_id"], "plan_reviewed")[-1]
    assert f"'{section}' matches no heading" in db.from_json(reviewed["payload"],
                                                             {})["reason"]


def test_the_question_carries_the_spec_outline_and_the_plan_the_whole_text(
        planning, store, project):
    """11, under Neo ruling Q1880: the question is an OUTLINE rendering; the children keep the whole text"""
    _, fo = planning
    big = FIXTURE_DESIGN_DOC_BODY + "\n".join(
        f"\n## Padding {n}\n\n" + "x" * 4000 for n in range(40))
    commit_spec(project, big)

    out = ops.submit_plan(fo["id"], a_plan(child("reader")))

    q = question(out["neo_question_id"])
    assert "## Padding 0" in q["context"] and "## Padding 39" in q["context"]
    assert "x" * 100 not in q["context"]       # every heading, no body prose
    assert len(q["context"]) < len(big)
    assert json.loads(store.get_feature_order(fo["id"])["plan"])["design_doc_content"] \
        == big


def test_a_refreshed_spec_reaches_the_children_and_the_feature_agent(planning, store,
                                                                     project):
    """12"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("reader", extra="FORCE_APPROVE")))
    revised = REVISED.replace("You are a Python engineer working on the exporter.",
                              "You are a Python engineer who writes frozen records.")
    commit_spec(project, revised)

    # `ops` directly, not `daemon.tick()`: a tick followed by a drain in the same test races on the...
    assert ops.refresh_plan_spec(fo["id"])["refreshed"]
    daemon._neo_drain()    # and then the release

    kid = store.feature_children(fo["id"], kinds=("worker",))[0]
    assert specs.spec_of(store, kid)["content"] == revised
    out = ops.rebuild_feature_agent(fo["id"])
    definition = Path(out["dir"]) / ".claude" / "agents" / f"{out['agent']}.md"
    assert "frozen records" in definition.read_text()


# -- a child's section is derivable, not written once


def _with_plan(store, *children: dict) -> tuple[dict, list[dict]]:
    """A released feature whose plan is written straight to the row, with its children"""
    fo = store.create_feature_order("CSV export", description=ASK)
    plan = a_plan(*children)
    plan["design_doc_content"] = FIXTURE_DESIGN_DOC_BODY
    store.update_feature_order(fo["id"], plan=db.to_json(plan))
    return fo, store.create_plan_children(fo["id"], list(children))


def test_a_child_with_no_recorded_section_resolves_it_from_the_plan_and_adopts_it(store):
    """§2.1"""
    _, (kid,) = _with_plan(store, child("reader"))
    store.update_work_order(kid["id"], spec_section=None)

    spec = specs.spec_of(store, store.get_work_order(kid["id"]))

    section = fixture_spec_section("reader")
    assert spec["section"] == section
    assert spec["section_text"]
    assert store.get_work_order(kid["id"])["spec_section"] == section


def test_the_heal_runs_once_and_the_fallback_is_not_re_entered(store, monkeypatch):
    """§2.1"""
    _, (kid,) = _with_plan(store, child("reader"))
    store.update_work_order(kid["id"], spec_section=None)
    specs.spec_of(store, store.get_work_order(kid["id"]))

    def never(*a, **k):
        raise AssertionError("the fallback ran against a healed row")

    monkeypatch.setattr(specs, "_plan_section_for_title", never)
    again = specs.spec_of(store, store.get_work_order(kid["id"]))

    assert again["section"] == fixture_spec_section("reader")


def test_a_recorded_section_that_disagrees_with_the_plan_is_never_overwritten(store):
    """§2.1, the user's ruling on question 1690: a corrected section is owned by whoever corrected it"""
    _, (kid,) = _with_plan(store, child("reader"))
    corrected = fixture_spec_section("by-hand")
    assert corrected != fixture_spec_section("reader")
    store.update_work_order(kid["id"], spec_section=corrected)

    spec = specs.spec_of(store, store.get_work_order(kid["id"]))

    assert spec["section"] == corrected
    assert store.get_work_order(kid["id"])["spec_section"] == corrected


def test_two_plan_children_sharing_one_title_resolve_to_nothing(store):
    """§2.1: the match must be UNIQUE"""
    twin = child("writer")
    twin["title"] = child("reader")["title"]
    _, kids = _with_plan(store, child("reader"), twin)
    for kid in kids:
        store.update_work_order(kid["id"], spec_section=None)

    spec = specs.spec_of(store, store.get_work_order(kids[0]["id"]))

    assert spec["section"] == "" and spec["section_text"] == ""
    assert not store.get_work_order(kids[0]["id"])["spec_section"]


def test_a_title_in_no_plan_item_resolves_to_nothing(store):
    """§2.1: absent resolves to nothing, never to a guess"""
    _, (kid,) = _with_plan(store, child("reader"))
    store.update_work_order(kid["id"], spec_section=None, title="Something else")

    spec = specs.spec_of(store, store.get_work_order(kid["id"]))

    assert spec["section"] == "" and spec["section_text"] == ""
    assert not store.get_work_order(kid["id"])["spec_section"]


def test_a_planner_and_a_manager_still_have_no_section_of_their_own(store):
    """§2.1: three readers depend on `spec_of` answering None for both"""
    fo, _ = _with_plan(store, child("reader"))
    planner = store.create_work_order("Plan CSV export", parent_id=fo["id"],
                                      kind="planner")
    manager = store.create_manager_order(fo["id"])

    assert specs.spec_of(store, planner) is None
    assert specs.spec_of(store, manager) is None


# -- the spec a person can open


def test_submitting_a_plan_records_which_revision_the_snapshot_came_from(planning,
                                                                         store):
    """§4"""
    _, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))

    plan = db.from_json(store.get_feature_order(fo["id"])["plan"], {})
    assert plan["design_doc_content"].startswith("# Exporter design")
    # `landing.committed_text`'s own words, verbatim
    assert re.match(r"(branch \S+|origin/\S+|\S+) @ [0-9a-f]{4,}",
                    plan["design_doc_source"]), plan["design_doc_source"]


def test_spec_link_points_a_planner_and_a_child_at_the_same_document(planning, store):
    """§5(a)"""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE")))
    released(daemon, store, fo["id"])

    planner = planner_of(store, fo["id"])
    kid = store.feature_children(fo["id"], kinds=("worker",))[0]

    assert specs.spec_of(store, planner) is None  # deliberately untouched
    link = specs.spec_link(store, planner)
    assert link is not None
    assert link["fo_id"] == fo["id"] and link["repo_path"] == FIXTURE_DESIGN_DOC
    assert link["anchor"] == ""  # a planner owns the whole feature, not a section

    child_link = specs.spec_link(store, kid)
    assert child_link is not None and child_link["anchor"]
    # A standalone work order has no plan and therefore no spec to point at
    ordinary = ops.create_work_order("proj_a", "an ordinary job")
    assert specs.spec_link(store, store.get_work_order(ordinary["id"])) is None


def test_the_cli_prints_one_section_and_not_the_whole_document(planning, store,
                                                              capsys):
    """Spec test 6"""
    from jarvis import cli

    _, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))

    assert cli.main(["fo", "spec", fo["id"], "--section", "3"]) == 0

    out = capsys.readouterr().out
    assert FIXTURE_DESIGN_DOC in out.splitlines()[0]  # the header line, with provenance
    assert "Failure handling" in out
    assert "union of keys" not in out  # §2 stays out


def test_the_cli_names_the_headings_when_a_section_does_not_resolve(planning, capsys):
    _, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))

    from jarvis import cli
    assert cli.main(["fo", "spec", fo["id"], "--section", "no such thing"]) == 1

    assert "Data model" in capsys.readouterr().err


def test_the_cli_refuses_a_feature_with_no_plan_and_names_its_state(planning, capsys):
    from jarvis import cli

    _, fo = planning
    assert cli.main(["fo", "spec", fo["id"]]) == 1

    assert "planning" in capsys.readouterr().err


def test_wo_show_carries_the_spec_link_for_a_planner(planning, store, capsys):
    """§6"""
    from jarvis import cli

    _, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))

    assert cli.main(["wo", "show", fo["plan_wo_id"], "--json"]) == 0

    detail = json.loads(capsys.readouterr().out)
    assert detail["spec"]["repo_path"] == FIXTURE_DESIGN_DOC
    assert detail["spec"]["fo_id"] == fo["id"]

    # And for a person: the document, the revision, and THE COMMAND
    assert cli.main(["wo", "show", fo["plan_wo_id"]]) == 0
    human = capsys.readouterr().out
    assert f"jarvis fo spec {fo['id']}" in human
    assert "http" not in human.split("spec")[-1]


def test_a_failed_flagged_feature_order_still_lists(started, store):
    """Obligation 6 of docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md"""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    quiet = ops.create_feature_order("proj_a", "another ask", description=ASK)
    store.set_feature_status(fo["id"], "failed")
    store.flag_feature_attention(fo["id"], "wo-1 failed")
    store.set_feature_status(quiet["id"], "completed")

    listed = [r["id"] for r in ops.list_feature_orders("proj_a")]
    assert fo["id"] in listed
    assert quiet["id"] not in listed
    assert quiet["id"] in [r["id"] for r in
                           ops.list_feature_orders("proj_a", include_settled=True)]


def test_the_github_link_is_omitted_rather_than_guessed(project):
    """§6"""
    from jarvis import github
    from jarvis.testing import with_origin

    # No origin: nothing to build an owner/repo from
    assert github.blob_url(project, FIXTURE_DESIGN_DOC) is None

    with_origin(project)
    url = github.blob_url(project, FIXTURE_DESIGN_DOC)
    assert url is not None
    assert url.startswith("https://github.com/acme/proj/blob/")
    # Built on the SHA, so it keeps showing what the page showed after main moves
    assert re.search(rf"/blob/[0-9a-f]{{40}}/{FIXTURE_DESIGN_DOC}$", url), url
    assert github.blob_url(project, "") is None
