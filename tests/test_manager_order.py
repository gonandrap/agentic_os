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
* **A long-lived session breaks four things, and the first one stops the fleet.**
  `count_active` has no kind filter, and a manager takes a turn every time its feature
  reports for that feature's whole life — so two features in flight would spend a
  `max_concurrent: 2` project's entire budget on bookkeeping.
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
from jarvis.testing import FIXTURE_DESIGN_DOC, fixture_spec_section

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
        "spec_section": fixture_spec_section(key),
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
    def _boot(validation: bool = True, max_concurrent: int = 4,
              feature_units: bool = True) -> Daemon:
        data = {
            "os": {
                "defaults": {"model": "sonnet", "max_concurrent": max_concurrent},
                "notifications": {"sinks": ["log"]},
                "validation": {"enabled": validation,
                               "feature_units": feature_units},
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
    """THE test in this file. Two features' managers must not eat a `max_concurrent: 2`
    project whole and leave `dispatch_pending` unable to claim anything again — a project
    that stops with nothing anywhere saying why.

    Paired with the control in the same test: two ORDINARY work orders holding a slot DO
    take them. Without that half, a `count_active` that returned zero unconditionally
    would pass the first half perfectly. The control runs them, rather than parking them
    in the manager's own status: since issue #134 a parked order spends no slot whatever
    its kind, so `waiting_input` would no longer tell the two apart.
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

    # The control: two ordinary workers actually running, and the count sees them.
    store.set_status(work["id"], "completed")
    for i in range(2):
        busy = ops.create_work_order("proj_a", f"busy {i}", description="something")
        store.set_status(busy["id"], "running")

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
    # Mid-turn, because that is where the exemption still does work: since issue #134 a
    # manager parked in `waiting_input` is outside the cap's set anyway, so a canary
    # armed on a parked one would fire whether or not the kind filter survived.
    store.set_status(manager["id"], "running")

    def fired() -> list[invariants.Violation]:
        return [v for v in invariants.check_project(store, repair=False)
                if v.invariant == "INV-MANAGER-SLOTS"]

    assert fired() == [], "healthy state, with a manager taking its turn, says nothing"

    from jarvis.project_store import SLOT_STATUSES

    def unfiltered(self) -> int:
        marks = ",".join("?" for _ in SLOT_STATUSES)
        return self.conn.execute(
            f"SELECT COUNT(*) c FROM work_orders WHERE status IN ({marks})",
            SLOT_STATUSES,
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
    `settle_features` does it here.

    `feature_units=False` is what this test has always been asking for and could not say
    until the switch existed: a manager, and a feature that settles on its children. With
    feature validation on, the last child landing sends the feature to the panel instead,
    and the manager is closed by the pass — the same `_complete_feature`, one caller
    further along. `tests/test_feature_validation.py` covers that route.
    """
    daemon = boot(validation=True, feature_units=False)
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


def test_an_idle_manager_settles_to_idle_without_attention(boot, store,
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
    assert parked["status"] == "idle"
    assert not parked["needs_attention"]

    idle = store.get_work_order(child["id"])
    assert idle["status"] == "needs_review"
    assert idle["needs_attention"]


def test_an_idle_manager_stays_unflagged_across_a_reconcile_tick(boot, store,
                                                                 settle_turns):
    """Settling it quietly is only half the job: `true_blockers` derives
    "worker is waiting on your input" from the status alone, and INV-ATTENTION-MISSING
    repairs any unflagged blocked work order — so before the manager had a status of its
    own the flag came straight back on the very next tick, which is where the user would
    actually have seen it.

    Paired in the same test with an ordinary worker parked in `waiting_input`, which must
    still be flagged: `idle` has to buy accuracy, not silence.
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
    assert store.get_work_order(manager["id"])["status"] == "idle"

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
    """The silence is bought by the `idle` status, not by the kind. A manager that FAILED
    is a feature with no addressee left, which is exactly the thing the user has to
    know."""
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


def test_a_manager_gets_the_skills_and_neither_crew_nor_planning_seats(boot, project):
    """`install_agent_assets` now names all three kinds, and a manager is none of them: it
    gets the skills every session gets and no agent definitions at all. Pinned because it
    is still a fall-through — a manager coordinates, it does not write the code, so the
    worker crew (spec 2026-09-23-the-crew-a-worker-must-use.md §5) is not its to delegate
    to, and nothing else in that function would notice if the branch moved."""
    boot(validation=True)

    manager_roots = bootstrap.install_agent_assets(project, "manager")
    worker_roots = bootstrap.install_agent_assets(project, "worker")
    planner_roots = bootstrap.install_agent_assets(project, "planner")

    assert len(manager_roots) == 1, "skills only"
    assert len(worker_roots) == len(planner_roots) == 2
    assert not any("agent-seats" in str(r) or "agent-crew" in str(r)
                   for r in manager_roots)
    assert any("agent-crew" in str(r) for r in worker_roots)
    assert any("agent-seats" in str(r) for r in planner_roots)


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
    has already been told about, and leave `settle_features` deciding what that means.

    `feature_units=False` for the same reason as the completion test above: what is being
    pinned is the refusal on a SETTLED feature, and the shortest honest way to a settled
    one is the route that does not go through the panel.
    """
    daemon = boot(validation=True, feature_units=False)
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

    STOPS AT THE QUEUE, deliberately: what is under test here is `bus.resolve` finding
    the manager, and the manager is left `running` because no settle pass is run. So this
    proves nothing about waking one that is `idle` — see
    `test_an_envelope_wakes_an_idle_manager_and_it_settles_back_to_idle` in §7, which
    carries the same envelope the rest of the way through a real `daemon.tick`.
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


# -- 6. `idle`: the status that says so (GitHub issue #264) ----------------------------
#
# docs/superpowers/specs/2026-09-16-an-idle-manager-is-not-waiting-on-you.md. Suppressing
# the attention FLAG was never enough — six surfaces re-derive meaning from the status
# alone — so these tests are about what each of them now says, not about the flag.


def test_an_idle_manager_does_not_read_as_waiting_on_the_user(boot, store, settle_turns):
    """The eleven hours in the issue were spent reading "Waiting on you" off a status,
    with the flag already down. Paired with an ordinary `waiting_input` worker, which
    must still read that way: `idle` has to buy accuracy, not silence."""
    from jarvis.timeline import STATUS_LABEL
    from jarvis.ui.app import FEATURED_STATUSES, STATUS_META

    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    daemon.tick()
    assert settle_turns(store), "the turns never ended"
    daemon.settle_turns(spec, store)

    fresh = store.get_work_order(manager["id"])
    assert fresh["status"] == "idle"
    assert "Waiting on you" not in STATUS_LABEL["idle"]
    assert "waiting on you" not in STATUS_META["idle"]["word"]
    assert STATUS_META["idle"]["tone"] != "warn"
    assert "idle" not in FEATURED_STATUSES, (
        "the dashboard's needs-me strip is for decisions somebody owes")
    assert "waiting on you" not in invariants.status_label(store, fresh).lower()
    # The control, in the same test.
    assert STATUS_LABEL["waiting_input"] == "Waiting on you"


def test_resume_auto_names_the_idle_manager_instead_of_a_permission_prompt(
        boot, store, settle_turns):
    """`waiting_on`'s fall-through called it an unanswered permission prompt — impossible
    under `auto`, where nothing can prompt. And the nudge it offered bought one turn of
    the manager saying nothing was needed, which is the loop the issue measured at 0.37
    USD a lap: it must be refused, and no message may be queued."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    daemon.tick()
    assert settle_turns(store), "the turns never ended"
    daemon.settle_turns(spec, store)

    wait = ops.waiting_on(store, store.get_work_order(manager["id"]))
    assert wait["what"] == "manager_idle"
    assert not wait["stalled"]
    assert "permission prompt" not in wait["detail"]

    out = ops.resume_in_auto(manager["id"], project_name="proj_a")
    assert out["nudged"] is False
    assert out["waiting_on"] == "manager_idle"
    assert not store.queued_messages(manager["id"]), "the lap must not be re-run"
    assert "resume_auto_declined" in [e["kind"] for e in store.list_events(manager["id"])]


def test_a_manager_carried_over_in_waiting_input_is_migrated_on_the_first_tick(
        boot, store, settle_turns):
    """Every manager alive when this release lands is parked in `waiting_input`, some of
    them flagged. `settle_turns` is what migrates them, which is why `idle` is in its
    sweep — and it must take the flag down itself: INV-ATTENTION-PHANTOM clears only
    terminal rows, so a flag raised under the old status would outlive it for ever."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    daemon.tick()
    assert settle_turns(store), "the turns never ended"
    # Exactly where 0.10.3 left it.
    store.set_status(manager["id"], "waiting_input")
    store.flag_attention(manager["id"], "worker is waiting on your input")

    daemon.settle_turns(spec, store)

    fresh = store.get_work_order(manager["id"])
    assert fresh["status"] == "idle"
    assert not fresh["needs_attention"]


def test_a_manager_whose_question_neo_hands_back_still_reaches_the_user(boot, store):
    """The bug the old `kind != 'manager'` carve-out was HIDING. A manager reaches
    `waiting_input` only by asking, so the one case that exemption suppressed was the one
    that most needed the user: Neo giving the question back."""
    from jarvis.neo_store import NeoStore

    daemon = boot(validation=True)
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    store.set_status(manager["id"], "waiting_input")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", manager["id"], "Two children conflict — which wins?")
        neo.mark(q["id"], "escalated")
    finally:
        neo.close()

    blockers = invariants.true_blockers(store, store.get_work_order(manager["id"]))

    assert blockers and str(q["id"]) in blockers[0]


def test_a_relaunched_manager_settles_back_to_idle(boot, store, settle_turns,
                                                   fake_claude, monkeypatch):
    """`idle` is in RETRY_SWEEP_STATUSES because a manager's turn can be refused for the
    usage limit, and a sweep that skipped it would strand the one work order a feature
    routes everything through. The relaunch reads `running` while the turn is out — and
    lands back in `idle`, not in `needs_review`."""
    from jarvis import worker_session

    monkeypatch.setattr(worker_session, "RATE_LIMIT_MIN_DELAY", 0)
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    daemon.tick()
    assert settle_turns(store), "the turns never ended"
    daemon.settle_turns(spec, store)
    assert store.get_work_order(manager["id"])["status"] == "idle"
    turn = store.create_turn(manager["id"], kind="message", prompt="a child reported")
    store.finish_turn(turn["id"], "failed",
                      error="Claude AI usage limit reached|1000000000")

    daemon.retry_paused_turns(spec, store)
    assert store.get_work_order(manager["id"])["status"] == "running", (
        "a live turn must not read as 'nothing to act on'")
    assert settle_turns(store), "the relaunched turn never ran"
    daemon.settle_work_order(spec, store, store.get_work_order(manager["id"]))

    assert store.get_work_order(manager["id"])["status"] == "idle"


def test_a_message_rotting_on_an_idle_manager_still_reaches_the_user(project,
                                                                    monkeypatch):
    """The one blocker `idle` may still derive, and the reason it is in BLOCKED_STATUSES
    and MESSAGE_STUCK_STATUSES at all. A feature routes everything through its manager,
    so a message the manager will never see strands the feature silently — issue 43 one
    level up. It inherited this coverage from `waiting_input`; moving the status without
    moving the coverage would have dropped it."""
    from jarvis.invariants import MESSAGE_STUCK_BLOCKER, true_blockers

    store = ProjectStore(project)
    try:
        wo = store.create_work_order("manage the feature", kind="manager")
        store.set_status(wo["id"], "idle")  # no session_id: HOLD_NO_SESSION, unaccounted
        store.queue_message(wo["id"], "a child deferred something to you")
        # Aged in the ROW, because `stuck_message` reads the clock itself.
        store.conn.execute("UPDATE wo_messages SET ts=ts-? WHERE wo_id=?",
                           (90 * 60, wo["id"]))

        assert MESSAGE_STUCK_BLOCKER in true_blockers(store,
                                                      store.get_work_order(wo["id"]))
    finally:
        store.close()


# -- 7. what the migration must NOT touch ----------------------------------------------
#
# Review round 1. Since issue #264 the manager branch of `Daemon.settle_work_order` is a
# REWRITE (`waiting_input` -> `idle`) plus an unflag, where it used to be a no-op for a
# manager already parked. `waiting_input` is the only carrier of the fact that a manager
# ASKED, so every test below poses a manager that asked, drives a REAL settle pass, and
# asserts the status, the flag and the derived blocker all survive it. Hand-setting a
# status and calling `true_blockers` directly — which §6's Neo test does — cannot catch a
# settler that would have overwritten the status first, which is the point of these.


def _manager_with_a_finished_turn(boot, store, settle_turns):
    """A manager whose turn is DONE — the shape that reaches the branch under test."""
    daemon = boot(validation=True)
    spec = daemon.catalog.project("proj_a")
    fo_id = release(daemon, "CSV export", "one")
    manager = store.manager_work_order(fo_id)
    assert manager is not None
    daemon.tick()
    assert settle_turns(store), "the turns never ended"
    return daemon, spec, manager


def _settled_idle(boot, store, settle_turns):
    """A manager that has actually reached `idle` through the real settler."""
    daemon, spec, manager = _manager_with_a_finished_turn(boot, store, settle_turns)
    daemon.settle_turns(spec, store)
    assert store.get_work_order(manager["id"])["status"] == "idle", "the precondition"
    return daemon, spec, store.get_work_order(manager["id"])


def test_an_envelope_wakes_an_idle_manager_and_it_settles_back_to_idle(boot, store,
                                                                      settle_turns):
    """THE BEHAVIOUR THE MANAGER EXISTS FOR, end to end through a real `daemon.tick`.

    Review round 2, and the gap it names is real: §5's
    `test_an_envelope_to_the_manager_role_is_delivered_to_the_manager` stops at
    `queued_messages` and never runs a delivery pass, and its manager is never settled,
    so it is still `running` — it could not have caught a delivery path that keys off a
    status tuple holding `waiting_input` and not `idle`. Nothing else did either: every
    other test here poses a manager that is idle or that asked, and
    `test_a_relaunched_manager_settles_back_to_idle` hand-builds its turn with
    `store.create_turn`, which is the retry sweep's path and not delivery's.

    Adding `idle` to MESSAGE_STUCK_STATUSES lets the OS report this path FAILING. This is
    the other half: it succeeding. One tick routes the envelope AND delivers it — see the
    ordering comment in `Daemon.tick` — so the assertions below are about the real
    `deliver_envelopes` -> `deliver_messages` -> `worker_session.send` chain, with no
    status filter anywhere in it that a manager could fall out of.

    The FULL CYCLE is the point, not just the wake: idle -> message -> running -> idle. A
    manager that woke once and then stuck in `running` would be the same silent stranding
    one status further along.
    """
    daemon, spec, manager = _settled_idle(boot, store, settle_turns)
    before = store.latest_turn(manager["id"])["seq"]
    child = store.feature_children(manager["parent_id"])[0]

    bus.post(
        store,
        subject=bus.Subject(wo_id=child["id"]),
        from_role="reviewer",
        to_role="manager",
        payload=bus.ReviewFeedback(
            round=1, outcome="rejected",
            reason="the tests do not exercise the change",
            asks=("cover the empty result set",)),
    )
    daemon.tick()

    woken = store.get_work_order(manager["id"])
    assert store.latest_turn(manager["id"])["seq"] > before, \
        "no turn was created: the envelope reached an idle manager and died there"
    assert woken["status"] == "running", "a turn is out; the record must say so"
    assert not store.queued_messages(manager["id"]), "the message was consumed"

    assert settle_turns(store), "the woken turn never ran"
    daemon.settle_turns(spec, store)

    assert store.get_work_order(manager["id"])["status"] == "idle", \
        "it must go back to sleep, not stick in `running`"


def test_a_user_message_wakes_an_idle_manager_too(boot, store, settle_turns):
    """The other road in, and it is a different call: `ops.send_message` is what
    `jarvis wo send` and the dashboard's box use, where the envelope above is what the
    validation loop posts. Paired with it because they converge on `deliver_messages`
    only if nothing between them filters on status."""
    daemon, spec, manager = _settled_idle(boot, store, settle_turns)
    before = store.latest_turn(manager["id"])["seq"]

    ops.send_message(manager["id"], "the second child is blocked on the first",
                     project_name="proj_a")
    daemon.tick()

    assert store.latest_turn(manager["id"])["seq"] > before
    assert settle_turns(store), "the woken turn never ran"
    daemon.settle_turns(spec, store)

    assert store.get_work_order(manager["id"])["status"] == "idle"


def test_a_manager_parked_on_an_escalated_question_is_not_migrated(boot, store,
                                                                   settle_turns):
    """Neo handed the question BACK, so the user owes an answer. A settle pass must not
    relabel that "nothing to act on".

    `neo_store.OPEN_Q_STATUSES` includes `escalated`, so `awaiting_neo` still answers for
    it — asserted here rather than assumed, because that membership is the only thing
    that makes this case reachable by the guard at all.
    """
    from jarvis.neo_store import NeoStore

    daemon, spec, manager = _manager_with_a_finished_turn(boot, store, settle_turns)
    store.set_status(manager["id"], "waiting_input")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", manager["id"], "Two children conflict — which wins?")
        neo.mark(q["id"], "escalated")
    finally:
        neo.close()
    store.flag_attention(manager["id"], invariants.neo_question_blocker(
        {"id": q["id"], "status": "escalated"}))
    assert invariants.something_is_out(store, manager["id"])

    daemon.settle_turns(spec, store)

    fresh = store.get_work_order(manager["id"])
    assert fresh["status"] == "waiting_input", "it asked; the status must say so"
    assert fresh["needs_attention"]
    blockers = invariants.true_blockers(store, fresh)
    assert blockers and str(q["id"]) in blockers[0]


def test_a_manager_parked_on_a_held_gate_is_not_migrated(boot, store, settle_turns):
    """The case the branch order genuinely did NOT cover, and the reason the guard is a
    predicate rather than a fall-through.

    `gates.file_request` parks a work order in `waiting_input` down both its roads, but
    the `elif` above the manager branch reads `pending_approvals`, which excludes
    `awaiting_case` — a request the worker filed by running the command before arguing
    it. Under the old code missing it cost nothing, because this branch's write was
    `waiting_input` either way. `invariants.something_is_out` is the whole predicate.
    """
    daemon, spec, manager = _manager_with_a_finished_turn(boot, store, settle_turns)
    store.set_status(manager["id"], "waiting_input")
    store.add_approval(manager["id"], "pr_merge", "gh " + "pr merge 42",
                       status="awaiting_case")
    assert not store.pending_approvals(manager["id"]), "the premise: HELD, not pending"
    assert invariants.something_is_out(store, manager["id"])

    daemon.settle_turns(spec, store)

    assert store.get_work_order(manager["id"])["status"] == "waiting_input"


def test_a_manager_parked_on_a_sign_in_keeps_its_status_and_its_flag(boot, store,
                                                                     settle_turns,
                                                                     signin):
    """`_park_on_signin` raises AUTH_BLOCKER on a manager whose turn died on auth. The
    settler must leave both alone — and, since the `kind != 'manager'` carve-out went,
    `true_blockers` re-derives that blocker for a manager too, which it never did before.

    The protection is `settle_work_order`'s early return on a FAILED turn, one branch
    above everything §6 tests. Pinned here because nothing else pins it for a manager.
    """
    daemon, spec, manager = _manager_with_a_finished_turn(boot, store, settle_turns)
    turn = store.create_turn(manager["id"], kind="message", prompt="a child reported")
    row = store.finish_turn(
        turn["id"], "failed",
        error="Failed to authenticate: OAuth session expired and could not be refreshed")
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (row["started_at"] - 3600, row["ended_at"] - 3600, turn["id"]))

    daemon.settle_turns(spec, store)

    fresh = store.get_work_order(manager["id"])
    assert fresh["status"] == "waiting_input", "a sign-in is the user's to do"
    assert fresh["attention_reason"] == invariants.AUTH_BLOCKER
    assert invariants.true_blockers(store, fresh) == [invariants.AUTH_BLOCKER]


def test_the_migration_keeps_a_flag_that_survives_the_move(boot, store, settle_turns):
    """The unflag is narrowed, not unconditional: it re-derives `true_blockers` against
    the row AS IT NOW IS. A pending assumption is owed whatever the status, so it stays —
    paired with §6's carried-over case, where the only blocker was the status itself and
    the flag correctly goes down."""
    daemon, spec, manager = _manager_with_a_finished_turn(boot, store, settle_turns)
    store.set_status(manager["id"], "waiting_input")
    store.add_assumption(manager["id"], "filed the third child as its own work order")
    store.flag_attention(manager["id"], "1 assumption pending your review")

    daemon.settle_turns(spec, store)

    fresh = store.get_work_order(manager["id"])
    assert fresh["status"] == "idle", "nothing was OUT, so the migration still runs"
    assert fresh["needs_attention"], "a decision the user owes outlives the status move"
    assert fresh["attention_reason"] == "1 assumption pending your review"
