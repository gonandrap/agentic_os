"""Feature orders: the planned unit of work above the work order.

Phase 2 of the feature-order design. The loop under test is
`jarvis fo create` -> the daemon opens ONE planner work order -> the planner submits a
validated plan -> Neo releases it or sends it back -> the children are created with real
dependency edges and the ordinary claim-time filter dispatches them in order.

Two properties this file is written to protect, because both are easy to lose in a
refactor and neither is visible from any single function:

* **Nothing about an ordinary work order changes.** The migration is additive and a
  project that never files a feature order must behave exactly as before. See
  `test_an_ordinary_work_order_is_untouched_by_any_of_this`.
* **A child work order is an ordinary work order.** No new scheduler, no new status, no
  new dispatch path — the parent is a row that owns them, and the edges between them are
  the Phase 1 `depends_on` edges.
"""

from __future__ import annotations

import json

import pytest

from jarvis import db, ops, plans
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


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
    }


def a_plan(*children: dict, **extra) -> dict:
    return {"summary": "an exporter", "children": list(children), **extra}


ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


@pytest.fixture()
def planning(started, store):
    """A feature order whose planner has been opened and dispatched."""
    daemon = started
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    return daemon, store.get_feature_order(fo["id"])


def planner_of(store, fo_id: str) -> dict:
    return store.get_work_order(store.get_feature_order(fo_id)["plan_wo_id"])


# -- creation ---------------------------------------------------------------------------


def test_a_feature_order_starts_pending_and_plans_nothing_by_itself(started, store):
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)

    assert fo["status"] == "pending"
    assert fo["plan_wo_id"] is None
    assert store.list_work_orders() == []


def test_a_feature_order_without_a_description_is_refused(started):
    """A work order can survive a bare title because a human reads it. A feature order's
    first reader is a planner in a fresh session, and it will decompose four words."""
    with pytest.raises(ops.OpsError, match="needs a description"):
        ops.create_feature_order("proj_a", "CSV export")


# -- the planner ------------------------------------------------------------------------


def test_the_daemon_opens_exactly_one_planner(planning, store):
    _, fo = planning

    assert fo["status"] == "planning"
    planner = planner_of(store, fo["id"])
    assert planner["kind"] == "planner"
    assert planner["parent_id"] == fo["id"]
    assert planner["status"] in ("dispatching", "running")
    # ONE child, not a fan-out. The daemon never decomposes anything itself.
    assert len(store.list_work_orders()) == 1


def test_the_planner_carries_the_ask_verbatim_and_a_planner_briefing(planning, store,
                                                                     fake_claude):
    _, fo = planning
    planner = planner_of(store, fo["id"])

    # On the record: what the user actually asked for, so `jarvis wo show` and
    # `jarvis fo show` read the same.
    assert planner["description"] == ASK
    # In the briefing: the planner contract, not the worker one.
    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"], count=1)[0]
    prompt = call["argv"][-1]
    assert "You are the PLANNER" in prompt
    assert f"jarvis fo plan {fo['id']} --from-file" in prompt
    assert "Do not build the feature" in prompt
    # The rule the whole plan lives or dies on has to be in there.
    assert "sees its own description and nothing else" in prompt
    # And it must NOT be told to finish the ordinary way, because `fo plan` is its finish.
    assert "Do not run `jarvis wo finish`" in prompt


def test_a_planner_is_not_created_twice(planning, store):
    """Idempotence is by status, not by a flag: the feature order leaves `pending` in
    the same call that files the planner."""
    daemon, fo = planning
    daemon.tick()
    daemon.tick()

    assert len([w for w in store.list_work_orders() if w["kind"] == "planner"]) == 1


def test_an_ordinary_work_order_is_untouched_by_any_of_this(started, store):
    """The migration is additive. A project that never files a feature order must not be
    able to tell that feature orders exist."""
    wo = ops.create_work_order("proj_a", "an ordinary job")

    row = store.get_work_order(wo["id"])
    assert row["parent_id"] is None
    assert row["kind"] == "worker"
    assert store.claim_next_pending()["id"] == wo["id"]


# -- plan submission --------------------------------------------------------------------


def test_submitting_a_plan_parks_it_for_review_and_settles_the_planner(planning, store):
    _, fo = planning

    out = ops.submit_plan(fo["id"], a_plan(child("reader"), child("writer")))

    assert out["status"] == "plan_review"
    assert store.get_feature_order(fo["id"])["status"] == "plan_review"
    # `jarvis fo plan` IS the planner's `jarvis wo finish` — nothing is left running.
    assert planner_of(store, fo["id"])["result_summary"]
    # Still nothing created: the plan is a proposal until it is released.
    assert [w for w in store.list_work_orders() if w["kind"] == "worker"] == []


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


# -- Neo's review -----------------------------------------------------------------------


def released(daemon, store, fo_id: str) -> dict:
    daemon._neo_drain()
    return store.get_feature_order(fo_id)


def test_neo_releasing_a_plan_creates_the_children_with_their_edges(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "executing"
    children = store.feature_children(fo["id"])
    assert [c["title"] for c in children] == ["Build schema", "Build api"]
    assert all(c["parent_id"] == fo["id"] and c["kind"] == "worker" for c in children)
    # The plan's local keys became real work-order ids on a real Phase 1 edge.
    assert store.dependencies(children[1]) == [children[0]["id"]]
    assert store.dependencies(children[0]) == []


def test_the_children_are_dispatched_in_dependency_order(planning, store):
    """No new scheduler: the claim-time filter Phase 1 shipped does all of it."""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    released(daemon, store, fo["id"])
    schema, api = store.feature_children(fo["id"])

    assert store.claim_next_pending()["id"] == schema["id"]
    assert store.claim_next_pending() is None  # `api` is blocked, and stays pending

    assert store.get_work_order(api["id"])["status"] == "pending"


def test_neo_sending_a_plan_back_returns_it_to_planning_with_the_reason(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_REJECT")))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "planning"
    assert store.feature_children(fo["id"]) == []
    # The reason reaches the planner as a message, so it revises in the SAME session
    # rather than starting cold.
    queued = store.list_messages(fo["plan_wo_id"])
    assert any("sent back" in m["content"] and "child two needs more context"
               in m["content"] for m in queued)


def test_a_rejected_plan_can_be_resubmitted_from_plan_review(planning, store):
    """The feature order is `planning` after a rejection, but a planner mid-revision may
    also still be sitting in `plan_review` — both have to accept a resubmission."""
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
    assert store.feature_children(fo["id"]) == []


def test_a_plan_at_the_cap_goes_to_the_user_even_when_neo_releases_it(planning, store):
    """The cap is one of the two backstops the Neo-reviews-plans default rests on, and a
    backstop the reviewer can wave through is not one. Neo is still ASKED — its reading
    is what makes a large graph reviewable — but it does not get to release it."""
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
    assert store.feature_children(fo["id"]) == []


def test_a_plan_under_the_cap_is_released_by_neo_alone(planning, store):
    """The control for the test above: the cap must not be so eager that the ordinary
    case still costs the user a decision — that is the whole cost this feature removes."""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(
        child("c0", extra="FORCE_APPROVE"),
        *[child(f"c{i}") for i in range(1, plans.CHILD_CAP - 1)],
    ))

    fo = released(daemon, store, fo["id"])

    assert fo["status"] == "executing"
    assert fo["needs_attention"] == 0


# -- the user's own decision --------------------------------------------------------------


def test_the_user_can_release_an_escalated_plan(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema"), child("api", needs=["schema"])))
    released(daemon, store, fo["id"])

    out = ops.review_plan(fo["id"], accept=True, feedback="looks right", decided_by="user")

    assert out["status"] == "executing"
    assert store.get_feature_order(fo["id"])["needs_attention"] == 0
    assert len(store.feature_children(fo["id"])) == 2


def test_the_user_rejecting_needs_a_reason(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))
    released(daemon, store, fo["id"])

    with pytest.raises(ops.OpsError, match="rejection needs feedback"):
        ops.review_plan(fo["id"], accept=False, decided_by="user")


def test_neos_verdict_is_dropped_if_the_user_got_there_first(planning, store):
    """Both deciders share one function, so the loser of the race has to be a no-op
    rather than a second decision applied on top of the first."""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE")))
    ops.review_plan(fo["id"], accept=False, feedback="wrong shape", decided_by="user")

    daemon._neo_drain()

    assert store.get_feature_order(fo["id"])["status"] == "planning"
    assert store.feature_children(fo["id"]) == []


# -- cancellation ---------------------------------------------------------------------------


def test_cancelling_a_feature_order_stops_everything_it_owns(planning, store):
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema", extra="FORCE_APPROVE"),
                                     child("api", needs=["schema"])))
    released(daemon, store, fo["id"])

    out = ops.cancel_feature_order(fo["id"])

    assert store.get_feature_order(fo["id"])["status"] == "cancelled"
    # A feature order that stopped while its children kept running would be a label.
    assert {c["status"] for c in store.feature_children(fo["id"])} == {"cancelled"}
    assert fo["plan_wo_id"] not in out["cancelled_work_orders"]  # already settled


def test_a_settled_feature_order_cannot_be_cancelled_again(planning, store):
    daemon, fo = planning
    ops.cancel_feature_order(fo["id"])

    with pytest.raises(ops.OpsError, match="already cancelled"):
        ops.cancel_feature_order(fo["id"])


# -- surfaces ---------------------------------------------------------------------------------


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
    # The derived label, so the tree does not claim a blocked child is about to start.
    assert api["status_label"].startswith("pending — blocked by")


def test_an_escalated_plan_is_one_attention_line_naming_the_feature_order(planning,
                                                                          store):
    """One line per feature order, never one per child: the strip that names everything
    is the strip that stops being read."""
    daemon, fo = planning
    ops.submit_plan(fo["id"], a_plan(child("schema")))
    released(daemon, store, fo["id"])

    status = ops.os_status()

    items = [a for a in status["attention"] if a.get("fo_id") == fo["id"]]
    assert len(items) == 1
    assert items[0]["decide"] == f"jarvis fo show {fo['id']}"
    # And NOT as a raw Neo escalation telling the user to `jarvis neo answer`.
    assert not [a for a in status["attention"] if a["status"] == "neo_escalated"]


def test_the_cli_round_trips_a_plan_through_a_file(planning, store, tmp_path, capsys):
    """`--from-file`, not an argument: a plan is a long string full of repo paths, which
    is exactly the input that trips the privileged-action classifier."""
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
    """A planner reads this message and nothing else, so it has to say what is wrong
    with the file rather than surfacing a traceback."""
    from jarvis import cli

    _, fo = planning
    path = tmp_path / "plan.json"
    path.write_text("not json")

    assert cli.main(["fo", "plan", fo["id"], "--from-file", str(path)]) == 1

    assert "not valid JSON" in capsys.readouterr().err
