"""A model or delivery failure never becomes an answer.

Spec: docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md.

Every rescue path in the OS is driven through `jarvis.testing`'s fault list, asserting
the same six things at each site:

  * nothing was decided — no answer, verdict, vote, approval or rejection recorded;
  * nothing was delivered downstream;
  * the unit of work is still pending and still retryable, with `attempts` incremented;
  * a later successful call answers it properly, and the result is the REAL answer;
  * the user is asked for nothing until the retries are genuinely exhausted;
  * and at exhaustion the surfaced item says it was UNREACHABLE, not that it was
    escalated.
"""

from __future__ import annotations

import time

import pytest

from jarvis import neo as neo_mod
from jarvis import ops, panel, supervisor
from jarvis import testing as T
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import MAX_ANSWER_ATTEMPTS, UNREACHABLE_PREFIX, NeoStore
from jarvis.project_store import MAX_DISPATCH_ATTEMPTS, ProjectStore
from jarvis.seats import Opinion


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    assert ops.start_os(str(catalog_file), foreground=True)["daemon"]["status"] \
        == "foreground"
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def asked(started, project):
    """A dispatched work order with one question queued for Neo."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    return daemon, wo


def _q(qid: int = 1) -> dict:
    neo = NeoStore()
    try:
        return neo.get(qid)
    finally:
        neo.close()


def _unhold(qid: int = 1) -> None:
    """Make a backed-off question claimable, so a test does not wait out the ladder."""
    neo = NeoStore()
    try:
        neo.clear_retry_hold(qid)
    finally:
        neo.close()


# -- §2: Neo's queue, the case that produced the ruling ----------------------------


def test_question_388_stays_queued_and_delivers_nothing(asked):
    """THE REGRESSION, reproduced exactly.

    Production question 388: `claude -p` exited 1 with zero tokens, and the drain loop
    wrote `status='failed'` at `attempts=0` and handed a synthesised `{escalate: True}`
    to the deliver hook. The user was shown an escalation Neo had never made.
    """
    deliver, unreachable = T.Recorder(), T.Recorder()
    store = NeoStore()
    try:
        results = neo_mod.drain_queue(
            store, model="sonnet", deliver=deliver, unreachable=unreachable,
            answer=T.FaultyAnswerer(T.RC1_EMPTY))
    finally:
        store.close()

    assert not deliver.calls, "a crash was delivered downstream as a verdict"
    assert not unreachable.calls, "the user was told at attempt zero, before any retry"
    assert results[0]["verdict"] is None
    assert results[0]["outcome"] == "queued"

    q = _q()
    assert q["status"] == "queued", "the question left Neo's queue on a transport fault"
    assert q["attempts"] == 1, "the retry machinery was bypassed rather than spent"
    assert q["answer"] is None
    assert q["claimed_at"] is None
    assert q["answer_reason"].startswith(UNREACHABLE_PREFIX)


@pytest.mark.parametrize("fault", T.TRANSPORT_FAULTS)
def test_every_transport_fault_leaves_the_question_retryable(asked, fault):
    deliver, unreachable = T.Recorder(), T.Recorder()
    store = NeoStore()
    try:
        neo_mod.drain_queue(store, model="sonnet", deliver=deliver,
                            unreachable=unreachable,
                            answer=T.FaultyAnswerer(fault))
    finally:
        store.close()
    assert not deliver.calls and not unreachable.calls
    q = _q()
    assert (q["status"], q["attempts"], q["answer"]) == ("queued", 1, None)


@pytest.mark.parametrize("fault", T.TRANSPORT_FAULTS)
def test_a_later_successful_call_gives_the_real_answer(asked, fault):
    """Retryable is only meaningful if the retry lands the true answer."""
    answerer = T.FaultyAnswerer(fault, succeed_after=1)
    deliver = T.Recorder()
    store = NeoStore()
    try:
        neo_mod.drain_queue(store, model="sonnet", deliver=deliver, answer=answerer)
        assert _q()["status"] == "queued"
        _unhold()
        neo_mod.drain_queue(store, model="sonnet", deliver=deliver, answer=answerer)
    finally:
        store.close()

    q = _q()
    assert q["status"] == "answered"
    assert q["answer"] == "the real answer"
    assert q["answered_by"] == "neo"
    assert len(deliver) == 1, "the answer was delivered once, and the failure never was"


def test_the_user_is_only_asked_once_the_retries_are_spent(asked):
    """Attempt by attempt to the ceiling: silent, silent, silent, then unreachable."""
    deliver, unreachable = T.Recorder(), T.Recorder()
    answerer = T.FaultyAnswerer(T.RC1_EMPTY)
    store = NeoStore()
    try:
        for attempt in range(1, MAX_ANSWER_ATTEMPTS + 1):
            _unhold()
            neo_mod.drain_queue(store, model="sonnet", deliver=deliver,
                                unreachable=unreachable, answer=answerer)
            assert _q()["status"] == "queued", f"gave up on attempt {attempt}"
            assert _q()["attempts"] == attempt
            assert not unreachable.calls, f"the user was asked on attempt {attempt}"
        # One more: the ceiling is reached and only now does it become the user's.
        _unhold()
        neo_mod.drain_queue(store, model="sonnet", deliver=deliver,
                            unreachable=unreachable, answer=answerer)
    finally:
        store.close()

    assert not deliver.calls, "a verdict was delivered on the way to exhaustion"
    assert len(unreachable) == 1
    q = _q()
    assert q["status"] == "failed"
    assert q["answer"] is None, "an unreachable question must carry no answer"
    assert "nobody has judged this" in q["answer_reason"]


def test_a_usage_limit_waits_out_the_window_and_spends_no_attempt(asked):
    """A refusal that states when it lifts is not a fault to ration (issue #235)."""
    reopens = time.time() + 3600
    deliver, unreachable = T.Recorder(), T.Recorder()
    store = NeoStore()
    try:
        results = neo_mod.drain_queue(
            store, model="sonnet", deliver=deliver, unreachable=unreachable,
            answer=T.FaultyAnswerer(T.USAGE_LIMIT, reset_at=reopens))
    finally:
        store.close()

    assert not deliver.calls and not unreachable.calls
    assert results[0]["outcome"] == "held"
    q = _q()
    assert q["status"] == "queued"
    assert q["attempts"] == 0, "a spent window burned one of the transport's retries"
    assert q["retry_after"] == pytest.approx(reopens)


def test_a_held_question_is_not_claimed_before_it_is_due(asked):
    """The backoff is what makes a retry a retry rather than the same failure thrice."""
    store = NeoStore()
    try:
        neo_mod.drain_queue(store, model="sonnet",
                            answer=T.FaultyAnswerer(T.RC1_EMPTY))
        assert store.claim_next() is None, "re-claimed inside its own backoff"
        store.clear_retry_hold(1)
        assert store.claim_next() is not None
    finally:
        store.close()


def test_a_real_escalation_still_escalates(asked):
    """The fix must not pass by making the OS never escalate at all."""
    deliver, unreachable = T.Recorder(), T.Recorder()
    verdict = {"escalate": True, "answer": "", "reason": "this spends money",
               "verdict": "denied", "approve": False, "dispatch": None}
    store = NeoStore()
    try:
        neo_mod.drain_queue(
            store, model="sonnet", deliver=deliver, unreachable=unreachable,
            answer=lambda *a, **k: dict(verdict))
    finally:
        store.close()

    assert len(deliver) == 1, "a judged escalation must still reach the user"
    assert not unreachable.calls, "a judgement was reported as a transport failure"
    q = _q()
    assert q["status"] == "escalated"
    assert q["answer_reason"] == "this spends money"
    assert q["attempts"] == 0


def test_the_inbox_says_unreachable_and_never_escalated(asked, project):
    """§3 of the work order: the surfaces must say which of the two happened."""
    daemon, wo = asked
    answerer = T.FaultyAnswerer(T.RC1_EMPTY)
    daemon._panel_answer = lambda _cfg: answerer          # noqa: SLF001
    for _ in range(MAX_ANSWER_ATTEMPTS + 1):
        _unhold()
        daemon._neo_drain()                               # noqa: SLF001

    central = CentralStore()
    try:
        rows = central.unacked_inbox()
    finally:
        central.close()
    titles = [r["title"] for r in rows]
    assert any("could not be reached" in t for t in titles), titles
    assert not any("escalated" in t.lower() for t in titles), \
        "a crash was announced to the user as an escalation"
    body = next(r["body"] for r in rows if "could not be reached" in r["title"])
    assert "NOBODY HAS JUDGED THIS" in body

    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
        assert fresh["needs_attention"]
        assert "could not answer" in fresh["attention_reason"]
    finally:
        store.close()


def test_an_approval_gate_is_never_decided_by_a_crash(asked, project):
    """The sharpest case: a fabricated verdict on a privileged action.

    Before the fix the synthesised `{escalate: True}` reached `_deliver_gate_verdict`,
    which wrote "Neo declined to decide" against the approval and marked the row
    escalated — a judgement on a gate that no model ever made.
    """
    daemon, _wo = asked
    neo = NeoStore()
    try:
        neo.conn.execute("UPDATE questions SET kind='approval' WHERE id=1")
    finally:
        neo.close()

    daemon._panel_answer = lambda _cfg: T.FaultyAnswerer(T.RC1_EMPTY)  # noqa: SLF001
    daemon._neo_drain()                                                # noqa: SLF001

    store = ProjectStore(project)
    try:
        approval = store.approval_for_question(1)
    finally:
        store.close()
    assert approval is None or approval["status"] == "pending", \
        "an unreachable model closed a privileged-action gate"
    assert _q()["status"] == "queued"


# -- §3: the panel — a seat nobody heard from --------------------------------------


def _ok(seat: str, reply: str = '{"escalate": false, "verdict": "approved"}') -> Opinion:
    return Opinion(seat=seat, raw=reply, status="ok", model="m")


def test_an_unreachable_veto_seat_is_not_a_shrunken_quorum():
    """Ruled by the user via Neo, question 436: no verdict without every veto seat."""
    opinions = [_ok("premise"), _ok("taste"),
                Opinion(seat="blast", raw="boom", status="abstained", model="m",
                        replied=False),
                _ok("record")]
    roster = ["premise", "record", "blast", "taste"]
    assert panel.unreachable_veto_seats(opinions, roster) == ["blast"]


def test_a_seat_this_build_does_not_ship_is_not_a_transport_fault():
    """A seat that can never run must not re-queue the question; retrying is pointless.

    Keyed on the `unavailable` MARKER, not on the word `failed` — review round 1. The
    real construction sites are driven in
    `tests/test_neo_panel.py::test_the_never_shipped_seat_is_exempt_by_its_marker_not_by_its_status`
    and `tests/test_validation_panel.py::test_a_validation_seat_this_build_does_not_ship_is_marked_unavailable`;
    this is the predicate's own unit.
    """
    opinions = [_ok("premise"),
                Opinion(seat="blast", raw="no such seat", status="failed", model="",
                        replied=False, unavailable=True)]
    assert panel.unreachable_veto_seats(opinions, ["premise", "blast"]) == []


def test_the_exemption_needs_the_marker_and_not_merely_the_word_failed():
    """The hole review round 1 found: `failed` is worn by two unrelated facts.

    An unreached seat recorded `failed` WITHOUT the marker is a transport fault, and
    must still stop the verdict. A predicate spelling out status words let this through.
    """
    opinions = [_ok("premise"),
                Opinion(seat="blast", raw="boom", status="failed", model="m",
                        replied=False)]
    assert panel.unreachable_veto_seats(opinions, ["premise", "blast"]) == ["blast"]


def test_an_unusable_reply_is_not_silence():
    """A seat that replied with rubbish was REACHED; `arbitrate` already handles it."""
    opinions = [_ok("premise"),
                Opinion(seat="blast", raw="{{{", status="failed", model="m")]
    assert panel.unreachable_veto_seats(opinions, ["premise", "blast"]) == []


def test_a_veto_seat_with_no_opinion_at_all_is_unreached():
    """`run_blind` cannot produce this today. If it ever does, a veto nobody recorded is
    a veto nobody heard — the safe direction is to refuse the verdict, not to assume."""
    assert panel.unreachable_veto_seats([_ok("premise")],
                                        ["premise", "blast"]) == ["blast"]


def test_a_silent_veto_seat_never_becomes_a_vote():
    """Belt and braces on the older rule: silence is neither veto nor consent."""
    silent = [{"seat": "blast", "status": "abstained", "reply": "boom"},
              {"seat": "record", "status": "abstained", "reply": "boom"}]
    assert panel.arbitrate(silent) is None


def test_a_refused_veto_seat_holds_the_question_and_spends_no_attempt(asked,
                                                                      fake_claude):
    """REVIEW ROUND 2, END TO END — the property the whole ladder turns on.

    A usage-limit refusal at `blast` must reach `NeoStore.hold_claim` through
    `drain_queue`'s `UsageLimitError` branch: parked until the window reopens, with
    `attempts` untouched. Through the generic branch instead it would spend all three
    retries inside twenty minutes against an outage the inbox has measured in hours —
    GitHub issue #235, which spec §1 says never to repeat.
    """
    from jarvis.catalog import NeoConfig, PanelConfig

    fake_claude.refuse_seat("blast")
    cfg = NeoConfig(panel=PanelConfig(
        enabled=True, roster=("premise", "record", "blast", "taste", "chair")))
    deliver, unreachable = T.Recorder(), T.Recorder()

    store = NeoStore()
    try:
        results = neo_mod.drain_queue(
            store, model=cfg.model, deliver=deliver, unreachable=unreachable,
            answer=lambda s, question, *a, **k: panel.decide(s, question, cfg))
    finally:
        store.close()

    assert not deliver.calls and not unreachable.calls
    assert results[0]["outcome"] == "held", (
        f"a refused veto seat took the wrong branch: {results[0]['outcome']}"
    )
    q = _q()
    assert q["status"] == "queued"
    assert q["attempts"] == 0, (
        "a spent window burned a transport retry — the issue #235 shape"
    )
    assert q["retry_after"] > time.time() + 60, (
        "the question was not parked until the window reopens"
    )


# -- §4: the supervisor's alarm queue ----------------------------------------------


@pytest.mark.parametrize("fault", T.TRANSPORT_FAULTS)
def test_an_unreachable_supervisor_decides_nothing(fault):
    exc = None
    try:
        T.raise_fault(fault)
    except Exception as e:                                # noqa: BLE001
        exc = e
    verdict = supervisor._transport_failure(exc, 500)     # noqa: SLF001
    assert verdict["unreachable"] is True
    assert verdict["decision"] == "", \
        "a call that never happened recorded a decision"
    assert verdict["question"] == "" and verdict["remedy"] == ""


def test_an_unreachable_alarm_goes_back_on_the_queue(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "spendy")
        alarm = store.add_alarm(wo["id"], kind="cost", reason="burning", seq=1)
        claimed = store.claim_next_alarm()
        assert claimed["id"] == alarm["id"]

        assert store.release_alarm_claim(alarm["id"], "the supervisor could not be "
                                                      "reached: boom") == "raised"
        row = next(a for a in store.alarms_of(wo["id"]) if a["id"] == alarm["id"])
        assert row["status"] == "raised"
        assert row["attempts"] == 1
        assert row["verdict"] is None, "a transport fault recorded a verdict"
        assert store.claim_next_alarm() is None, "re-claimed inside its own backoff"
    finally:
        store.close()


def test_an_alarm_is_only_failed_once_its_retries_are_spent(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "spendy")
        alarm = store.add_alarm(wo["id"], kind="cost", reason="burning", seq=1)
        # `attempts` counts RETRIES GRANTED, so three retries means the fourth call is
        # the one that gives up — `reclaim_stale_alarms`' reading of the same column.
        for attempt in range(1, 5):
            store.conn.execute("UPDATE wo_alarms SET retry_after=NULL WHERE id=?",
                               (alarm["id"],))
            store.claim_next_alarm()
            outcome = store.release_alarm_claim(alarm["id"], "unreachable", 3)
            if attempt < 4:
                assert outcome == "raised", f"gave up on attempt {attempt}"
        assert outcome == "failed"
        row = next(a for a in store.alarms_of(wo["id"]) if a["id"] == alarm["id"])
        assert row["status"] == "failed"
        assert "nobody has judged this" in row["verdict_reason"]
    finally:
        store.close()


# -- §5: dispatch -------------------------------------------------------------------


def test_a_launch_that_failed_leaves_the_order_pending(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "build it")
        store.claim_next_pending()
        assert store.release_dispatch_claim(wo["id"], "boom") == "pending"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "pending", "a blip became a terminal state"
        assert fresh["dispatch_attempts"] == 1
        assert not fresh["needs_attention"], \
            "the user was flagged before a single retry"
        assert store.claim_next_pending() is None, "re-claimed inside its own backoff"
    finally:
        store.close()


def test_dispatch_only_fails_at_the_ceiling(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "build it")
        for _ in range(MAX_DISPATCH_ATTEMPTS):
            store.conn.execute("UPDATE work_orders SET retry_after=NULL WHERE id=?",
                               (wo["id"],))
            store.claim_next_pending()
            outcome = store.release_dispatch_claim(wo["id"], "boom")
        assert outcome == "failed"
        assert store.get_work_order(wo["id"])["status"] == "failed"
    finally:
        store.close()


def test_a_successful_launch_clears_the_failed_ones(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "build it")
        store.claim_next_pending()
        store.release_dispatch_claim(wo["id"], "boom")
        store.clear_dispatch_attempts(wo["id"])
        fresh = store.get_work_order(wo["id"])
        assert fresh["dispatch_attempts"] == 0
        assert fresh["retry_after"] is None
    finally:
        store.close()


# -- §6: message delivery ------------------------------------------------------------


def test_the_users_words_are_not_dropped_on_a_delivery_failure(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "build it")
        msg_id = store.queue_message(wo["id"], "actually, use JSON")

        assert store.record_delivery_failure(msg_id, "boom") == "queued"
        row = next(m for m in store.list_messages(wo["id"]) if m["id"] == msg_id)
        assert row["status"] == "queued", "the user's message was discarded"
        assert row["attempts"] == 1
        assert row["last_error"] == "boom"
        assert [m["id"] for m in store.deliverable_messages()] == [], \
            "offered again inside its own backoff"
        assert [m["id"] for m in store.queued_messages()] == [msg_id], \
            "a held message must stay visible to `invariants.stuck_message`"
    finally:
        store.close()


def test_a_message_is_only_failed_once_its_retries_are_spent(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "build it")
        msg_id = store.queue_message(wo["id"], "actually, use JSON")
        outcomes = [store.record_delivery_failure(msg_id, "boom") for _ in range(3)]
        assert outcomes == ["queued", "queued", "failed"]
        row = next(m for m in store.list_messages(wo["id"]) if m["id"] == msg_id)
        assert row["status"] == "failed"
    finally:
        store.close()


def test_a_recovered_transport_delivers_the_message_it_held(started, project):
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "build it")
        msg_id = store.queue_message(wo["id"], "actually, use JSON")
        store.record_delivery_failure(msg_id, "boom")
        store.conn.execute("UPDATE wo_messages SET retry_after=NULL WHERE id=?",
                           (msg_id,))
        assert [m["content"] for m in store.deliverable_messages()] \
            == ["actually, use JSON"]
    finally:
        store.close()
