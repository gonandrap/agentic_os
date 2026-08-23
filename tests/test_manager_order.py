"""The project manager order: the third work-order kind, and what it breaks.

A feature order has no session, so a review that rejects one has nobody to tell. The
**project manager order** is that somebody: one `kind='manager'` work order created
alongside the children when a plan is released, which owns the feature's follow-through
and is the addressee for anything the feature needs a human-shaped decision about.

Two properties this file exists to protect, because neither is visible from any single
function:

* **With `os.validation.enabled` off, nothing changes.** No manager is created, plan
  release is the call it always was, and every other test in the suite still passes
  unedited. Every test below that needs a manager turns the flag on explicitly.
* **A long-lived idle session breaks four things, and the first one stops the fleet.**
  `count_active` has no kind filter, `waiting_input` is an ACTIVE status, and a manager
  is designed to sit in it for its feature's whole life — so two features in flight would
  spend a `max_concurrent: 2` project's entire budget on two sessions doing nothing.
  `test_an_idle_manager_does_not_spend_a_concurrency_slot` is the important test in this
  file; INV-MANAGER-SLOTS is the alarm that proves the exemption is still there.

Every test that can be is PAIRED: the manager's special case and the ordinary worker's
behaviour asserted side by side in one test, so a test that passes because the general
case broke cannot look like a test that passes because the special case works.
"""

from __future__ import annotations

import json

import pytest

from jarvis import bootstrap, bus, dispatch, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


def a_child(key: str) -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} half of the exporter: add the module, wire it into the "
            f"command that calls it, and cover both paths with tests in the existing "
            f"suite. Do not change the public interface of the caller."
        ),
        "needs": [],
    }


def a_plan(*keys: str) -> dict:
    return {"summary": "an exporter", "design_doc": FIXTURE_DESIGN_DOC,
            "children": [a_child(k) for k in keys]}


@pytest.fixture()
def boot(tmp_path, jarvis_home, fake_claude, project):
    """Start the OS against a catalog this test wrote, so `os.validation.enabled` and
    `max_concurrent` are part of the test rather than of a shared fixture.

    Callable more than once in a test: `start_os` re-registers the catalog path, and
    `ops.validation_enabled` reads it on demand, so a test can release one plan with the
    flag off and the next with it on and compare the two.
    """
    def _boot(validation: bool = True, max_concurrent: int = 4) -> Daemon:
        data = {
            "os": {
                "defaults": {"model": "sonnet", "max_concurrent": max_concurrent},
                "notifications": {"sinks": ["log"]},
                "validation": {"enabled": validation},
            },
            "projects": [
                {"name": "proj_a", "path": str(project), "description": "test project"},
            ],
        }
        path = tmp_path / f"catalog-{validation}-{max_concurrent}.json"
        path.write_text(json.dumps(data))
        ops.start_os(str(path), foreground=True)
        return Daemon(load_catalog(path))

    return _boot


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def release(daemon: Daemon, title: str, *keys: str) -> str:
    """A feature order whose plan has been submitted and released. Returns its id.

    The tick is what opens the planner and moves the feature to `planning`; the plan is
    then submitted by hand rather than by that planner, because what these tests are
    about is what RELEASE creates, not how the plan was written.
    """
    fo = ops.create_feature_order("proj_a", title, description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan(*keys))
    ops.review_plan(fo["id"], accept=True, decided_by="user")
    return str(fo["id"])


# -- 1. creation -----------------------------------------------------------------------


def test_a_released_plan_creates_a_manager_only_when_validation_is_enabled(boot, store):
    """The pair that pins the feature flag: same plan, same project, both ways round.

    The children must be IDENTICAL either way. A manager that changed what the user
    reviewed — an extra edge, a different order — would make the flag a behaviour change
    rather than an addition.
    """
    daemon = boot(validation=False)
    off = release(daemon, "CSV export (off)", "one", "two")

    assert store.manager_work_order(off) is None

    daemon = boot(validation=True)
    on = release(daemon, "CSV export (on)", "one", "two")
    manager = store.manager_work_order(on)

    assert manager is not None
    assert manager["kind"] == "manager"
    assert manager["parent_id"] == on
    assert manager["origin"] == "jarvis"
    assert manager["status"] == "pending"
    managers = [w for w in store.list_work_orders(limit=500, include_hidden=True)
                if w["kind"] == "manager"]
    assert len(managers) == 1, "exactly one manager, and only for the enabled feature"
    assert ([(c["title"], c["depends_on"]) for c in store.feature_children(off)]
            == [(c["title"], c["depends_on"]) for c in store.feature_children(on)])


def test_the_manager_is_created_in_the_same_transaction_as_the_children(boot, store,
                                                                        monkeypatch):
    """All-or-nothing, for the same reason the children are: a feature holding children
    but no manager is a feature whose rejections have nowhere to go."""
    daemon = boot(validation=True)
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], a_plan("one", "two"))

    def boom(self, fo_id):
        raise RuntimeError("the manager insert failed")

    monkeypatch.setattr(ProjectStore, "create_manager_order", boom)
    with pytest.raises(RuntimeError, match="the manager insert failed"):
        ops.review_plan(fo["id"], accept=True, decided_by="user")

    assert store.feature_children(fo["id"]) == [], "the children rolled back with it"
    assert store.manager_work_order(fo["id"]) is None
    assert store.get_feature_order(fo["id"])["status"] == "plan_review"


# -- 2. the concurrency trap -----------------------------------------------------------


def test_an_idle_manager_does_not_spend_a_concurrency_slot(boot, store):
    """THE test in this file. `count_active` counts every ACTIVE status and
    `waiting_input` is one of them, so two features' managers would eat a
    `max_concurrent: 2` project whole and `dispatch_pending` would never claim anything
    again — a project that stops with nothing anywhere saying why.

    Paired with the control in the same test: two ORDINARY work orders parked in exactly
    the same status DO take the slots. Without that half, a `count_active` that returned
    zero unconditionally would pass the first half perfectly.
    """
    daemon = boot(validation=True, max_concurrent=2)
    spec = daemon.catalog.project("proj_a")
    for name in ("one", "two"):
        fo = ops.create_feature_order("proj_a", f"feature {name}", description=ASK)
        manager = store.create_manager_order(fo["id"])
        store.set_status(manager["id"], "waiting_input")

    assert store.count_active() == 0, "two parked managers, and not one slot spent"

    work = ops.create_work_order("proj_a", "unrelated work", description="something")
    daemon.dispatch_pending(spec, store)

    assert store.get_work_order(work["id"])["status"] != "pending"

    # The control: the same status, the same count, on two ordinary workers.
    store.set_status(work["id"], "completed")
    for i in range(2):
        busy = ops.create_work_order("proj_a", f"busy {i}", description="something")
        store.set_status(busy["id"], "waiting_input")

    assert store.count_active() == 2

    blocked = ops.create_work_order("proj_a", "more work", description="something")
    daemon.dispatch_pending(spec, store)

    assert store.get_work_order(blocked["id"])["status"] == "pending"


def test_inv_manager_slots_fires_when_the_exemption_is_removed(boot, store, monkeypatch):
    """A canary that cannot be shown to fire is not a canary.

    Silent on healthy state WITH a manager present (the first half — a checker that
    fired on healthy state would spam the timeline every tick for ever), and loud the
    moment `count_active` goes back to counting everything.
    """
    daemon = boot(validation=True)
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    store.set_status(manager["id"], "waiting_input")

    def fired() -> list[invariants.Violation]:
        return [v for v in invariants.check_project(store, repair=False)
                if v.invariant == "INV-MANAGER-SLOTS"]

    assert fired() == [], "healthy state, with an idle manager, says nothing"

    from jarvis.project_store import ACTIVE_STATUSES

    def unfiltered(self) -> int:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        return self.conn.execute(
            f"SELECT COUNT(*) c FROM work_orders WHERE status IN ({marks})",
            ACTIVE_STATUSES,
        ).fetchone()["c"]

    monkeypatch.setattr(ProjectStore, "count_active", unfiltered)
    violations = fired()

    assert len(violations) == 1
    assert not violations[0].repaired, "a code regression is not derivable from state"
    assert "count_active" in violations[0].detail
    assert "wo-9652be2f" in violations[0].detail


# -- 3. the manager is not a child -----------------------------------------------------


def test_a_feature_completes_while_its_manager_is_open(boot, store):
    """`feature_children` filters to `kind='worker'`, so the manager cannot deadlock the
    completion — and because it cannot, nothing else would ever close it, which is why
    `settle_features` does it here."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    assert manager["id"] not in [c["id"] for c in store.feature_children(fo_id)]
    for c in store.feature_children(fo_id):
        store.set_status(c["id"], "completed")
    assert store.get_work_order(manager["id"])["status"] == "pending", "still open"

    daemon.settle_features(spec, store)

    assert store.get_feature_order(fo_id)["status"] == "completed"
    assert store.get_work_order(manager["id"])["status"] == "completed"
    assert not store.get_work_order(manager["id"])["needs_attention"]


def test_cancelling_the_manager_does_not_fail_the_feature(boot, store):
    """Paired with the child that DOES fail it: the difference is the kind, and it comes
    for free from `feature_children`'s positive filter rather than from a rule anyone
    has to remember."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None

    ops.cancel(manager["id"])
    daemon.settle_features(spec, store)

    assert store.get_feature_order(fo_id)["status"] == "executing"

    ops.cancel(store.feature_children(fo_id)[0]["id"])
    daemon.settle_features(spec, store)

    assert store.get_feature_order(fo_id)["status"] == "failed"


def test_a_failed_feature_closes_its_manager_too(boot, store):
    """Whichever way the feature ended, the manager ends with it. A live addressee under
    a settled feature would take delivery of messages about work that is over."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    store.set_status(store.feature_children(fo_id)[0]["id"], "failed")

    daemon.settle_features(spec, store)

    assert store.get_feature_order(fo_id)["status"] == "failed"
    assert store.get_work_order(manager["id"])["status"] == "completed"


def test_fo_cancel_cancels_the_manager(boot, store):
    """`jarvis fo cancel` reaches down to every session the feature owns. The manager is
    not a child, so — exactly like the planner — it has to be reached explicitly."""
    daemon = boot(validation=True)
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None

    out = ops.cancel_feature_order(fo_id)

    assert manager["id"] in out["cancelled_work_orders"]
    assert store.get_work_order(manager["id"])["status"] == "cancelled"


# -- 4. settlement ---------------------------------------------------------------------


def test_an_idle_manager_settles_to_waiting_input_without_attention(boot, store,
                                                                    settle_turns):
    """Idle by design on one side, idle by abandonment on the other, in one settlement
    pass over one feature. Without the manager branch every feature order in the fleet
    would carry a permanent false flag."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    child = store.feature_children(fo_id)[0]
    daemon.tick()
    assert settle_turns(store), "the turns never ended"

    daemon.settle_turns(spec, store)

    parked = store.get_work_order(manager["id"])
    assert parked["status"] == "waiting_input"
    assert not parked["needs_attention"]

    idle = store.get_work_order(child["id"])
    assert idle["status"] == "needs_review"
    assert idle["needs_attention"]


def test_an_idle_manager_stays_unflagged_across_a_reconcile_tick(boot, store,
                                                                 settle_turns):
    """Settling it quietly is only half the job: `true_blockers` derives
    "worker is waiting on your input" from the status alone, and INV-ATTENTION-MISSING
    repairs any unflagged blocked work order — so before the `kind='manager'` exemption
    the flag came straight back on the very next tick, which is where the user would
    actually have seen it.

    Paired in the same test with an ordinary worker parked in the SAME status, which must
    still be flagged: the exemption has to buy accuracy, not silence.
    """
    from jarvis.invariants import true_blockers

    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    daemon.tick()
    assert settle_turns(store), "the turns never ended"
    daemon.settle_turns(spec, store)
    assert store.get_work_order(manager["id"])["status"] == "waiting_input"

    parked = ops.create_work_order("proj_a", "an ordinary worker",
                                   description="something")
    store.set_status(parked["id"], "waiting_input")
    daemon.check_invariants(spec, store)  # what the reconcile tick runs

    fresh = store.get_work_order(manager["id"])
    assert not fresh["needs_attention"], "an idle manager is not a decision anyone owes"
    assert true_blockers(store, fresh) == []
    assert store.get_work_order(parked["id"])["needs_attention"], "the control"
    assert manager["id"] not in [a["wo_id"] for a in ops.os_status()["attention"]]


def test_a_manager_in_any_other_status_still_reaches_the_user(boot, store):
    """The exemption is on `waiting_input`, not on the kind. A manager that FAILED is a
    feature with no addressee left, which is exactly the thing the user has to know."""
    from jarvis.invariants import true_blockers

    daemon = boot(validation=True)
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    store.set_status(manager["id"], "failed")

    assert true_blockers(store, store.get_work_order(manager["id"])) != []


# -- 5. the contract -------------------------------------------------------------------


def test_a_manager_is_told_the_feature_and_not_the_worker_contract(boot, store):
    """It gets the feature's ask and its children, and none of the worker contract's
    instructions to produce a change — paired with the worker's prompt, which has them."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    child = store.feature_children(fo_id)[0]

    prompt = dispatch.build_worker_prompt(
        manager, spec, feature=dispatch.feature_context(store, manager))

    assert ASK in prompt, "the manager reasons about the feature, so it gets the ask"
    for c in store.feature_children(fo_id):
        assert c["id"] in prompt
    assert "will not open a pull request" in prompt
    assert "open a PR" not in prompt
    assert f"jarvis wo finish {manager['id']}" not in prompt
    assert f"--parent {fo_id}" in prompt, "the one way it can file remediation work"

    worker_prompt = dispatch.build_worker_prompt(child, spec)

    assert "open a PR" in worker_prompt
    assert f"jarvis wo finish {child['id']}" in worker_prompt


def test_a_manager_gets_the_worker_assets_and_not_the_planning_seats(boot, project):
    """`install_agent_assets` branches on `kind == "planner"`, so a manager falls to the
    worker path — which is what it should get. Pinned because it is a fall-through: the
    manager kind is nowhere in that function, and nothing else would notice if the branch
    became `kind != "worker"`."""
    boot(validation=True)

    manager_roots = bootstrap.install_agent_assets(project, "manager")
    worker_roots = bootstrap.install_agent_assets(project, "worker")
    planner_roots = bootstrap.install_agent_assets(project, "planner")

    assert manager_roots == worker_roots
    assert len(planner_roots) == len(worker_roots) + 1
    assert not any("agent-seats" in str(r) for r in manager_roots)


# -- 6. filing remediation work under the feature --------------------------------------


def test_a_work_order_can_be_filed_under_an_open_feature(boot, store):
    """What `--parent` buys, and it is the whole reason the manager's contract can be
    carried out: a child the feature waits for and shows, not a stray work order beside
    it. Paired with the same call without the flag, which is unchanged."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    for c in store.feature_children(fo_id):
        store.set_status(c["id"], "completed")

    remediation = ops.create_work_order(
        "proj_a", "cover the empty result set",
        description="the review asked for a test over an empty result set",
        parent_id=fo_id)
    stray = ops.create_work_order("proj_a", "unrelated", description="something else")

    assert remediation["parent_id"] == fo_id
    assert remediation["kind"] == "worker"
    assert stray["parent_id"] is None
    assert remediation["id"] in [c["id"] for c in store.feature_children(fo_id)]

    daemon.settle_features(spec, store)

    assert store.get_feature_order(fo_id)["status"] == "executing", \
        "the feature waits for the work its manager filed"


def test_a_settled_feature_refuses_new_children(boot, store):
    """Attaching a child to a completed feature would silently reopen a unit the user
    has already been told about, and leave `settle_features` deciding what that means."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    for c in store.feature_children(fo_id):
        store.set_status(c["id"], "completed")
    daemon.settle_features(spec, store)
    assert store.get_feature_order(fo_id)["status"] == "completed"

    with pytest.raises(ops.OpsError, match="nothing more can be filed"):
        ops.create_work_order("proj_a", "too late", description="x", parent_id=fo_id)


def test_the_cli_carries_the_parent_flag_through(boot, store, capsys):
    """The manager reaches this through the CLI, not through `ops`, so the wiring is
    pinned end to end — a flag parsed and dropped would leave the contract naming a
    command that silently does the wrong thing."""
    from jarvis import cli

    daemon = boot(validation=True)
    fo_id = release(daemon, "CSV export", "one")

    assert cli.main(["wo", "create", "proj_a", "cover the empty result set",
                     "-d", "the review asked for it", "--parent", fo_id]) == 0

    out = capsys.readouterr().out
    filed = store.feature_children(fo_id)[-1]
    assert filed["title"] == "cover the empty result set"
    assert filed["id"] in out and fo_id in out


def test_filing_under_an_unknown_feature_says_so(boot):
    """The CLI only catches OpsError, so a typo'd id must not surface as a KeyError
    traceback in the terminal."""
    boot(validation=True)

    with pytest.raises(ops.OpsError, match="no feature order"):
        ops.create_work_order("proj_a", "orphan", description="x", parent_id="fo-nope")


# -- 7. the bus finds it ---------------------------------------------------------------


def test_an_envelope_to_the_manager_role_is_delivered_to_the_manager(boot, store):
    """The first end-to-end proof that `bus.resolve` can find a real manager: nothing
    created one until now, so the `to_role='manager'` half of the routing table had only
    ever resolved to None.

    The envelope is about a CHILD work order, which is the shape the validation loop
    posts: the sender names a role and a subject and never learns who read it.
    """
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one", "two")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    child = store.feature_children(fo_id)[0]

    envelope_id = bus.post(
        store,
        subject=bus.Subject(wo_id=child["id"]),
        from_role="reviewer",
        to_role="manager",
        payload=bus.ReviewFeedback(
            round=1, outcome="rejected",
            reason="the tests do not exercise the change",
            asks=("cover the empty result set",)),
    )
    daemon.deliver_envelopes(spec, store)

    messages = store.queued_messages(manager["id"])
    assert len(messages) == 1
    assert "cover the empty result set" in messages[0]["content"]
    delivered = next(e for e in store.envelopes() if e["id"] == envelope_id)
    assert delivered["state"] == "delivered"
    assert delivered["delivered_wo_id"] == manager["id"]
