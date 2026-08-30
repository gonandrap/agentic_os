"""A question escalated to the user must stop asking once the decision is taken.

Reconstructed from three questions that were live in production when this was written,
none of them of kind `question` and all three stale for days:

* **130** — a plan escalated over the child cap. The planner revised and resubmitted
  from `plan_review`, `ops.submit_plan` moved `feature_orders.plan_question_id` to the
  new review, the user approved THAT one, and 130 went on asking. **67** was the same
  bug three days earlier on the same feature order.
* **118** — a `release` escalated to the user. The worker had decorated the approved
  command, so a duplicate request was filed; when it ran the real command
  `gates.open_gate` closed the duplicate approval and said nothing to Neo.

The shared root cause: a question row is only ever closed through the pointer its
SUBJECT holds, and both subjects can move that pointer or retire without closing what it
pointed at. So this file tests two things about every such site — that it closes the
question, and that closing writes a record naming what took the decision instead, which
is what was missing when the user asked "how was that escalation addressed?".
"""

from __future__ import annotations

import pytest

from jarvis import gates, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import check_project
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC, fixture_spec_section


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


def question(neo_store, question_id: int) -> dict:
    q = neo_store.get(question_id)
    assert q is not None, question_id
    return q


# -- plans: a resubmission moves the pointer ------------------------------------------

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


def child(key: str, extra: str = "") -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} half of the exporter: add the module, wire it into the "
            f"command that calls it, and cover both paths with tests in the existing "
            f"suite. Do not change the public interface of the caller. {extra}"
        ),
        "needs": [],
        "spec_section": fixture_spec_section(key),
    }


def a_plan(*children: dict) -> dict:
    markers = " ".join(sorted({m for c in children
                               for m in ("FORCE_APPROVE", "FORCE_REJECT")
                               if m in c["description"]}))
    return {"summary": f"an exporter {markers}".strip(),
            "design_doc": FIXTURE_DESIGN_DOC,
            "children": list(children)}


@pytest.fixture()
def planning(jarvis_home, fake_claude, catalog_file, project, store):
    """A feature order whose planner has been opened and dispatched."""
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    return daemon, store.get_feature_order(fo["id"])


@pytest.fixture()
def escalated_plan(planning, store):
    """Production 130's exact state: a plan review Neo handed back to the user."""
    daemon, fo = planning
    first = ops.submit_plan(fo["id"], a_plan(child("schema")))  # the fake Neo escalates
    daemon._neo_drain()
    return daemon, store.get_feature_order(fo["id"]), first["neo_question_id"]


def test_resubmitting_a_plan_closes_the_review_it_replaced(escalated_plan, store, neo):
    daemon, fo, stale = escalated_plan
    assert question(neo, stale)["status"] == "escalated"

    second = ops.submit_plan(fo["id"], a_plan(child("second")))

    closed = question(neo, stale)
    assert closed["status"] == "answered"
    assert str(second["neo_question_id"]) in closed["answer"]
    # The successor is the live one, and it is the only one left asking.
    assert question(neo, second["neo_question_id"])["status"] == "queued"


def test_a_superseded_review_records_who_closed_it_and_why(escalated_plan, store, neo):
    """The user's complaint was not that the row was open, it was that nothing said how
    the decision had been taken. `answered_by` must not read as a human ruling."""
    daemon, fo, stale = escalated_plan

    ops.submit_plan(fo["id"], a_plan(child("second")))

    closed = question(neo, stale)
    assert closed["answered_by"] == "os"
    assert "resubmitted" in closed["answer_reason"]
    # ...and it stays out of the review queue, which only judges Neo's own answers.
    assert neo.counts().get("unreviewed", 0) == 0


def test_cancelling_a_feature_order_closes_its_open_plan_review(escalated_plan, store,
                                                                neo):
    daemon, fo, stale = escalated_plan

    ops.cancel_feature_order(fo["id"])

    closed = question(neo, stale)
    assert closed["status"] == "answered"
    assert closed["answered_by"] == "os"
    assert fo["id"] in closed["answer_reason"]


def test_the_users_own_answer_is_never_overwritten(escalated_plan, store, neo):
    """Superseding must be a no-op on a decided row. The race is real: answering an
    escalation leaves the feature order in `plan_review`, which is a state a planner may
    resubmit from — so the closing write lands on a question the user has already ruled
    on, and without the guard it erases the ruling."""
    daemon, fo, stale = escalated_plan
    ops.neo_answer_escalated(stale, "eleven is fine, go")

    ops.submit_plan(fo["id"], a_plan(child("second")))

    decided = question(neo, stale)
    assert decided["answered_by"] == "user"
    assert decided["answer"] == "eleven is fine, go"


# -- gates: an approval closed by the OS ----------------------------------------------

DEPLOY = "./scripts/ship" "it.sh"           # split so this file cannot trip its own gate
STAGED = f"{DEPLOY} --stage 0.5.4"
WRAPPED = f"{STAGED} 2>&1 | tail -40"


@pytest.fixture()
def gated(jarvis_home, project, store):
    """A running work order in a project with every gate live."""
    from jarvis.hooks import preflight_decision

    all_gates = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))
    wo = store.create_work_order("ship the thing", description="cut a release")
    store.set_status(wo["id"], "running")
    env = {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT": "proj_a",
           "JARVIS_PROJECT_PATH": str(project), "JARVIS_GATES": all_gates.to_json()}

    def attempt(command):
        return preflight_decision(
            {"tool_name": "Bash", "tool_input": {"command": command},
             "cwd": str(project)}, env)

    return wo, attempt


@pytest.fixture()
def escalated_duplicate(gated, store, neo):
    """Production 118's exact state: the approved command has run, and a decorated
    duplicate of it is still escalated to the user."""
    wo, attempt = gated
    attempt(STAGED)
    approval = store.latest_approval_for(wo["id"], "release", STAGED)
    gates.apply_decision(store, approval["id"], verdict="approved", reason="ok",
                         decided_by="neo")
    attempt(WRAPPED)                                  # blocked; files the duplicate
    duplicate = store.latest_approval_for(wo["id"], "release", WRAPPED)
    store.mark_approval_escalated(duplicate["id"], "an unjustified real release")
    neo.mark(duplicate["neo_question_id"], "escalated", reason="the user must rule")
    return wo, attempt, approval, duplicate


def test_running_the_approved_command_closes_the_duplicates_escalation(
        escalated_duplicate, store, neo):
    wo, attempt, approval, duplicate = escalated_duplicate

    attempt(STAGED)                                   # the release actually happens here

    assert store.get_approval(duplicate["id"])["status"] == "expired"
    closed = question(neo, duplicate["neo_question_id"])
    assert closed["status"] == "answered"
    assert closed["answered_by"] == "os"
    assert str(approval["id"]) in closed["answer"]


def test_closing_that_escalation_records_no_authorisation(escalated_duplicate, store,
                                                          neo):
    """`gate_superseded` exists because none of the three verdicts is true here. The
    answer written on the question must not read as one either."""
    wo, attempt, approval, duplicate = escalated_duplicate

    attempt(STAGED)

    closed = question(neo, duplicate["neo_question_id"])
    assert closed["answered_by"] not in ("user", "neo")
    assert "APPROVED" not in closed["answer"]
    assert "nothing here authorises anything" in closed["answer_reason"]


def test_a_gate_still_pending_keeps_its_escalation(gated, store, neo):
    """The whole point of a gate: while the request is undecided the question is real."""
    wo, attempt = gated
    attempt(STAGED)
    approval = store.latest_approval_for(wo["id"], "release", STAGED)
    store.mark_approval_escalated(approval["id"], "no justification")
    neo.mark(approval["neo_question_id"], "escalated", reason="the user must rule")

    check_project(store, repair=True)

    assert question(neo, approval["neo_question_id"])["status"] == "escalated"


# -- the invariant: the backstop for a site that forgets -------------------------------


def test_an_orphaned_plan_review_is_closed_on_the_tick(escalated_plan, store, neo):
    """The fourth site. Nothing calls `supersede` here — the pointer is moved behind the
    OS's back, exactly as `ops.submit_plan` used to do it — and the tick still clears it."""
    daemon, fo, stale = escalated_plan
    later = neo.ask("proj_a", fo["id"], "a newer review", kind="plan")
    store.update_feature_order(fo["id"], plan_question_id=later["id"])

    violations = check_project(store, repair=True)

    stale_ones = [v for v in violations if v.invariant == "INV-NEO-ESCALATION-STALE"]
    assert [v.context["question_id"] for v in stale_ones] == [stale]
    assert stale_ones[0].repaired
    assert question(neo, stale)["status"] == "answered"
    assert str(later["id"]) in question(neo, stale)["answer"]


def test_a_decided_approval_leaves_no_escalation_behind(escalated_duplicate, store, neo):
    wo, attempt, approval, duplicate = escalated_duplicate
    store.supersede_approval(duplicate["id"], "closed without telling Neo")

    check_project(store, repair=True)

    closed = question(neo, duplicate["neo_question_id"])
    assert closed["status"] == "answered"
    assert "expired" in closed["answer_reason"]


def test_a_plan_review_still_awaiting_the_user_is_left_alone(escalated_plan, store, neo):
    daemon, fo, stale = escalated_plan

    violations = check_project(store, repair=True)

    assert [v for v in violations if v.invariant == "INV-NEO-ESCALATION-STALE"] == []
    assert question(neo, stale)["status"] == "escalated"


def test_reporting_mode_reports_the_stale_escalation_without_closing_it(escalated_plan,
                                                                       store, neo):
    """`jarvis doctor` without --repair. Neo's store is OS-wide, so the `_ReadOnly` proxy
    over the project store cannot intercept this write and the checker skips it itself."""
    daemon, fo, stale = escalated_plan
    later = neo.ask("proj_a", fo["id"], "a newer review", kind="plan")
    store.update_feature_order(fo["id"], plan_question_id=later["id"])

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations if v.context.get("question_id") == stale] \
        == ["INV-NEO-ESCALATION-STALE"]
    assert question(neo, stale)["status"] == "escalated"


def test_a_review_naming_its_feature_order_is_reported_unattached(planning, store, neo):
    """A plan question names the FEATURE order when its planner is gone, and the daemon
    writes a violation's `wo_id` onto the work order timeline — an FK the events table
    enforces. Reported without one rather than crashing the reporting loop."""
    daemon, fo = planning
    orphan = neo.ask("proj_a", fo["id"], "a review with no planner", kind="plan")
    neo.mark(orphan["id"], "escalated", reason="the user must rule")
    store.update_feature_order(fo["id"], plan_question_id=orphan["id"])
    store.set_feature_status(fo["id"], "cancelled")

    violations = [v for v in check_project(store, repair=True)
                  if v.context.get("question_id") == orphan["id"]]

    assert [v.wo_id for v in violations] == [None]
    daemon.check_invariants(daemon.catalog.projects[0], store)   # must not raise


def test_another_projects_escalation_is_not_this_projects_to_close(escalated_plan, store,
                                                                   neo):
    """The checks run per project against an OS-wide question store, so ownership has to
    be derived from whether this project knows the subject at all."""
    daemon, fo, stale = escalated_plan
    elsewhere = neo.ask("proj_b", "wo-somewhere", "a plan in another project",
                        kind="plan")
    neo.mark(elsewhere["id"], "escalated", reason="the user must rule")

    check_project(store, repair=True)

    assert question(neo, elsewhere["id"])["status"] == "escalated"


# -- the listing that showed them ------------------------------------------------------


def test_neo_list_stops_at_answers_only_neo_owes_a_review_for(escalated_plan, store, neo,
                                                              capsys):
    """`jarvis status` counts unreviewed answers with `NeoStore.counts`, which reads
    `answered_by='neo'`. The list under it did not, so every answer the user or the OS
    wrote stayed on it for ever — three of them in production, plus every question this
    change now closes."""
    daemon, fo, stale = escalated_plan
    second = ops.submit_plan(fo["id"], a_plan(child("second")))["neo_question_id"]
    assert question(neo, stale)["review_status"] == "unreviewed"

    from jarvis import cli

    cli.main(["neo", "list"])

    out = capsys.readouterr().out
    assert f"#{stale} " not in out
    assert f"#{second} " in out          # ...and the live one is still on the list
