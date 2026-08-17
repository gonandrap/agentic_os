"""A work order parked on a Neo question costs the user nothing until Neo hands it back.

GitHub issue 100. `jarvis wo ask` exists to keep a decision off the user's plate:
`ops.ask_question` parks the work order in `waiting_input` WITHOUT flagging attention,
and says so in its own docstring. Three separate places then decided, independently and
within a minute, that an idle worker in `waiting_input` meant "the user is being asked":

  * `invariants.true_blockers` — the flag came back on the next reconcile tick, and the
    self-check announced it had *repaired* an inconsistency while creating one;
  * the `Notification` branch of `hooks.handle_hook` — Claude Code's routine idle prompt,
    ~1 min after the turn ends, stamped "Claude is waiting for your input" over it;
  * `Daemon.settle_work_order` — a turn that ended with no queued message and no
    `jarvis wo finish` was filed as `needs_review — worker idle without …`.

Three of five sampled `jarvis_os` work orders had it, and the remedy the attention line
offered (`jarvis wo resume-auto`) could not help: `auto` is the fleet-wide default, so
the flip it performs is `auto → auto`, and all it really does is spend a conversation
re-send on a worker that was waiting correctly.

Every test here pins the SPECIFIC combination that shipped past the suite —
`waiting_input` PLUS an open question — because each of the three sites was already
tested, in isolation, against state that never had both.
"""

from __future__ import annotations

import pytest

from jarvis import neo as neo_mod
from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.hooks import handle_hook
from jarvis.invariants import (
    awaiting_neo,
    check_project,
    neo_question_blocker,
    status_label,
    true_blockers,
)
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

IDLE_NOTIFICATION = "Claude is waiting for your input"
GENERIC_BLOCKER = "worker is waiting on your input"


# -- fixtures -------------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """The OS running against the fixture catalog — `permission_mode` falls to `auto`,
    exactly as the production catalog does."""
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def park_on_a_question(store: ProjectStore, title: str = "build the exporter",
                       status: str = "queued", kind: str = "question") -> tuple[dict, dict]:
    """A work order in the state `jarvis wo ask` leaves behind: `waiting_input`, not
    flagged, with one open question in Neo's DB. Returns `(work order, question)`.

    Built through the two stores rather than through `ops.ask_question` so the state can
    be posed at any point of the question's lifecycle; `test_the_live_sequence_…` below
    walks the real path end to end.
    """
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


# -- 1. the derivation: invariants.true_blockers ---------------------------------------


@pytest.mark.parametrize("status", ["queued", "answering"])
def test_a_question_neo_still_holds_is_not_a_user_blocker(project, status):
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store, status=status)

    assert true_blockers(store, wo) == []


@pytest.mark.parametrize("status", ["escalated", "failed"])
def test_a_question_neo_hands_back_is_a_user_blocker(project, status):
    """The other half, and the half that must not regress in the fixing: Neo escalating
    IS Neo handing the decision over, and a question it could not answer at all has
    nobody else left to answer it."""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status=status)

    blockers = true_blockers(store, wo)

    assert blockers == [neo_question_blocker(q)]
    # …and it names the way through, rather than the generic line that used to send the
    # user at a session to type into.
    assert f"jarvis neo answer {q['id']}" in blockers[0]
    assert GENERIC_BLOCKER not in blockers


def test_an_answered_question_stops_suppressing_anything(project):
    """The suppression is scoped to the wait, not to the work order. Once Neo has
    answered, a work order still sitting in `waiting_input` is back to being whatever it
    was before — `Daemon._neo_drain` moves it on, and if that ever fails to happen the
    user must hear about it."""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store)
    neo = NeoStore()
    try:
        neo.record_answer(q["id"], "CSV")
    finally:
        neo.close()

    assert true_blockers(store, wo) == [GENERIC_BLOCKER]


def test_a_reply_already_queued_is_not_a_reason_to_ask_again(project):
    """The last few seconds of the same wait. `ops.send_message` clears the flag as the
    user writes; until the daemon delivers, `waiting_input` stands — and the invariant
    put the flag back within a tick, asking the user for something they had just done."""
    store = ProjectStore(project)
    wo = store.create_work_order("blocked task")
    store.set_status(wo["id"], "waiting_input")
    store.queue_message(wo["id"], "yes, go ahead", source="user")

    assert true_blockers(store, store.get_work_order(wo["id"])) == []


def test_a_gate_question_is_left_to_the_gate_machinery(project):
    """`kind='approval'` shadows a row in the project store's approvals table, which
    `true_blockers` already reads. Counting it here too would report one gate twice, in
    two vocabularies."""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store, kind="approval")

    assert awaiting_neo(wo["id"]) is None


def test_an_unreadable_neo_db_fails_towards_the_user(project, monkeypatch):
    """Cross-DB reads fail; the question is which way. A silent stall is the dangerous
    direction (`check_blocked_work_is_surfaced` exists for it), so an unreadable
    `neo.db` must degrade to the old, noisy behaviour rather than to silence."""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store)

    def boom(*a, **k):
        raise OSError("neo.db is not readable")

    monkeypatch.setattr("jarvis.neo_store.NeoStore.__init__", boom)

    assert awaiting_neo(wo["id"]) is None
    assert true_blockers(store, wo) == [GENERIC_BLOCKER]


# -- 2. the invariant that flagged it: INV-ATTENTION-MISSING ---------------------------


def test_the_self_check_no_longer_flags_a_work_order_parked_on_neo(project):
    """The observed production sequence, verbatim: worker asks, ~20s later the reconcile
    tick reports `INV-ATTENTION-MISSING` "repaired" and the user is paged."""
    store = ProjectStore(project)
    wo, _ = park_on_a_question(store)

    violations = check_project(store, repair=True)

    assert [v for v in violations if v.wo_id == wo["id"]] == []
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_the_self_check_still_surfaces_an_escalation(project):
    """The mirror. A question Neo sent up must be flagged even if nothing else flagged
    it — `reclaim_stale` marks a question `failed` with no delivery callback at all, so
    this invariant is the only thing that would ever raise it."""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="failed")

    violations = check_project(store, repair=True)

    assert any(v.invariant == "INV-ATTENTION-MISSING" and v.wo_id == wo["id"]
               for v in violations)
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"]
    assert fresh["attention_reason"] == neo_question_blocker(q)


def test_the_escalation_reason_survives_repeated_ticks(project):
    """kn-78346a2d: a reason `true_blockers` cannot re-derive is silently relabelled.
    The daemon and the derivation share one function precisely so this cannot drift."""
    store = ProjectStore(project)
    wo, q = park_on_a_question(store, status="escalated")

    for _ in range(3):
        check_project(store, repair=True)

    assert store.get_work_order(wo["id"])["attention_reason"] == neo_question_blocker(q)


# -- 3. the hook: Claude Code's idle prompt --------------------------------------------


@pytest.mark.parametrize("status", ["queued", "answering"])
def test_the_idle_prompt_does_not_page_the_user_while_neo_has_it(project, status):
    """`waiting_input` is in the branch's allowed statuses, so a parked worker sails
    past the settled-order guard and the idle prompt flags it a minute later."""
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
    """The kn-78346a2d failure in its original form: the generic idle message stamped
    over the one reason that says what to do."""
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
    """Same shape, the other wait: `gates.request` parks the work order in
    `waiting_input` too, and the worker was told to end its turn there."""
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


def test_a_real_permission_prompt_still_reaches_the_user(project):
    """The case the hook exists for. A `waiting_input` work order with nothing out —
    no question, no gate — is the one thing only the user can clear, and the guard must
    not swallow it."""
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


# -- 4. the reconciler: Daemon.settle_work_order ---------------------------------------


def test_settling_parks_a_worker_that_asked_neo(started, project, settle_turns):
    """A worker that ends its turn on `jarvis wo ask` has done exactly what the contract
    asks of it. Before this, the `else` branch caught it — `needs_review`, "worker idle
    without `jarvis wo finish`" — whenever Neo had not answered within the tick.

    `Daemon.settle_turns` directly rather than `tick`: a full tick also kicks the Neo
    drain onto its thread pool, which would answer the question out from under the
    assertion. The race is the test's, not the OS's, and losing it would make this pass
    for the wrong reason (kn-95a32178).
    """
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
    """The control: the same settlement pass, the same ended turn, no question. This
    must still reach the user, or the fix has bought silence rather than accuracy."""
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


# -- 5. the drain: what the answer does to the wait ------------------------------------


def test_answering_ends_the_wait(started, project):
    """Mirror of `gates.apply_decision`. The answer is out; what the work order waits on
    now is the OS delivering it, and `waiting_input` outliving that reads as a user
    blocker on every surface (this is what PR 99 fixed for gates)."""
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
    """Narrow on both sides, like the gate path: one answer does not end a wait another
    question is still holding, and the work order must not be reported as moving."""
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
    """`jarvis neo answer` cleared the attention flag and left `waiting_input` behind, so
    `true_blockers` derived the flag again on the next tick — the user answered and was
    asked again seconds later."""
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


# -- 6. what the user reads ------------------------------------------------------------


def test_the_listing_says_who_is_holding_it(project):
    """`waiting_input` renders as "Waiting on you" everywhere. For the whole time Neo
    holds the question that is false, and it can be minutes."""
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
    """The escalated question already carries its own line, with the text and the
    command. The work order's own flag saying the same thing less well is the noise the
    gate rollup was built to avoid."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    daemon._neo_drain()

    items = [a for a in ops.os_status()["attention"] if a.get("wo_id") == wo["id"]]

    assert len(items) == 1
    assert items[0]["status"] == "neo_escalated"
    assert items[0]["decide"].startswith("jarvis neo answer 1")
