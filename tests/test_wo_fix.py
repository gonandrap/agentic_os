"""§11 of docs/specs/2026-09-24-order-observability.md: the user reaching a SHIPPED remedy.

The entry point is the only new thing. §11 adds no remedy, widens no allow-list and skips
no grant, so these tests are written to fail if any of those three drifted: the registry
equality and the AST pin live in `tests/test_remedies.py` and stay there, and what is
graded here is the ALARM-LESS path — a proposal, a verdict and an application on a work
order that has no `wo_alarms` row at all.

THE TRAP THIS FILE IS BUILT AGAINST is `test_remedies.py`'s, one layer along: a negative
assertion is green on a path that never ran. "no alarm row was written" passes when
nothing was proposed, and "no message was queued" passes when the remedy never ran. So
every refusal here is asserted beside the positive it is the opposite of, and every
"nothing happened" is paired with a handler whose call count is asserted zero and then
one.
"""

from __future__ import annotations

import re
import time

import pytest

from jarvis import catalog, db, gates, ops, remedies
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

# §5's arming helper, reused verbatim: a catalog written by hand here could arm a remedy
# the parser would refuse and grade nothing.
from test_remedies import _arm


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


def _store(wo_id: str) -> ProjectStore:
    return ProjectStore(ops.find_work_order(wo_id)[1])


def _cfg(*allowed: str, enabled: bool = True) -> catalog.RemedyConfig:
    return catalog.RemedyConfig(enabled=enabled, allowed=tuple(allowed))


def _propose(store, project: str, subject: dict, remedy: str, argument: str,
             cfg: catalog.RemedyConfig, reason: str = "parked with nothing saying why"):
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        return remedies.propose_fix(store, neo, project, subject, remedy, argument, cfg,
                                    reason=reason)
    finally:
        neo.close()


def _events(store, wo_id: str, kind: str) -> list[dict]:
    return store.events_of_kind(wo_id, kind)


def _inbox(wo_id: str) -> list[dict]:
    central = CentralStore()
    try:
        return [dict(r) for r in central.conn.execute(
            "SELECT * FROM inbox WHERE wo_id=? ORDER BY id", (wo_id,)).fetchall()]
    finally:
        central.close()


# -- 1. the intent string, which is how `apply` learns what it was authorised to do -----


def test_the_user_intent_round_trips_and_a_foreign_string_is_not_guessed():
    """`apply` recovers the remedy and the subject FROM THE GRANT on this path, because
    there is no alarm row holding them. So the formatter and the parser are one another's
    inverse or the grant means something other than what the reviewer read.

    Both directions and a negative, in one test: a round trip, the SUPERVISOR's own
    `INTENT` (which must not be read as a user's), and an unrelated command line.
    """
    command = remedies.user_intent("wo-1234", "nudge", "say where you are")
    assert remedies.parse_user_intent(command) == {
        "remedy": "nudge", "subject_id": "wo-1234",
        "argument": "say where you are"}

    # A multi-line argument survives whole: the argument is the brief a filed work order
    # is written from, and a parser that stopped at the first newline would truncate it.
    long = "first line\n\nand more"
    assert remedies.parse_user_intent(
        remedies.user_intent("fo-99", "file_work_order", long))["argument"] == long

    assert remedies.parse_user_intent(
        remedies.INTENT.format(alarm_id="al-1", remedy="nudge", subject_id="wo-1",
                               argument="x")) is None
    assert remedies.parse_user_intent("gh pr merge 12 --squash") is None
    assert remedies.parse_user_intent("") is None


# -- 2. a proposal with no alarm row ---------------------------------------------------


def test_a_user_proposal_files_a_grant_and_a_question_and_no_alarm_row(started):
    """The whole point of §11: the three shipped remedies reachable from a diagnosis that
    has no alarm. The approval, the Neo question and the event are asserted TOGETHER with
    the absence of an alarm row, so "no alarm was written" cannot pass on a path that
    filed nothing either.
    """
    started()
    wo = ops.create_work_order("proj_a", "the stranded one")
    store = _store(wo["id"])
    try:
        outcome = _propose(store, "proj_a", store.get_work_order(wo["id"]), "nudge",
                           "say where you are", _cfg("nudge"))
        assert outcome["proposed"], outcome["reason"]
        approval = outcome["approval"]
        assert approval["kind"] == "self_heal"
        assert approval["status"] == "pending"
        assert approval["max_uses"] == remedies.GRANT_USES == 1
        assert approval["matched"] == ""
        assert remedies.parse_user_intent(approval["command"]) == {
            "remedy": "nudge", "subject_id": wo["id"],
            "argument": "say where you are"}

        assert outcome["question"]["kind"] == "approval"
        # The reviewer rules on the ACTION, and is told who asked: a request that read
        # "the supervisor judged it unhealthy" would be a false statement about a
        # user-initiated fix.
        question = outcome["question"]["question"]
        assert remedies.REMEDIES["nudge"].blast in question
        assert "supervisor" not in question.lower()
        assert approval["id"] == store.get_approval(approval["id"])["id"]

        assert store.alarms_of(wo["id"]) == []
        (event,) = _events(store, wo["id"], "remedy_proposed")
        payload = db.from_json(event["payload"])
        assert payload.get("alarm_id") is None
        assert payload["remedy"] == "nudge"
        # Nothing has reached the session: a proposal is not an act.
        assert store.queued_messages(wo["id"]) == []
    finally:
        store.close()


@pytest.mark.parametrize("remedy,cfg,expected", [
    ("nudge", _cfg("nudge", enabled=False), "supervisor.remedies.enabled"),
    ("nudge", _cfg("unblock"), "supervisor.remedies.allowed"),
    ("restart-the-daemon", _cfg("nudge"), "is not a remedy this OS has"),
])
def test_the_catalog_refuses_a_user_fix_and_files_nothing(started, remedy, cfg,
                                                          expected):
    """§11 widens no allow-list, and the user path inherits `_refusal` VERBATIM. Paired
    with the proposal test above, which is the same call with the switch armed."""
    started()
    wo = ops.create_work_order("proj_a", "the stranded one")
    store = _store(wo["id"])
    try:
        outcome = _propose(store, "proj_a", store.get_work_order(wo["id"]), remedy, "x",
                           cfg)
        assert not outcome["proposed"]
        assert expected in outcome["reason"]
        assert outcome["approval"] is None and outcome["question"] is None
        assert store.list_approvals(wo["id"]) == []
        assert _events(store, wo["id"], "remedy_proposed") == []
    finally:
        store.close()


def test_unblock_is_refused_on_a_feature_order_subject(started):
    """`Remedy.subjects` is enforced on this path too — `unblock` is work-order only, and
    the same call with `nudge` (which takes both) proposes."""
    started()
    wo = ops.create_work_order("proj_a", "the carrier")
    store = _store(wo["id"])
    try:
        feature = store.create_feature_order("a big one", "do it")
        store.create_work_order("manage it", parent_id=feature["id"], kind="manager")
        refused = _propose(store, "proj_a", feature, "unblock", "x",
                           _cfg("unblock", "nudge"))
        assert not refused["proposed"]
        assert "does not apply to a feature_order" in refused["reason"]

        allowed = _propose(store, "proj_a", feature, "nudge", "where are you",
                           _cfg("unblock", "nudge"))
        assert allowed["proposed"], allowed["reason"]
    finally:
        store.close()


def test_a_second_identical_request_is_refused_by_number(started):
    """Two live grants for one fix is two authorisations for one act. The refusal names
    the request the user is already waiting on, so they can go and answer it."""
    started()
    wo = ops.create_work_order("proj_a", "the stranded one")
    store = _store(wo["id"])
    try:
        first = _propose(store, "proj_a", store.get_work_order(wo["id"]), "nudge",
                         "say where you are", _cfg("nudge"))
        again = _propose(store, "proj_a", store.get_work_order(wo["id"]), "nudge",
                         "say where you are", _cfg("nudge"))
        assert not again["proposed"]
        assert str(first["approval"]["id"]) in again["reason"]
        assert len(store.list_approvals(wo["id"])) == 1
    finally:
        store.close()


# -- 3. applying it ---------------------------------------------------------------------


def _granted(store, wo_id: str, remedy: str, argument: str) -> dict:
    """A proposed user fix whose reviewer has said yes."""
    outcome = _propose(store, "proj_a", store.get_work_order(wo_id), remedy, argument,
                       _cfg(remedy))
    assert outcome["proposed"], outcome["reason"]
    store.decide_approval(outcome["approval"]["id"], "approved", "go on", "test")
    return store.get_approval(outcome["approval"]["id"])


@pytest.mark.parametrize("status", ["pending", "denied", "dismissed", "expired"])
def test_no_grant_no_user_fix_and_the_handler_is_never_reached(started, monkeypatch,
                                                              status):
    """THE SAFETY PROPERTY, unchanged by the new entry point. `pytest.raises` alone would
    pass on a refusal thrown AFTER the message went out, so the handler is patched and
    its call count is the evidence that nothing reached the session."""
    started()
    wo = ops.create_work_order("proj_a", "the stranded one")
    calls: list[tuple] = []
    monkeypatch.setitem(
        remedies.REMEDIES, "nudge",
        remedies.Remedy(**{**vars(remedies.REMEDIES["nudge"]),
                           "apply": lambda *a: calls.append(a) or "ran"}))
    store, central = _store(wo["id"]), CentralStore()
    try:
        outcome = _propose(store, "proj_a", store.get_work_order(wo["id"]), "nudge",
                           "say where you are", _cfg("nudge"))
        approval_id = outcome["approval"]["id"]
        if status == "expired":
            store.decide_approval(approval_id, "approved", "yes", "test")
            store.conn.execute("UPDATE approvals SET expires_at=? WHERE id=?",
                               (db.now() - 1, approval_id))
        elif status != "pending":
            store.decide_approval(approval_id, status, "because", "test")

        with pytest.raises(remedies.RemedyRefused):
            remedies.apply(store, central, "proj_a", store.get_approval(approval_id),
                           None, store.get_work_order(wo["id"]))
        assert calls == []
        assert store.queued_messages(wo["id"]) == []

        # The positive partner, in the same fixture: approved, it runs exactly once.
        if status == "pending":
            store.decide_approval(approval_id, "approved", "go on", "test")
            assert remedies.apply(store, central, "proj_a",
                                  store.get_approval(approval_id), None,
                                  store.get_work_order(wo["id"])) == "ran"
            assert len(calls) == 1
            assert store.get_approval(approval_id)["uses"] == 1
    finally:
        central.close()
        store.close()


def test_an_approved_user_nudge_is_sent_once_and_names_no_alarm(started):
    """The nudge's own words. `NUDGE` says the OS FLAGGED the order and was given
    permission to ask about it, which is false when the user asked — so the user path has
    its own wording, and the supervisor's stays byte-identical (asserted below)."""
    started()
    wo = ops.create_work_order("proj_a", "the quiet one")
    store, central = _store(wo["id"]), CentralStore()
    try:
        approval = _granted(store, wo["id"], "nudge", "say where you are")
        result = remedies.apply(store, central, "proj_a", approval, None,
                                store.get_work_order(wo["id"]))
        assert wo["id"] in result

        (message,) = store.queued_messages(wo["id"])
        assert message["source"] == remedies.MESSAGE_SOURCE
        assert "say where you are" in message["content"]
        assert "flagged" not in message["content"]
        assert "alarm" not in message["content"].lower()
        assert "al-" not in message["content"]

        (event,) = _events(store, wo["id"], "remedy_applied")
        payload = db.from_json(event["payload"])
        assert payload.get("alarm_id") is None
        assert payload["use"] == 1
        assert store.get_approval(approval["id"])["uses"] == 1
        assert store.alarms_of(wo["id"]) == []

        rows = [r for r in _inbox(wo["id"]) if "nudge" in (r["body"] or "")]
        assert rows, _inbox(wo["id"])
        assert "alarms show" not in (rows[-1]["body"] or "")
    finally:
        central.close()
        store.close()


def test_the_supervisors_own_nudge_wording_is_unchanged(started):
    """The other side of the same fork. A user-initiated wording that LEAKED into the
    supervisor's path would be an OS that stopped saying it flagged the order itself."""
    started()
    wo = ops.create_work_order("proj_a", "the burning one")
    store, central = _store(wo["id"]), CentralStore()
    try:
        alarm = store.add_alarm(wo["id"], "long-turn", 1, "two hours on one turn")
        approval = store.add_approval(
            wo["id"], "self_heal",
            f"heal {alarm['id']}: nudge {wo['id']} — say where you are", max_uses=1)
        store.update_alarm(alarm["id"], status="proposed", verdict="propose",
                           remedy="nudge", remedy_argument="say where you are",
                           remedy_approval_id=approval["id"])
        store.decide_approval(approval["id"], "approved", "go on", "test")
        remedies.apply(store, central, "proj_a", store.get_approval(approval["id"]),
                       store.get_alarm(alarm["id"]), store.get_work_order(wo["id"]))

        (message,) = store.queued_messages(wo["id"])
        assert message["content"] == remedies.NUDGE.format(
            reason="two hours on one turn", argument="say where you are")
        assert store.get_alarm(alarm["id"])["status"] == "acked"
    finally:
        central.close()
        store.close()


def test_the_user_fix_brief_points_at_the_diagnosis_and_not_at_an_alarm(started):
    """`FIX_BRIEF` tells the worker to run `jarvis alarms show <alarm>`. On this path
    there is no alarm to show, so the brief points at §6's diagnosis instead — a brief
    naming a row that does not exist arrives as an assertion with no evidence."""
    started()
    wo = ops.create_work_order("proj_a", "the root cause one")
    store, central = _store(wo["id"]), CentralStore()
    try:
        approval = _granted(store, wo["id"], "file_work_order",
                            "Find why proj_a keeps parking.\n\nmore detail")
        result = remedies.apply(store, central, "proj_a", approval, None,
                                store.get_work_order(wo["id"]))
        filed = [w for w in store.list_work_orders() if w["id"] != wo["id"]]
        assert len(filed) == 2, result
        fix = next(w for w in filed if not w["title"].startswith("Ship the fix"))
        ship = next(w for w in filed if w["title"].startswith("Ship the fix"))
        assert store.dependencies(ship) == [fix["id"]]

        brief = fix["description"]
        assert "jarvis wo why " + wo["id"] in brief
        assert "alarms show" not in brief
        assert "supervisor" not in brief.lower()
        assert "more detail" in brief
        assert "al-" not in ship["title"]
    finally:
        central.close()
        store.close()


def test_the_supervisors_own_fix_brief_is_unchanged(started):
    """The alarm half of the same fork, asserted against the literal."""
    started()
    wo = ops.create_work_order("proj_a", "the burning one")
    store, central = _store(wo["id"]), CentralStore()
    try:
        alarm = store.add_alarm(wo["id"], "big-rewrite", 1, "300k re-written")
        command = f"heal {alarm['id']}: file_work_order {wo['id']} — Fix the prefix miss."
        approval = store.add_approval(wo["id"], "self_heal", command, max_uses=1)
        store.update_alarm(alarm["id"], status="proposed", verdict="propose",
                           remedy="file_work_order",
                           remedy_argument="Fix the prefix miss.",
                           remedy_approval_id=approval["id"])
        store.decide_approval(approval["id"], "approved", "go on", "test")
        remedies.apply(store, central, "proj_a", store.get_approval(approval["id"]),
                       store.get_alarm(alarm["id"]), store.get_work_order(wo["id"]))

        fix = next(w for w in store.list_work_orders()
                   if w["title"] == "Fix the prefix miss.")
        assert fix["description"] == remedies.FIX_BRIEF.format(
            alarm_id=alarm["id"], reason="300k re-written",
            argument="Fix the prefix miss.")
    finally:
        central.close()
        store.close()


# -- 4. the verdict and the daemon ------------------------------------------------------


def test_a_denied_user_fix_says_nothing_was_done(started):
    """A refusal performs nothing and SAYS so, in the user's words. Never a queued
    message: the worker never asked, and a denial delivered into a running turn is the
    one act this gate exists to fence."""
    started()
    wo = ops.create_work_order("proj_a", "the stranded one")
    store = _store(wo["id"])
    try:
        outcome = _propose(store, "proj_a", store.get_work_order(wo["id"]), "nudge",
                           "say where you are", _cfg("nudge"))
        gates.apply_decision(store, outcome["approval"]["id"], "denied",
                             "not now", "test", project="proj_a")
        assert store.queued_messages(wo["id"]) == []
        rows = _inbox(wo["id"])
        assert rows, "the denial was silent"
        body = f"{rows[-1]['title']}\n{rows[-1]['body']}"
        assert "nothing was done" in body.lower()
        assert "not now" in body
        assert "al-" not in body
        (event,) = _events(store, wo["id"], "remedy_refused")
        assert db.from_json(event["payload"]).get("alarm_id") is None
    finally:
        store.close()


def test_the_daemon_applies_an_approved_alarmless_grant(started, catalog_file):
    """`remedy_tick` scanned alarms at `proposed`, so an alarm-less grant would have sat
    approved for ever. Asserted with the alarm-less row absent, and against the message
    the handler actually queues."""
    _arm(catalog_file, "nudge")
    daemon = started()
    wo = ops.create_work_order("proj_a", "the quiet one")
    store = _store(wo["id"])
    try:
        approval = _granted(store, wo["id"], "nudge", "say where you are")
        assert store.queued_messages(wo["id"]) == []
    finally:
        store.close()

    daemon.remedy_tick()
    deadline = time.monotonic() + 20.0
    while daemon.remedy_applying and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not daemon.remedy_applying, "the remedy tick never finished"

    store = _store(wo["id"])
    try:
        (message,) = store.queued_messages(wo["id"])
        assert "say where you are" in message["content"]
        assert store.get_approval(approval["id"])["uses"] == 1
        assert len(_events(store, wo["id"], "remedy_applied")) == 1

        # And it is not applied twice: the grant is spent, so the next tick finds
        # nothing — the property that stops one approval nudging every tick for ever.
        daemon.remedy_tick()
        deadline = time.monotonic() + 20.0
        while daemon.remedy_applying and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(_store(wo["id"]).queued_messages(wo["id"])) == 1
    finally:
        store.close()


def test_the_timeline_credits_the_user_rather_than_the_supervisor():
    """`remedy_proposed`/`remedy_applied` render as "The supervisor …", which is a false
    statement about a fix the user asked for. Both arms in one test, keyed on the payload
    having no `alarm_id`."""
    from jarvis import timeline

    supervised, _ = timeline._describe("remedy_applied",
                                       {"alarm_id": "al-1", "remedy": "nudge",
                                        "result": "queued one message"})
    assert supervised == "The supervisor acted"
    asked, detail = timeline._describe("remedy_applied",
                                       {"remedy": "nudge", "result": "queued one"})
    assert "supervisor" not in asked.lower()
    assert "queued one" in detail

    proposed, _ = timeline._describe("remedy_proposed",
                                     {"remedy": "nudge", "argument": "x"})
    assert "supervisor" not in proposed.lower()


# -- 5. `ops.fix` and `jarvis wo fix`: the entry point itself ---------------------------
#
# The layer above everything above. `ops.fix` MATCHES a blocker to one of the three
# shipped remedies and returns a dict; nothing here may add a remedy, widen an allow-list
# or act. So every test below grades two things at once: the sentence the payload carries,
# and what the databases hold afterwards.


def _neo_questions() -> list[dict]:
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        return [dict(r) for r in neo.conn.execute(
            "SELECT * FROM questions ORDER BY id").fetchall()]
    finally:
        neo.close()


def _nothing_was_written(wo_id: str) -> None:
    """Every row a proposal would have written, asserted absent. Paired in every test with
    the positive it is the opposite of — a negative alone is green on a path that never
    ran."""
    store = _store(wo_id)
    try:
        assert store.list_approvals(wo_id) == []
        assert store.alarms_of(wo_id) == []
        assert _events(store, wo_id, "remedy_proposed") == []
        assert store.queued_messages(wo_id) == []
    finally:
        store.close()
    assert _neo_questions() == []


def _stalled(title: str = "stalled on a prompt") -> dict:
    """The one `waiting_on` answer that reports `stalled` — no turn, `waiting_input`."""
    wo = ops.create_work_order("proj_a", title)
    store = _store(wo["id"])
    try:
        store.set_status(wo["id"], "waiting_input")
    finally:
        store.close()
    return wo


def _stranded(dead: bool) -> dict:
    """A `pending` order behind an edge that either can never clear, or is still live."""
    dep = ops.create_work_order("proj_a", "the thing it builds on")
    wo = ops.create_work_order("proj_a", "the stranded one", depends_on=[dep["id"]])
    if dead:
        store = _store(wo["id"])
        try:
            store.set_status(dep["id"], "cancelled")
        finally:
            store.close()
    return wo


def test_a_stalled_prompt_is_matched_to_nudge_and_nothing_is_written(started,
                                                                    catalog_file):
    """The first matched case. `confirm=False` is the default and FILES NOTHING, so the
    proposal and the empty tables are asserted together."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    out = ops.fix(wo["id"])

    assert out["blocker"] == ops.diagnose(wo["id"])["blocker"]
    assert out["blocker"]["what"] == "prompt"
    assert out["remedy"] == "nudge"
    # Straight off the registry, never re-worded: the words the user weighs and the words
    # the reviewer will rule on are one string.
    assert out["proposal"]["headline"] == remedies.REMEDIES["nudge"].headline
    assert out["proposal"]["blast"] == remedies.REMEDIES["nudge"].blast
    assert out["proposal"]["subject"] == wo["id"]
    assert out["proposal"]["argument"]
    assert "--confirm" in out["proposal"]["approving"]
    assert out["filed"] is None
    assert out["your_move"] is None
    _nothing_was_written(wo["id"])


def test_dead_edges_are_matched_to_unblock(started, catalog_file):
    _arm(catalog_file, "unblock")
    wo = _stranded(dead=True)

    out = ops.fix(wo["id"])

    assert out["blocker"]["what"] == "pending"
    assert out["remedy"] == "unblock"
    assert out["proposal"]["blast"] == remedies.REMEDIES["unblock"].blast
    assert out["your_move"] is None
    _nothing_was_written(wo["id"])


def test_live_edges_only_offer_the_users_own_all_command(started, catalog_file):
    """`unblock`'s handler is the DEFAULT mode and cuts only dead edges, so an order whose
    every edge is live is not covered — and `--all` is the user's own call, not an act the
    OS offers. Paired with the dead-edge test above: the same shape, one status apart."""
    _arm(catalog_file, "unblock")
    wo = _stranded(dead=False)

    out = ops.fix(wo["id"])

    assert out["remedy"] is None
    assert out["proposal"] is None
    assert out["your_move"]["detail"] == f"jarvis wo unblock {wo['id']} --all"
    assert "LIVE" in out["note"]
    assert "yours to run" in out["your_move"]["note"]
    _nothing_was_written(wo["id"])


def test_an_uncovered_blocker_hands_back_the_diagnosis_as_the_users_command(started,
                                                                           catalog_file):
    """§6's pre-validated way through, LABELLED as the user's: never a silent no-op and
    never an invented remedy."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = ops.create_work_order("proj_a", "waiting on a decision")
    ops.assume(wo["id"], "exporting CSV by default")

    out = ops.fix(wo["id"])

    assert out["blocker"]["what"] == "assumptions"
    assert out["remedy"] is None and out["proposal"] is None
    assert out["your_move"]["detail"] == ops.diagnose(wo["id"])["blocker"]["detail"]
    assert "no shipped remedy" in out["note"]
    _nothing_was_written(wo["id"])


def test_a_held_gate_is_labelled_the_workers_move_and_not_the_users(started,
                                                                    catalog_file,
                                                                    project):
    """`gate_held`'s way through — `jarvis gate request` / `jarvis gate contest` — is
    refused from anyone but the worker in its own worktree (spec 2026-09-12 §8). Labelling
    it "yours to run" sends the user at a command the OS will not accept from them."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = ops.create_work_order("proj_a", "ship it")
    store = _store(wo["id"])
    try:
        store.set_status(wo["id"], "waiting_input")
        approval = store.add_approval(wo["id"], kind="release",
                                      command="./scripts/shipit.sh", matched="shipit",
                                      justification=gates.NO_CASE_JUSTIFICATION,
                                      status=gates.AWAITING_CASE)
    finally:
        store.close()

    out = ops.fix(wo["id"])

    assert out["blocker"]["what"] == "gate_held"
    assert out["remedy"] is None and out["proposal"] is None
    # The detail still travels verbatim; only the label changed.
    assert out["your_move"]["detail"] == ops.diagnose(wo["id"])["blocker"]["detail"]
    assert f"jarvis gate request {approval['id']}" in out["your_move"]["detail"]
    assert "yours to run" not in out["your_move"]["note"]
    assert out["your_move"]["note"] == ops.FIX_WORKERS_TO_RUN
    assert "WORKER's" in out["your_move"]["note"]
    # Paired with the positive above: an uncovered blocker whose command IS the user's
    # still says so, so this is a per-slug label and not a rename of the only one.
    assert "yours to run" in ops.FIX_YOURS_TO_RUN
    assert ops.FIX_WORKER_MOVE == frozenset({"gate_held"})

    store = _store(wo["id"])
    try:
        assert store.alarms_of(wo["id"]) == []
        assert _events(store, wo["id"], "remedy_proposed") == []
        assert store.queued_messages(wo["id"]) == []
    finally:
        store.close()
    assert _neo_questions() == []


def test_a_nudge_the_mapping_calls_wrong_is_refused_in_its_own_words(started,
                                                                    catalog_file,
                                                                    monkeypatch):
    """`NUDGE_IS_WRONG` owns the rule and §11 mirrors it rather than restating it. No store
    shape puts a mapped slug on a matched blocker today, so the mapping is stated directly
    — the way `test_wo_why` grades the same arm."""
    _arm(catalog_file, "nudge")
    monkeypatch.setitem(ops.NUDGE_IS_WRONG, "prompt",
                        "a nudge cannot help — the mapping says so.")
    wo = _stalled()

    out = ops.fix(wo["id"])
    assert out["remedy"] is None and out["proposal"] is None
    assert "the mapping says so." in out["note"]

    # Named explicitly, refused in the same words: there is no --force here, and an OS
    # that proposes a nudge its own mapping calls wrong is proposing a known no-op.
    asked = ops.fix(wo["id"], remedy="nudge")
    assert asked["proposal"] is None
    assert "the mapping says so." in asked["note"]
    _nothing_was_written(wo["id"])


@pytest.mark.parametrize("armed,expected", [
    ((), "supervisor.remedies.enabled"),
    (("unblock",), "supervisor.remedies.allowed"),
])
def test_the_catalog_refuses_and_nothing_is_offered(started, catalog_file, armed,
                                                   expected):
    """Remedies off, and armed-but-not-this-one. Both say so IN WORDS and offer nothing —
    never an empty list of proposals (issue #227)."""
    _arm(catalog_file, *armed, enabled=bool(armed))
    wo = _stalled()

    out = ops.fix(wo["id"])

    assert out["remedy"] is None
    assert out["proposal"] is None
    assert expected in out["note"]
    _nothing_was_written(wo["id"])


def test_file_work_order_is_only_reachable_on_request_and_needs_an_argument(
        started, catalog_file):
    """No `waiting_on` slug means "a defect needs code written", so nothing matches this
    one automatically. Asked for, it refuses in `remedies`' own words with no argument and
    proposes with one."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = _stalled()

    assert ops.fix(wo["id"])["remedy"] == "nudge"

    bare = ops.fix(wo["id"], remedy="file_work_order")
    assert bare["proposal"] is None
    assert bare["note"] == remedies.NO_ARGUMENT.format(origin=wo["id"])

    named = ops.fix(wo["id"], remedy="file_work_order",
                    argument="Find why proj_a keeps parking.\n\nmore detail")
    assert named["remedy"] == "file_work_order"
    assert named["proposal"]["argument"].endswith("more detail")
    _nothing_was_written(wo["id"])


def test_confirm_files_one_approval_and_one_question_and_names_the_request(started,
                                                                          catalog_file):
    """`confirm=True` files it through `propose_fix` and does nothing else: the user is
    told the request number and that a reviewer decides. NOTHING IS APPLIED HERE."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    out = ops.fix(wo["id"], confirm=True)

    assert out["filed"]["proposed"] is True
    assert out["filed"]["unreachable"] is False
    approval_id = out["filed"]["approval"]
    assert str(approval_id) in out["filed"]["note"]
    assert out["filed"]["question"]
    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
        assert approval["id"] == approval_id
        assert approval["status"] == "pending"
        assert approval["kind"] == remedies.GATE_KIND
        assert len(_events(store, wo["id"], "remedy_proposed")) == 1
        # The act itself has NOT happened: `Daemon.remedy_tick` applies an approved grant.
        assert store.queued_messages(wo["id"]) == []
        assert _events(store, wo["id"], "remedy_applied") == []
        assert store.alarms_of(wo["id"]) == []
    finally:
        store.close()
    assert len(_neo_questions()) == 1


def test_neo_unreachable_leaves_the_request_pending_and_never_decided(started,
                                                                     catalog_file,
                                                                     monkeypatch):
    """A failure is not a verdict (kn-40db1828). Unreachable is reported as unreachable —
    never decided, never escalated, never refused."""
    from jarvis.neo_store import NeoStore

    _arm(catalog_file, "nudge")
    wo = _stalled()

    def boom(*a, **kw):
        raise RuntimeError("neo.db is locked")

    monkeypatch.setattr(NeoStore, "ask", boom)
    out = ops.fix(wo["id"], confirm=True)

    assert out["filed"]["unreachable"] is True
    assert out["filed"]["proposed"] is False
    note = f"{out['filed']['reason']} {out['filed']['note']}".lower()
    assert "could not reach" in note
    for word in ("refused", "denied", "escalated"):
        assert word not in note
    assert _neo_questions() == []
    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
        assert approval["status"] == "pending"
        assert str(approval["id"]) in out["filed"]["note"]
    finally:
        store.close()


def test_the_unreachable_payload_carries_no_word_of_the_exception(started,
                                                                  catalog_file,
                                                                  monkeypatch):
    """§6's boundary, inherited: NOTHING in the payload is text the OS did not write
    (`diagnose`'s docstring, kn-1791a5e6). An exception string can carry a path. It stays
    in the log."""
    from jarvis.neo_store import NeoStore

    _arm(catalog_file, "nudge")
    wo = _stalled()
    secret = "/home/someone/.jarvis/proj_a/neo.db is locked"

    monkeypatch.setattr(NeoStore, "ask", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError(secret)))
    out = ops.fix(wo["id"], confirm=True)

    blob = db.to_json(out)
    for word in ("/home/", "someone", ".jarvis/", "neo.db", "locked", "RuntimeError"):
        assert word not in blob, word
    assert "could not reach" in out["filed"]["reason"].lower()
    assert out["filed"]["unreachable"] is True


def test_an_approved_grant_is_found_behind_a_wall_of_other_approvals(started,
                                                                    catalog_file):
    """`list_approvals`'s window is the 200 newest rows of EVERY kind, so ordinary gate
    traffic could bury a §11 grant between approval and application — an approved act
    that silently never happens."""
    _arm(catalog_file, "nudge")
    daemon = started()
    wo = ops.create_work_order("proj_a", "the buried one")
    store = _store(wo["id"])
    try:
        approval = _granted(store, wo["id"], "nudge", "say where you are")
        for i in range(250):
            other = store.add_approval(wo["id"], "other_kind", f"some command {i}")
            store.decide_approval(other["id"], "approved", "go on", "test")
        assert approval["id"] not in [a["id"] for a in
                                      store.list_approvals(statuses=("approved",))]
        assert [a["id"] for a in remedies.user_grants(store)] == [approval["id"]]
    finally:
        store.close()

    daemon.remedy_tick()
    deadline = time.monotonic() + 20.0
    while daemon.remedy_applying and time.monotonic() < deadline:
        time.sleep(0.01)
    store = _store(wo["id"])
    try:
        (message,) = store.queued_messages(wo["id"])
        assert "say where you are" in message["content"]
    finally:
        store.close()


def test_a_settled_order_says_nothing_is_running(started, catalog_file):
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = ops.create_work_order("proj_a", "the finished one")
    store = _store(wo["id"])
    try:
        store.set_status(wo["id"], "completed")
    finally:
        store.close()

    out = ops.fix(wo["id"])

    assert out["status"] == "completed"
    assert out["remedy"] is None and out["proposal"] is None
    assert out["your_move"] is None
    assert "nothing" in out["note"]
    _nothing_was_written(wo["id"])


def test_an_idle_manager_is_not_a_blocker_and_is_handed_no_command(started, catalog_file):
    """An idle feature manager is doing exactly what it exists to do, so it is NOT a
    blocker: no "no shipped remedy covers this" and no "yours to run". Its `detail` names
    no command, and a sentence that describes a wait must never be handed back as the
    user's move — `FIX_NOTHING_TO_CLEAR`'s rule."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = ops.create_work_order("proj_a", "the feature's manager")
    store = _store(wo["id"])
    try:
        store.set_status(wo["id"], "idle")
    finally:
        store.close()

    out = ops.fix(wo["id"])

    assert out["blocker"]["what"] == "manager_idle"
    assert out["remedy"] is None and out["proposal"] is None
    assert out["your_move"] is None
    assert out["note"] == ops.FIX_NOTHING.format(detail=out["blocker"]["detail"])
    assert "yours to run" not in db.to_json(out)
    assert "no shipped remedy" not in out["note"]
    _nothing_was_written(wo["id"])


def test_a_signin_park_is_the_users_own_move(started, catalog_file):
    """`FIX_USERS_MOVE`'s reason, on the member whose command is not a `jarvis` one at all:
    `signin`'s detail names `/login`, which only the user can type. Unchanged behaviour —
    the diagnosis handed back, labelled theirs — and asserted HERE rather than left to the
    new default arm, which would otherwise swallow every unclassified slug silently."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = ops.create_work_order("proj_a", "parked on a sign-in")
    store = _store(wo["id"])
    try:
        store.set_status(wo["id"], "waiting_input")
        turn = store.create_turn(wo["id"], kind="message", prompt="do the thing")
        store.finish_turn(turn["id"], "failed",
                          error="Failed to authenticate: OAuth session expired and "
                                "could not be refreshed")
    finally:
        store.close()

    out = ops.fix(wo["id"])

    assert out["blocker"]["what"] == "signin"
    assert out["remedy"] is None and out["proposal"] is None
    assert out["your_move"]["detail"] == out["blocker"]["detail"]
    assert out["your_move"]["note"] == ops.FIX_YOURS_TO_RUN
    assert out["note"] == ops.FIX_UNCOVERED.format(what="signin")
    assert "signin" in ops.FIX_USERS_MOVE
    _nothing_was_written(wo["id"])


# -- 6. the slug NOBODY classified: a gap in the OS, not a shrug ------------------------
#
# Neo q839, reversing §11's "add NO remedy": auto-matching a blocker against a closed
# table in `ops` means the OS never learns which blockers it has no remedy FOR. Every
# slug `waiting_on` answers today is classified by one of the four sets, so the only way
# to reach the new default arm is a slug from the future — stated directly, the way
# `test_a_nudge_the_mapping_calls_wrong_is_refused_in_its_own_words` states its mapping.

UNKNOWN = {"what": "quota_ledger_drift", "stalled": False,
           "detail": "the OS has no idea, and that is the point of this test"}


def _unclassified(monkeypatch) -> None:
    monkeypatch.setattr(ops, "waiting_on", lambda store, wo: dict(UNKNOWN))


def test_an_unclassified_blocker_is_offered_a_new_remedy_order_and_writes_nothing(
        started, catalog_file, monkeypatch):
    """The new default. `file_work_order` with an OS-authored brief that names the slug and
    asks for a REUSABLE remedy — and with `confirm=False` still nothing written at all."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = _stalled()
    _unclassified(monkeypatch)

    out = ops.fix(wo["id"])

    assert out["blocker"]["what"] == UNKNOWN["what"]
    assert out["remedy"] == "file_work_order"
    assert out["proposal"]["blast"] == remedies.REMEDIES["file_work_order"].blast
    brief = out["proposal"]["argument"]
    assert brief == ops.FIX_NEW_REMEDY_BRIEF.format(
        what=UNKNOWN["what"], detail=UNKNOWN["detail"], wo_id=wo["id"])
    # The four things the order is for, each asserted rather than assumed from the brief
    # being long: the slug, the module, the registry's own closing act, and the arming it
    # must NOT do.
    assert UNKNOWN["what"] in brief and UNKNOWN["detail"] in brief
    assert "src/jarvis/remedies.py" in brief
    assert "covers" in brief and "SHIPPED_REMEDIES" in brief
    assert "tests/test_remedies.py" in brief
    assert "allow-list" in brief
    assert wo["id"] in brief
    assert out["your_move"] is None
    assert out["filed"] is None
    _nothing_was_written(wo["id"])


def test_the_new_remedy_brief_carries_no_text_the_os_did_not_write(started, catalog_file,
                                                                  monkeypatch):
    """`diagnose`'s boundary, kn-1791a5e6: the slug and §6's `detail` are the ONLY
    interpolations, and both are OS-authored. No exception tail, no gate command, no
    transcript line."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = ops.create_work_order("proj_a", "ship it")
    store = _store(wo["id"])
    try:
        store.set_status(wo["id"], "waiting_input")
        store.add_approval(wo["id"], kind="release", command="./scripts/shipit.sh",
                           matched="shipit", justification="a worker's own words here",
                           status=gates.AWAITING_CASE)
        turn = store.create_turn(wo["id"], kind="message", prompt="SECRET PROMPT TEXT")
        store.finish_turn(turn["id"], "failed", error="Traceback: /home/someone/x.py")
    finally:
        store.close()
    _unclassified(monkeypatch)

    brief = ops.fix(wo["id"])["proposal"]["argument"]

    for leaked in ("scripts/shipit.sh", "a worker's own words here", "SECRET PROMPT TEXT",
                   "Traceback", "/home/someone"):
        assert leaked not in brief
    # Paired with the negatives: the brief is not empty, so the assertions above are not
    # green on a blank string.
    assert UNKNOWN["what"] in brief


def test_confirming_it_files_one_grant_and_adds_no_remedy(started, catalog_file,
                                                         monkeypatch):
    """The act rides the same `self_heal` grant as every other remedy: one request, a
    reviewer decides, nothing applied. AND THE REGISTRY IS STILL CLOSED afterwards — the
    order writes the new remedy as a reviewed diff, `ops.fix` never authors one."""
    _arm(catalog_file, *remedies.SHIPPED_REMEDIES)
    wo = _stalled()
    _unclassified(monkeypatch)

    out = ops.fix(wo["id"], confirm=True)

    assert out["remedy"] == "file_work_order"
    assert out["filed"]["proposed"] is True
    assert str(out["filed"]["approval"]) in out["filed"]["note"]
    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
        assert approval["kind"] == remedies.GATE_KIND
        assert approval["status"] == "pending"
        # Filed, not applied: no work order exists yet and none may until the gate opens.
        assert store.queued_messages(wo["id"]) == []
        assert [w["id"] for w in store.list_work_orders()] == [wo["id"]]
    finally:
        store.close()
    assert tuple(remedies.REMEDIES) == remedies.SHIPPED_REMEDIES


def test_the_cli_renders_the_payload_and_derives_nothing(started, catalog_file, capsys):
    """Both surfaces read one dict. The JSON arm is asserted equal to `ops.fix`, and the
    human arm against the payload's OWN sentences — a figure or a sentence in the terminal
    that is not in the dict is a second derivation for `--json` to disagree with."""
    import json

    from jarvis import cli

    _arm(catalog_file, "nudge")
    wo = _stalled()

    assert cli.main(["wo", "fix", wo["id"], "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == ops.fix(wo["id"])

    fresh = ops.fix(wo["id"])
    assert cli.main(["wo", "fix", wo["id"]]) == 0
    out = capsys.readouterr().out
    assert fresh["proposal"]["headline"] in out
    assert fresh["proposal"]["blast"] in out
    assert fresh["blocker"]["detail"] in out
    assert fresh["proposal"]["approving"] in out
    _nothing_was_written(wo["id"])

    assert cli.main(["wo", "fix", wo["id"], "--confirm"]) == 0
    confirmed = capsys.readouterr().out
    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
    finally:
        store.close()
    assert str(approval["id"]) in confirmed


# -- 8. the same control on §7's debugging page ----------------------------------------
#
# The page renders `ops.fix`'s dict VERBATIM, exactly as it renders the other four
# payloads: the route computes nothing, the GET writes nothing, and only the button
# confirms. What is graded here is the surface — the matching, the refusals and the
# filing are graded above, against the one function both surfaces call.

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis.ui.app import create_app  # noqa: E402

BLOCKS = ("diagnosis", "fix", "live", "anatomy", "context")


@pytest.fixture()
def client(started):
    started()
    return TestClient(create_app(), follow_redirects=False)


def _places(text: str, *names: str) -> list[int]:
    return [text.index(f'id="block-{n}"') for n in names]


def test_the_fix_block_sits_under_the_diagnosis_it_acts_on(client, catalog_file):
    """§11 clears the blocker §6 just named, so it belongs beside it — under the
    diagnosis and above the live frame."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    page = client.get(f"/wo/proj_a/{wo['id']}/debug")

    assert page.status_code == 200
    places = _places(page.text, *BLOCKS)
    assert places == sorted(places)
    fix = ops.fix(wo["id"])
    assert fix["proposal"]["headline"] in page.text
    assert fix["proposal"]["blast"] in page.text
    assert fix["note"] in page.text
    assert f'action="/wo/proj_a/{wo["id"]}/fix"' in page.text


def test_opening_the_page_files_nothing(client, catalog_file):
    """`confirm=False` on the GET. Asserted beside the proposal it did NOT file, so the
    empty tables cannot pass on a page that offered nothing."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    page = client.get(f"/wo/proj_a/{wo['id']}/debug")

    assert f'action="/wo/proj_a/{wo["id"]}/fix"' in page.text
    _nothing_was_written(wo["id"])


def test_the_button_files_one_proposal_and_the_page_names_the_gate_request(
        client, catalog_file):
    _arm(catalog_file, "nudge")
    wo = _stalled()

    posted = client.post(f"/wo/proj_a/{wo['id']}/fix")
    assert posted.status_code == 303
    page = client.get(posted.headers["location"])

    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
        assert approval["status"] == "pending"
        assert len(_events(store, wo["id"], "remedy_proposed")) == 1
        # FILED, NOT DONE: the daemon applies an approved grant, not this route.
        assert store.queued_messages(wo["id"]) == []
        assert _events(store, wo["id"], "remedy_applied") == []
    finally:
        store.close()
    assert len(_neo_questions()) == 1
    assert page.status_code == 200
    assert str(approval["id"]) in page.text
    assert "a reviewer decides" in page.text


def test_a_payload_with_nothing_to_offer_prints_its_note_and_offers_no_button(
        client, catalog_file):
    """Remedies off for the project: the payload's own sentence, and no control at all —
    never an empty list and never a disabled button worded by the page (issue #227)."""
    _arm(catalog_file, enabled=False)
    wo = _stalled()

    page = client.get(f"/wo/proj_a/{wo['id']}/debug")

    assert page.status_code == 200
    assert 'id="block-fix"' in page.text
    assert ops.fix(wo["id"])["note"] in page.text
    assert f'action="/wo/proj_a/{wo["id"]}/fix"' not in page.text
    _nothing_was_written(wo["id"])


def test_an_unreadable_fix_leaves_the_other_blocks_standing(client, catalog_file,
                                                            monkeypatch):
    """Degradation is a feature, not an error page: a 500 on the dashboard costs an inbox
    item and an `INV-UI-HEALTHY` fleet alarm."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ops, "fix", boom)

    page = client.get(f"/wo/proj_a/{wo['id']}/debug")

    assert page.status_code == 200
    for name in BLOCKS:
        assert f'id="block-{name}"' in page.text
    assert page.text.count("could not be read") == 1
    assert "the fix could not be read" in page.text
    assert "RuntimeError" in page.text and "kaboom" in page.text


# -- 9. what the redirect may carry -----------------------------------------------------
#
# The page prints no text a visitor supplied. A crafted `?filed=…` used to read as the OS
# speaking about what happened to an order — autoescaped, so never XSS, but a surface that
# can be made to state a false fact about an ACT is what §11's wording rules exist to
# prevent. The redirect carries an ID, the words are rebuilt by `ops`, exactly as
# `ops.forced_round_notice` does it for `validation/force`.

def test_the_redirect_carries_the_approval_id_and_nothing_else(client, catalog_file):
    _arm(catalog_file, "nudge")
    wo = _stalled()

    posted = client.post(f"/wo/proj_a/{wo['id']}/fix")

    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
    finally:
        store.close()
    assert posted.headers["location"] == (
        f"/wo/proj_a/{wo['id']}/debug?filed={approval['id']}#block-fix")
    page = client.get(posted.headers["location"])
    assert ops.fix_filed_notice(approval["id"]) in page.text


def _notice_sentences() -> list[str]:
    """Every OS-authored sentence a `?filed=` note could be built from, with the format
    slots cut out: `FIX_FILED`'s, `FIX_STILL_PENDING`'s and `FIX_UNREACHABLE`'s. Fragments
    shorter than a clause are dropped — they are the punctuation between slots, not
    wording, and `gate request` alone would match the page's own button."""
    out: list[str] = []
    for template in (ops.FIX_FILED, ops.FIX_STILL_PENDING, ops.FIX_UNREACHABLE):
        out += [f for f in (p.strip() for p in re.split(r"\{[^}]*\}", template))
                if len(f) > 20]
    return out


#: `raw_absent` is whether searching the whole page for the crafted string MEANS anything.
#: `-1` is a substring of most generated ids (`wo-1a2b…`), so that search collides with the
#: order's own id about one run in sixteen and the test failed on a coin toss. What is
#: asserted for every value instead is the fact the test is about: NO notice was rendered
#: from it.
@pytest.mark.parametrize("crafted,raw_absent", [
    ("not-an-int", True),
    ("the OS cancelled this order and deleted its branch", True),
    ("12x", True),
    ("-1", False),
])
def test_a_crafted_filed_query_puts_no_words_on_the_page(client, catalog_file, crafted,
                                                         raw_absent):
    """Anything that is not an approval id renders NO note at all — never one built from
    text a visitor supplied."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    page = client.get(f"/wo/proj_a/{wo['id']}/debug", params={"filed": crafted})

    assert page.status_code == 200
    assert 'id="block-fix"' in page.text
    if raw_absent:
        assert crafted not in page.text
    # The injected claim, word by word — `crafted not in page.text` alone would pass on a
    # page that rendered it with one character escaped.
    for word in ("cancelled", "deleted", "branch", "not-an-int", "12x"):
        assert word not in page.text, word
    # And no note at all, in `ops`'s own wording: nothing the OS would say about a filed,
    # pending or unreachable fix is on the page.
    for sentence in _notice_sentences():
        assert sentence not in page.text, sentence


def test_the_unreachable_press_names_the_request_the_user_can_answer(client, catalog_file,
                                                                    monkeypatch):
    """A failure is not a verdict (kn-40db1828), on the page too — AND the pending request
    survives the redirect. `propose_fix` writes the grant before it asks, so an unreachable
    Neo leaves a request the user can answer themselves, which is the one thing that gets
    them unstuck. Carried as `pending-<id>` and reworded by `ops`, never as text."""
    from jarvis.neo_store import NeoStore

    _arm(catalog_file, "nudge")
    wo = _stalled()
    monkeypatch.setattr(NeoStore, "ask", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("neo.db is locked")))

    posted = client.post(f"/wo/proj_a/{wo['id']}/fix")
    page = client.get(posted.headers["location"])

    store = _store(wo["id"])
    try:
        (approval,) = store.list_approvals(wo["id"])
        assert approval["status"] == "pending"
    finally:
        store.close()
    assert posted.headers["location"] == (
        f"/wo/proj_a/{wo['id']}/debug?filed=pending-{approval['id']}#block-fix")
    assert page.status_code == 200
    # `ops.fix_pending_notice`'s words, escaped by Jinja where it quotes the command.
    from markupsafe import escape

    assert str(escape(ops.fix_pending_notice(approval["id"]))) in page.text
    assert f"gate request {approval['id']} is filed and still pending" in page.text
    assert ops.FIX_UNREACHABLE in page.text
    assert "is filed and a reviewer decides it" not in page.text
    assert "neo.db" not in page.text and "locked" not in page.text


def test_the_unreachable_press_with_no_request_at_all_names_none(client, catalog_file,
                                                                 monkeypatch):
    """The other half of the pair: nobody could be asked AND nothing was filed, so the
    redirect carries the bounded flag and the page names no request. Paired with the test
    above — a page that always said "still pending" would be wrong here."""
    _arm(catalog_file, "nudge")
    wo = _stalled()
    # It throws BEFORE the grant is written, so there is no request to name.
    monkeypatch.setattr(remedies, "propose_fix", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("neo.db is locked")))

    posted = client.post(f"/wo/proj_a/{wo['id']}/fix")
    page = client.get(posted.headers["location"])

    assert "filed=unreachable" in posted.headers["location"]
    assert page.status_code == 200
    assert ops.FIX_UNREACHABLE in page.text
    assert "still pending" not in page.text
    assert "is filed and a reviewer decides it" not in page.text
    assert "neo.db" not in page.text and "locked" not in page.text


@pytest.mark.parametrize("crafted", ["pending-abc", "pending--1", "pending-", "pending-1x"])
def test_a_crafted_pending_query_puts_no_words_on_the_page(client, catalog_file, crafted):
    """The prefix is parsed as a prefix plus an INTEGER. Anything else renders nothing at
    all, exactly like an unbounded `?filed=`."""
    _arm(catalog_file, "nudge")
    wo = _stalled()

    page = client.get(f"/wo/proj_a/{wo['id']}/debug", params={"filed": crafted})

    assert page.status_code == 200
    assert 'id="block-fix"' in page.text
    assert "still pending" not in page.text
    assert "is filed and a reviewer decides it" not in page.text
    assert crafted not in page.text
