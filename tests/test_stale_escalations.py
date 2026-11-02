"""A question escalated to the user must stop asking once the decision is taken"""

from __future__ import annotations

import json

import pytest

from jarvis import gates, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import check_project
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC, fixture_spec_section

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


def question(neo_store, question_id: int) -> dict:
    q = neo_store.get(question_id)
    assert q is not None, question_id
    return q


# -- plans: a resubmission moves the pointer

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
    """A feature order whose planner has been opened and dispatched"""
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    return daemon, store.get_feature_order(fo["id"])


@pytest.fixture()
def escalated_plan(planning, store):
    """Production 130's exact state: a plan review Neo handed back to the user"""
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
    # The successor is the live one, and it is the only one left asking
    assert question(neo, second["neo_question_id"])["status"] == "queued"


def test_a_superseded_review_records_who_closed_it_and_why(escalated_plan, store, neo):
    """The user's complaint was not that the row was open"""
    daemon, fo, stale = escalated_plan

    ops.submit_plan(fo["id"], a_plan(child("second")))

    closed = question(neo, stale)
    assert closed["answered_by"] == "os"
    assert "resubmitted" in closed["answer_reason"]
    # ...and it stays out of the review queue, which only judges Neo's own answers
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
    """Superseding must be a no-op on a decided row"""
    daemon, fo, stale = escalated_plan
    ops.neo_answer_escalated(stale, "eleven is fine, go")

    ops.submit_plan(fo["id"], a_plan(child("second")))

    decided = question(neo, stale)
    assert decided["answered_by"] == "user"
    assert decided["answer"] == "eleven is fine, go"


# -- gates: an approval closed by the OS

DEPLOY = "./scripts/ship" "it.sh"           # split so this file cannot trip its own gate
STAGED = f"{DEPLOY} --stage 0.5.4"
WRAPPED = f"{STAGED} 2>&1 | tail -40"


@pytest.fixture()
def gated(jarvis_home, project, store):
    """A running work order in a project with every gate live"""
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

    def argue(command, why="tests are green"):
        """Attempt it and then make its case — what puts a request in front of Neo"""
        attempt(command)
        action = gates.classify(command, all_gates)
        approval = store.latest_approval_for(wo["id"], action.kind, action.command)
        n = NeoStore()
        try:
            gates.amend_request(store, n, wo, action, approval, justification=why)
            gates.queue_for_review(store, n, "proj_a", wo, action,
                                   store.get_approval(approval["id"]))
        finally:
            n.close()
        return store.get_approval(approval["id"])

    return wo, attempt, argue


@pytest.fixture()
def escalated_duplicate(gated, store, neo):
    """Production 118's exact state: the approved command has run"""
    wo, attempt, argue = gated
    attempt(STAGED)
    approval = store.latest_approval_for(wo["id"], "release", STAGED)
    gates.apply_decision(store, approval["id"], verdict="approved", reason="ok",
                         decided_by="neo")
    duplicate = argue(WRAPPED)                        # blocked; the duplicate is argued
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
    """`gate_superseded` exists because none of the three verdicts is true here"""
    wo, attempt, approval, duplicate = escalated_duplicate

    attempt(STAGED)

    closed = question(neo, duplicate["neo_question_id"])
    assert closed["answered_by"] not in ("user", "neo")
    assert "APPROVED" not in closed["answer"]
    assert "nothing here authorises anything" in closed["answer_reason"]


def test_a_gate_still_pending_keeps_its_escalation(gated, store, neo):
    """The whole point of a gate: while the request is undecided the question is real"""
    wo, attempt, argue = gated
    approval = argue(STAGED)
    store.mark_approval_escalated(approval["id"], "not enough evidence")
    neo.mark(approval["neo_question_id"], "escalated", reason="the user must rule")

    check_project(store, repair=True)

    assert question(neo, approval["neo_question_id"])["status"] == "escalated"


# -- the invariant: the backstop for a site that forgets


def test_an_orphaned_plan_review_is_closed_on_the_tick(escalated_plan, store, neo):
    """The fourth site"""
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
    """`jarvis doctor` without --repair"""
    daemon, fo, stale = escalated_plan
    later = neo.ask("proj_a", fo["id"], "a newer review", kind="plan")
    store.update_feature_order(fo["id"], plan_question_id=later["id"])

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations if v.context.get("question_id") == stale] \
        == ["INV-NEO-ESCALATION-STALE"]
    assert question(neo, stale)["status"] == "escalated"


def test_a_review_naming_its_feature_order_is_reported_unattached(planning, store, neo):
    """A plan question names the FEATURE order when its planner is gone"""
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
    """The checks run per project against an OS-wide question store"""
    daemon, fo, stale = escalated_plan
    elsewhere = neo.ask("proj_b", "wo-somewhere", "a plan in another project",
                        kind="plan")
    neo.mark(elsewhere["id"], "escalated", reason="the user must rule")

    check_project(store, repair=True)

    assert question(neo, elsewhere["id"])["status"] == "escalated"


# -- alarms: the third pointer, and the third subject that can move it


@pytest.fixture()
def escalated_alarm(store, neo):
    """An alarm handed to Neo, with `wo_alarms.neo_question_id` pointing at the question"""
    wo = store.create_work_order(title="the long one", description="")
    alarm = store.add_alarm(wo["id"], "long-turn", 1, "still being billed")
    q = neo.ask("proj_a", wo["id"], "does this spend need the user?",
                context="the packet", kind="alarm")
    store.update_alarm(alarm["id"], status="escalated", verdict="escalate",
                       verdict_reason="cannot account for it", neo_question_id=q["id"])
    neo.mark(q["id"], "escalated", reason="the user must rule")
    return wo, alarm, q["id"]


def test_an_alarm_decided_elsewhere_stops_asking_on_the_tick(escalated_alarm, store, neo):
    """The site nothing else owns: an alarm moved off `escalated` by any route at all"""
    wo, alarm, stale = escalated_alarm
    store.update_alarm(alarm["id"], status="acked", verdict="ack", note="it is fine")

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert [v.context["question_id"] for v in violations] == [stale]
    assert violations[0].context["kind"] == "alarm"
    assert violations[0].repaired
    closed = question(neo, stale)
    assert closed["status"] == "answered"
    assert closed["answered_by"] == "os"
    assert alarm["id"] in closed["answer"]


def test_an_alarm_still_with_the_user_keeps_its_escalation(escalated_alarm, store, neo):
    """The other half of the cutoff"""
    wo, alarm, stale = escalated_alarm

    violations = check_project(store, repair=True)

    assert [v for v in violations if v.invariant == "INV-NEO-ESCALATION-STALE"] == []
    assert question(neo, stale)["status"] == "escalated"


# The point fix beside this net — `ops.review_alarm` superseding the question as the user decides the...


# -- the worker's own question: no pointer at all, only the timeline


def event_at(store: ProjectStore, wo_id: str, kind: str, ts: float,
             payload: dict | None = None) -> None:
    """One timeline row, placed on the clock — the store stamps `db.now()` otherwise"""
    store.add_event(wo_id, kind, payload or {})
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events WHERE wo_id=?)",
        (ts, wo_id))
    store.conn.commit()


@pytest.fixture()
def worker_question(store, neo):
    """`jarvis wo ask`, escalated"""
    wo = store.create_work_order(title="the exporter", description="")
    store.set_status(wo["id"], "needs_review")
    q = neo.ask("proj_a", wo["id"], "CSV or JSON for the export default?")
    neo.mark(q["id"], "escalated", reason="the user must rule")
    return store.get_work_order(wo["id"]), question(neo, q["id"])


def test_a_review_of_a_delivery_newer_than_the_question_retires_it(worker_question,
                                                                   store, neo):
    """wo-4beada49: the user settled the assumptions the question was about and nothing could retire it"""
    wo, q = worker_question
    event_at(store, wo["id"], "finished", q["ts"] + 10)
    event_at(store, wo["id"], "reviewed", q["ts"] + 20, {"accepted": True})

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert [v.context["question_id"] for v in violations] == [q["id"]]
    assert violations[0].context["kind"] == "question"
    closed = question(neo, q["id"])
    assert closed["status"] == "answered"
    assert closed["answered_by"] == "os"
    assert wo["id"] in closed["answer_reason"]


def test_a_finish_with_no_review_leaves_the_question_open(worker_question, store, neo):
    """Neo's ruling, and the half most likely to be lost: a finish is the WORKER's act"""
    wo, q = worker_question
    event_at(store, wo["id"], "finished", q["ts"] + 10)

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert violations == []
    assert question(neo, q["id"])["status"] == "escalated"


def test_a_review_of_a_delivery_older_than_the_question_leaves_it_open(worker_question,
                                                                       store, neo):
    """The `finished` BETWEEN them is what makes the verdict about a delivery newer than the question"""
    wo, q = worker_question
    event_at(store, wo["id"], "finished", q["ts"] - 20)
    event_at(store, wo["id"], "reviewed", q["ts"] + 20, {"accepted": True})

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert violations == []
    assert question(neo, q["id"])["status"] == "escalated"


def test_a_review_with_no_delivery_between_leaves_the_question_open(worker_question,
                                                                    store, neo):
    """The other boundary of the same predicate: a verdict with NO `finished` between it and the question..."""
    wo, q = worker_question
    event_at(store, wo["id"], "reviewed", q["ts"] + 20, {"accepted": True})

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert violations == []
    assert question(neo, q["id"])["status"] == "escalated"


def _gate_question(store, neo, status: str, text: str = "") -> tuple[dict, dict]:
    wo = store.create_work_order(title="the release", description="")
    gate = store.add_approval(wo["id"], "release", "./scripts/shipit.sh")
    if status != "pending":
        store.decide_approval(gate["id"], verdict=status, reason="ok", decided_by="neo")
    store.conn.commit()
    q = neo.ask("proj_a", wo["id"],
                text or f"[Gate {gate['id']}] Approve release gate {gate['id']}?")
    neo.mark(q["id"], "escalated", reason="the user must rule")
    return gate, question(neo, q["id"])


def test_a_question_asking_for_a_verdict_on_a_decided_gate_settles(store, neo):
    """Issue 1388: the worker asked the user to approve a gate Neo had already approved"""
    gate, q = _gate_question(store, neo, "approved")

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert [v.context["question_id"] for v in violations] == [q["id"]]
    closed = question(neo, q["id"])
    assert (closed["status"], closed["answered_by"]) == ("answered", "os")
    assert f"gate {gate['id']} is already approved by neo" in closed["answer"]
    # Neo's ruling: a text match can be wrong, so it is on the record
    [event] = store.events_of_kind(q["wo_id"], "question_superseded_by_gate")
    assert json.loads(event["payload"])["approval_id"] == gate["id"]


@pytest.mark.parametrize("status,text", [
    ("pending", ""),
    # Names the gate but asks for no verdict
    ("approved", "Gate {n} went through; should the changelog list the fix?"),
])
def test_a_gate_question_that_is_still_open_or_asks_no_verdict_stays(store, neo,
                                                                     status, text):
    wo = store.create_work_order(title="probe", description="")
    n = store.add_approval(wo["id"], "release", "x")["id"] + 1
    _, q = _gate_question(store, neo, status, text.format(n=n))

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert violations == []
    assert question(neo, q["id"])["status"] == "escalated"


def test_another_projects_worker_question_is_left_alone(store, neo):
    """The rule all the siblings share: the checks run per project against an OS-wide `neo.db`"""
    stranger = neo.ask("proj_b", "wo-elsewhere", "CSV or JSON?")
    neo.mark(stranger["id"], "escalated", reason="theirs to decide")

    violations = [v for v in check_project(store, repair=True)
                  if v.invariant == "INV-NEO-ESCALATION-STALE"]

    assert violations == []
    assert question(neo, stranger["id"])["status"] == "escalated"


# -- the listing that showed them


def test_neo_list_stops_at_answers_only_neo_owes_a_review_for(escalated_plan, store, neo,
                                                              capsys):
    """`jarvis status` counts unreviewed answers with `NeoStore.counts`, which reads `answered_by='neo'`"""
    daemon, fo, stale = escalated_plan
    second = ops.submit_plan(fo["id"], a_plan(child("second")))["neo_question_id"]
    assert question(neo, stale)["review_status"] == "unreviewed"

    from jarvis import cli

    cli.main(["neo", "list"])

    out = capsys.readouterr().out
    assert f"#{stale} " not in out
    assert f"#{second} " in out          # ...and the live one is still on the list
