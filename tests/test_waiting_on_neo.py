"""A work order parked on a Neo question costs the user nothing until Neo hands it back"""

from __future__ import annotations

import json

import pytest

from jarvis import gates
from jarvis import neo as neo_mod
from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.hooks import IDLE_PROMPT, IDLE_WHILE_WORKING, handle_hook
from jarvis.invariants import (
    IDLE_NO_FINISH_BLOCKER,
    awaiting_neo,
    check_project,
    neo_question_blocker,
    status_label,
    true_blockers,
)
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

IDLE_NOTIFICATION = "Claude is waiting for your input"
GENERIC_BLOCKER = "worker is waiting on your input"


def ignored_reason(store: ProjectStore, wo_id: str) -> str:
    """The reason the `Notification` branch recorded for swallowing the idle prompt"""
    ignored = [e for e in store.list_events(wo_id) if e["kind"] == "notification_ignored"]
    assert len(ignored) == 1, f"expected exactly one notification_ignored, got {ignored}"
    return json.loads(ignored[0]["payload"])["reason"]


# -- fixtures


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, live_daemon):
    """The OS running against the fixture catalog"""
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def park_on_a_question(store: ProjectStore, title: str = "build the exporter",
                       status: str = "queued", kind: str = "question") -> tuple[dict, dict]:
    """A work order in the state `jarvis wo ask` leaves behind: `waiting_input`"""
    wo = store.create_work_order(title)
    store.set_status(wo["id"], "waiting_input")
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", wo["id"], "CSV or JSON for the export default?", kind=kind)
        if status != "queued":
            neo.mark(q["id"], status)
            q = neo.get(q["id"]) or q
    finally:
        neo.close()
    return store.get_work_order(wo["id"]), q


# -- 1. the derivation: invariants.true_blockers


@pytest.mark.parametrize("status", ["queued", "answering"])
def test_a_question_neo_still_holds_is_not_a_user_blocker(project, status):
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store, status=status)

    assert true_blockers(store, wo) == []


@pytest.mark.parametrize("status", ["escalated", "failed"])
def test_a_question_neo_hands_back_is_a_user_blocker(project, status):
    """The other half, and the half that must not regress in the fixing: Neo escalating IS Neo handing the..."""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status=status)

    blockers = true_blockers(store, wo)

    assert blockers == [neo_question_blocker(q)]
    # …and it names the way through, rather than the generic line that used to send the user at a session...
    assert f"jarvis neo answer {q['id']}" in blockers[0]
    assert GENERIC_BLOCKER not in blockers


def test_an_answered_question_stops_suppressing_anything(project):
    """The suppression is scoped to the wait, not to the work order"""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store)
    neo = NeoStore()
    try:
        neo.record_answer(q["id"], "CSV")
    finally:
        neo.close()

    assert true_blockers(store, wo) == [GENERIC_BLOCKER]


def test_a_reply_already_queued_is_not_a_reason_to_ask_again(project):
    """The last few seconds of the same wait (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md §5b)"""
    store = ProjectStore(project)
    wo = store.create_work_order("blocked task")
    store.update_work_order(wo["id"], session_id="sess-1")
    turn = store.create_turn(wo["id"], "dispatch", "do the thing")
    store.finish_turn(turn["id"], "done", result="waiting on your reply")
    store.set_status(wo["id"], "waiting_input")
    store.queue_message(wo["id"], "yes, go ahead", source="user")
    # §5b: `[]` because `Daemon.deliver_messages` owns the row, not because nothing derived
    assert ops.scheduled_actor(store, store.get_work_order(wo["id"])).predicate == (
        "daemon.delivers_message")

    assert true_blockers(store, store.get_work_order(wo["id"])) == []


def test_a_gate_question_is_left_to_the_gate_machinery(project):
    """`kind='approval'` shadows a row in the project store's approvals table"""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store, kind="approval")

    assert awaiting_neo(wo["id"]) is None


def test_an_unreadable_neo_db_fails_towards_the_user(project, monkeypatch):
    """Cross-DB reads fail; the question is which way"""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store)

    def boom(*a, **k):
        raise OSError("neo.db is not readable")

    monkeypatch.setattr("jarvis.neo_store.NeoStore.__init__", boom)

    assert awaiting_neo(wo["id"]) is None
    assert true_blockers(store, wo) == [GENERIC_BLOCKER]


# -- 2. the invariant that flagged it: INV-ATTENTION-MISSING


def test_the_self_check_no_longer_flags_a_work_order_parked_on_neo(project):
    """The observed production sequence"""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store)

    violations = check_project(store, repair=True)

    assert [v for v in violations if v.wo_id == wo["id"]] == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_the_self_check_still_surfaces_an_escalation(project):
    """The mirror"""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="failed")

    violations = check_project(store, repair=True)

    assert any(v.invariant == "INV-ATTENTION-MISSING" and v.wo_id == wo["id"]
               for v in violations)
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"]
    assert fresh["attention_reason"] == neo_question_blocker(q)


def test_the_escalation_reason_survives_repeated_ticks(project):
    """kn-78346a2d: a reason `true_blockers` cannot re-derive is silently relabelled"""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="escalated")

    for _ in range(3):
        check_project(store, repair=True)

    assert store.get_work_order(wo["id"])["attention_reason"] == neo_question_blocker(q)


# -- 3. the hook: Claude Code's idle prompt


@pytest.mark.parametrize("status", ["queued", "answering"])
def test_the_idle_prompt_does_not_page_the_user_while_neo_has_it(project, status):
    """`waiting_input` is in the branch's allowed statuses"""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store, status=status)

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = store.get_work_order(wo["id"])
    assert not fresh["needs_attention"]
    assert fresh["status"] == "waiting_input"
    assert store.unrouted_notifications() == []
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "notification_ignored" in kinds


def test_the_idle_prompt_does_not_overwrite_an_escalation(project):
    """The kn-78346a2d failure in its original form: the generic idle message stamped over the one reason..."""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="escalated")
    store.flag_attention(wo["id"], neo_question_blocker(q))

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    assert store.get_work_order(wo["id"])["attention_reason"] == neo_question_blocker(q)


def test_the_idle_prompt_is_ignored_while_a_gate_is_with_neo(project):
    """Same shape, the other wait: `gates.request` parks the work order in `waiting_input` too"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")
    store.set_status(wo["id"], "waiting_input")
    store.add_approval(wo["id"], kind="release", command="scripts/shipit.sh",
                       matched="shipit", justification="ready", evidence="tests green")

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    assert not store.get_work_order(wo["id"])["needs_attention"]
    assert ignored_reason(store, wo["id"]) == \
        "idle prompt while parked on a privileged-action gate awaiting a verdict"


def test_the_idle_prompt_is_ignored_while_a_gate_awaits_the_worker_s_case(project):
    """GitHub issue 197, the fourth road in"""
    store = ProjectStore(project)
    wo = store.create_work_order("ship it")
    store.set_status(wo["id"], "running")
    action = gates.GatedAction(kind="release", summary="cut a release",
                               command="scripts/shipit.sh", matched="shipit")
    approval, question = gates.file_request(store, None, "proj",
                                            store.get_work_order(wo["id"]), action,
                                            hold=True)
    assert question is None and approval["status"] == gates.AWAITING_CASE
    # `running`: a held request parks nothing (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)
    assert store.get_work_order(wo["id"])["status"] == "running"

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = store.get_work_order(wo["id"])
    assert not fresh["needs_attention"]
    assert store.unrouted_notifications() == []
    # NOT interchangeable with the pending reason above
    assert ignored_reason(store, wo["id"]) == \
        "idle prompt while parked on a privileged-action gate awaiting the worker's case"


def test_a_real_permission_prompt_still_reaches_the_user(project):
    """The case the hook exists for"""
    store = ProjectStore(project)
    wo = store.create_work_order("real work")
    store.set_status(wo["id"], "running")

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": "Claude needs permission to run npm test"},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"] == 1
    assert "permission" in fresh["attention_reason"]


# -- 3b. the hook: the idle prompt on a session that is still working (issue 1166)


def working(store: ProjectStore, status: str = "running") -> dict:
    """A work order whose worker has a turn in flight — what `claude agents` calls busy"""
    wo = store.create_work_order("background-session transport")
    store.set_status(wo["id"], status)
    store.create_turn(wo["id"], "dispatch", "go")
    return store.get_work_order(wo["id"])


def test_the_shared_constant_is_the_message_claude_code_sends(project):
    """Both the hook and INV-WAIT-WORKING match this literal and nothing else"""
    assert IDLE_PROMPT == IDLE_NOTIFICATION


def test_the_idle_prompt_does_not_page_the_user_while_the_worker_works(project):
    """The bug: the idle timer fires mid-tool-call and the order is still running"""
    store = ProjectStore(project)
    wo = working(store)

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert not fresh["needs_attention"]
    assert store.unrouted_notifications() == []
    assert ignored_reason(store, wo["id"]) == IDLE_WHILE_WORKING


def test_a_permission_prompt_mid_turn_still_reaches_the_user(project):
    """The guard is on the message, not on the turn: a real prompt also arrives mid-turn"""
    store = ProjectStore(project)
    wo = working(store)

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": "Claude needs your permission to use Bash"},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"] == 1
    assert "permission" in fresh["attention_reason"]


def test_the_idle_prompt_still_parks_a_worker_whose_turn_has_ended(project):
    """The control: no turn in flight, so the prompt means what it says"""
    store = ProjectStore(project)
    wo = working(store)
    store.finish_turn(store.latest_turn(wo["id"])["id"], "done")

    handle_hook(
        {"hook_event_name": "Notification", "session_id": "s1",
         "cwd": str(project), "message": IDLE_NOTIFICATION},
        {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(project)},
    )

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["attention_reason"] == IDLE_NOTIFICATION


# -- 3c. the state the live bug left heals itself


def parked_by_the_idle_prompt(store: ProjectStore) -> dict:
    """What the hook used to leave: `waiting_input` + the idle flag, turn still running"""
    wo = working(store, status="waiting_input")
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)
    return store.get_work_order(wo["id"])


def test_the_invariant_ends_a_wait_the_worker_is_not_in(project):
    store = ProjectStore(project)
    wo = parked_by_the_idle_prompt(store)

    violations = check_project(store, repair=True)

    assert [v.invariant for v in violations] == ["INV-WAIT-WORKING"]
    assert violations[0].repaired
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert not fresh["needs_attention"]
    # INV-ATTENTION-MISSING does not put it back on the next pass
    assert check_project(store, repair=True) == []


def test_the_invariant_reports_without_repairing(project):
    store = ProjectStore(project)
    wo = parked_by_the_idle_prompt(store)

    violations = check_project(store, repair=False)

    assert [v.invariant for v in violations] == ["INV-WAIT-WORKING"]
    assert not violations[0].repaired
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["attention_reason"] == IDLE_NOTIFICATION


def test_the_invariant_leaves_a_real_permission_prompt_alone(project):
    """The exact literal, not a substring: the user is the only way out of a prompt"""
    store = ProjectStore(project)
    wo = working(store, status="waiting_input")
    store.flag_attention(wo["id"], "Claude needs your permission to use Bash")

    violations = check_project(store, repair=True)

    assert "INV-WAIT-WORKING" not in [v.invariant for v in violations]
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"] == 1


def test_the_invariant_leaves_a_wait_with_something_out_alone(project):
    """`end_wait_if_nothing_is_out` still owns the predicate: Neo holds this one"""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store)
    store.create_turn(wo["id"], "dispatch", "go")
    store.flag_attention(wo["id"], IDLE_NOTIFICATION)

    violations = check_project(store, repair=True)

    assert "INV-WAIT-WORKING" not in [v.invariant for v in violations]
    assert store.get_work_order(wo["id"])["status"] == "waiting_input"


# -- 4. the reconciler: Daemon.settle_work_order


def test_settling_parks_a_worker_that_asked_neo(started, project, settle_turns):
    """A worker that ends its turn on `jarvis wo ask` has done exactly what the contract asks of it"""
    daemon = started
    spec = daemon.catalog.project("proj_a")
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    store = ProjectStore(project)
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    assert store.get_work_order(wo["id"])["status"] == "waiting_input"
    assert settle_turns(store), "the worker's turn never ended"

    daemon.settle_turns(spec, store)  # nothing queued, no summary, Neo has not answered

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert not fresh["needs_attention"]
    assert ops.os_status()["attention"] == []


def test_settling_still_files_a_genuinely_idle_worker(started, project, settle_turns):
    """The control: the same settlement pass, the same ended turn, no question"""
    daemon = started
    spec = daemon.catalog.project("proj_a")
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    store = ProjectStore(project)
    assert settle_turns(store), "the worker's turn never ended"

    daemon.settle_turns(spec, store)

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert fresh["needs_attention"]
    assert fresh["attention_reason"] == IDLE_NO_FINISH_BLOCKER
    assert true_blockers(store, fresh) == [IDLE_NO_FINISH_BLOCKER]


# -- 5. the drain: what the answer does to the wait


def test_answering_ends_the_wait(started, project):
    """Mirror of `gates.apply_decision`"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")

    daemon._neo_drain()

    store = ProjectStore(project)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert not fresh["needs_attention"]
    assert true_blockers(store, fresh) == []
    msgs = store.queued_messages(wo["id"])
    assert len(msgs) == 1 and msgs[0]["content"].startswith(neo_mod.ANSWER_PREFIX)


def test_answering_one_of_two_questions_keeps_the_wait(started, project):
    """Narrow on both sides, like the gate path: one answer does not end a wait another question is still..."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    ops.ask_question(wo["id"], "And should it stream or buffer?")
    neo = NeoStore()
    try:
        neo.mark(2, "answering")  # claimed by a call still in flight
    finally:
        neo.close()

    daemon._neo_drain()  # answers question 1 only

    store = ProjectStore(project)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert not fresh["needs_attention"]
    assert true_blockers(store, fresh) == []


def test_the_users_answer_ends_the_wait_for_good(started, project):
    """`jarvis neo answer` cleared the attention flag and left `waiting_input` behind"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["needs_attention"]

    ops.neo_answer_escalated(1, "Yes, during the maintenance window")

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "running"
    assert not fresh["needs_attention"]
    assert true_blockers(store, fresh) == []
    check_project(store, repair=True)
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_escalation_keeps_the_work_order_parked_and_names_the_question(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")

    daemon._neo_drain()

    store = ProjectStore(project)
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "waiting_input"
    assert fresh["needs_attention"]
    neo = NeoStore()
    try:
        q = neo.get(1)
        assert q is not None
    finally:
        neo.close()
    assert fresh["attention_reason"] == neo_question_blocker(q)
    # and the reason is re-derivable, so the next reconcile tick leaves it alone
    assert true_blockers(store, fresh) == [neo_question_blocker(q)]


# -- 5b. what it is waiting on when the user owes a review too


def test_a_needs_review_park_outranks_the_workers_own_escalated_question(project):
    """`waiting_on` is ordered like `true_blockers`"""
    store = ProjectStore(project)
    wo, _q = park_on_a_question(store, status="escalated")
    store.set_status(wo["id"], "needs_review")

    answer = ops.waiting_on(store, store.get_work_order(wo["id"]))

    assert answer["what"] == "needs_review"


def test_a_question_escalated_on_an_unparked_order_still_reports_the_question(project):
    """The other half: the re-rank is scoped to the `needs_review` park"""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="escalated")

    answer = ops.waiting_on(store, store.get_work_order(wo["id"]))

    assert answer["what"] == "neo_escalated"
    assert answer["detail"] == neo_question_blocker(q)


# -- 6. what the user reads


def test_the_listing_says_who_is_holding_it(project):
    """`waiting_input` renders as "Waiting on you" everywhere"""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store)

    label = status_label(store, wo)

    assert label.startswith("waiting_input — ")
    assert f"with Neo" in label and str(q["id"]) in label


def test_the_listing_says_when_neo_has_handed_it_back(project):
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="escalated")

    assert f"handed question {q['id']} back to you" in status_label(store, wo)


def test_an_escalation_is_one_attention_line_not_two(started, project):
    """The escalated question already carries its own line, with the text and the command"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()

    items = [a for a in ops.os_status()["attention"] if a.get("wo_id") == wo["id"]]

    assert len(items) == 1
    assert items[0]["status"] == "neo_escalated"
    assert items[0]["decide"].startswith("jarvis neo answer 1")
