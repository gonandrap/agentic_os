"""The supervisor: the agent that answers a cost alarm, and the tick that runs it.

§2 of docs/superpowers/specs/2026-08-31-the-supervisor.md.

WHAT THESE TESTS ARE BUILT TO AVOID. A test that reaches the daemon without explicitly
enabling the supervisor exercises the disabled path and still gets a perfectly good
result — nothing fails — so the assertions are on CALL COUNTS and ROWS, never on a
verdict having come back. And `needs_attention == 0` grades nothing on its own:
`ProjectStore.clear_attention` reaches it too, and that is the regression the ack path
forbids. What discriminates is a re-derivable blocker landing in
`acknowledged_blockers`, which only `ops.ack_attention` writes.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from jarvis import catalog, db, ops, supervisor, usage
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

#: The shipped supervisor defaults. Named once so a test asserts against the CATALOG
#: rather than against a literal it copied from it.
CFG = catalog.SupervisorConfig()


def _reclaim(store):
    """`reclaim_stale_alarms` at the shipped bounds — both now come from the catalog."""
    return store.reclaim_stale_alarms(CFG.stale_reviewing_seconds,
                                      CFG.max_review_attempts)


def _stamp(at: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


def _prompt_row(at: float, text: str) -> dict:
    """A worker prompt in a Claude Code transcript. Duplicated from
    `tests/test_inspection.py` rather than shared through a new module: that file is
    being edited by three sibling work orders on this feature at the same time."""
    return {"type": "user", "timestamp": _stamp(at), "message": {"content": text},
            "promptSource": "sdk"}


def _assistant_row(at: float, mid: str, write: int = 0) -> dict:
    return {"type": "assistant", "timestamp": _stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 0,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": 0, "output_tokens": 1},
                        "content": [{"type": "text", "text": "ok"}]}}


# -- fixtures --------------------------------------------------------------------------


def _enable(catalog_file: Path, **overrides) -> None:
    """Turn the supervisor on in the shipped test catalog.

    EXPLICIT IN EVERY TEST THAT WANTS IT, never a fixture default: the whole point of the
    disabled pin below is that reaching the daemon without this call must exercise
    nothing, and a fixture that quietly enabled it would make that pin unfalsifiable.
    """
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True, **overrides}
    catalog_file.write_text(json.dumps(data))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


def _burning(daemon, monkeypatch, tmp_path, title="the long one", description=""):
    """One work order with a live `long-turn` alarm raised against it the real way.

    Through `Daemon.check_burning_turns` rather than a hand-written row, because the
    contract under test is that the supervisor picks up what the raiser produces.
    """
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    wo = ops.create_work_order("proj_a", title, description=description)
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        at = turn["started_at"]
        (root / "-proj" / f"{wo['id']}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in [
                # AFTER the turn row, as `claude` writes it: a transcript turn older
                # than the dispatch is the PREVIOUS turn, which `alarms` will not judge.
                _prompt_row(at + 1, "You are the worker agent for wo-1"),
                _assistant_row(at + 5, "m1"),
            ]))
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        monkeypatch.setattr("jarvis.daemon.time.time", lambda: at + 2 * 3600)
        daemon.check_burning_turns(daemon.catalog.projects[0], store)
    finally:
        store.close()
    return wo["id"]


def _drain(daemon, timeout: float = 20.0) -> None:
    """Run one supervisor tick and wait for its thread to finish.

    The drain runs on its own pool, so a test that asserted straight after the tick would
    be racing it. Waiting on the daemon's OWN guard rather than on a sleep: the guard is
    lowered by the future's done-callback, so this cannot pass early.
    """
    daemon.supervisor_tick()
    deadline = time.monotonic() + timeout
    while daemon.supervisor_draining and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not daemon.supervisor_draining, "the supervisor drain never finished"


def _alarm(wo_id: str) -> dict:
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        (row,) = store.alarms_of(wo_id)
        return row
    finally:
        store.close()


def _supervisor_calls(fake_claude) -> list[dict]:
    """Every `claude` invocation that was a supervisor review, identified the way the
    fake identifies one: by the persona in `--append-system-prompt`."""
    out = []
    for call in fake_claude.calls:
        argv = call.get("argv") or []
        if "--append-system-prompt" in argv:
            system = argv[argv.index("--append-system-prompt") + 1]
            if supervisor.SUPERVISOR_PERSONA.splitlines()[0] in system:
                out.append(call)
    return out


def _agent_calls(kind: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    store = CentralStore()
    try:
        return [dict(r) for r in store.conn.execute(
            "SELECT * FROM agent_calls WHERE kind=?", (kind,)).fetchall()]
    finally:
        store.close()


# -- the ack, which is the whole point -------------------------------------------------


def test_an_explicable_alarm_is_acked_and_the_ack_is_remembered_not_wiped(
        started, catalog_file, monkeypatch, tmp_path, fake_claude):
    """The done condition of §2, and the assertion that makes it mean something.

    `needs_attention == 0` ALONE GRADES NOTHING — `ProjectStore.clear_attention` reaches
    it too, and that is the exact regression the ack path forbids: it wipes
    `acknowledged_blockers` ("any ack against it is spent"), so a supervisor using it
    silently discards what the user has already dismissed on that order.

    So the order is given a blocker `invariants.true_blockers` genuinely re-derives, and
    the discriminating assertion is that the blocker is WRITTEN INTO
    `acknowledged_blockers` — which only `ops.ack_attention` does. `clear_attention`
    would leave the column NULL.

    A live alarm cannot be that blocker: `true_blockers` has no branch for one (an alarm
    fires on a `running` order and no branch matches that status), which is why the
    supervisor's answer is recorded on the ALARM ROW and the row is the memory.
    """
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        # A blocker that survives a re-derivation, so the ack has something to remember.
        store.update_work_order(wo_id, status="failed")
        store.flag_attention(wo_id, "worker failed — review and retry")
    finally:
        store.close()

    _drain(daemon)

    alarm = _alarm(wo_id)
    assert alarm["status"] == "acked"
    assert alarm["verdict"] == "ack"
    assert alarm["note"] and alarm["decided_at"]

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        wo = store.get_work_order(wo_id)
        assert wo["needs_attention"] == 0
        # THE DISCRIMINATING ASSERTION. `clear_attention` would have left this NULL.
        assert json.loads(wo["acknowledged_blockers"]) == [
            "worker failed — review and retry"]
        (event,) = store.events_of_kind(wo_id, "alarm_reviewed")
        payload = json.loads(event["payload"])
        assert payload["alarm_id"] == alarm["id"]
        assert payload["verdict"] == "ack"
        assert payload["note"] == alarm["note"]
    finally:
        store.close()

    # Exactly one, and of the right kind: `jarvis cost <wo-id>` is where the user finds
    # out what answering their alarm cost.
    (row,) = _agent_calls("supervisor")
    assert row["wo_id"] == wo_id and row["project"] == "proj_a"
    assert len(_supervisor_calls(fake_claude)) == 1


def test_the_ack_reaches_the_user_as_an_inbox_row_carrying_the_note(
        started, catalog_file, monkeypatch, tmp_path):
    """The notification the user gets INSTEAD of an attention item. Inbox rows reach
    every sink including Telegram, so the title is user-facing copy and is specified in
    the design rather than left to the implementation."""
    from jarvis.central_store import CentralStore

    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)
    _drain(daemon)

    central = CentralStore()
    try:
        rows = [r for r in central.unacked_inbox() if r["wo_id"] == wo_id]
    finally:
        central.close()

    (row,) = rows
    assert row["title"] == f"Supervisor cleared an alarm on {wo_id}"
    assert row["level"] == "info"
    assert row["body"] == _alarm(wo_id)["note"]


def test_a_pending_assumption_leaves_the_flag_up_and_the_alarm_is_still_acked(
        started, catalog_file, monkeypatch, tmp_path):
    """`ops.ack_attention` REFUSES an order with a decision waiting for the user, and
    that refusal is the right one to inherit: the assumption is the louder ask and
    burying it would drop work the user asked for.

    The alarm is still recorded `acked` — it WAS judged — and the flag stays up. That
    pair is the whole assertion: recording nothing would lose the verdict, and lowering
    the flag would bury the assumption.
    """
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.add_assumption(wo_id, "I used tabs, not spaces")
        store.flag_attention(wo_id, "1 assumption pending your review")
    finally:
        store.close()

    _drain(daemon)

    assert _alarm(wo_id)["status"] == "acked"
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        assert store.get_work_order(wo_id)["needs_attention"] == 1
    finally:
        store.close()


def test_an_escalate_verdict_records_the_intent_and_leaves_the_flag_up(
        started, catalog_file, monkeypatch, tmp_path):
    """The verdict half of §3's escalation, on the order that raised the alarm.

    The Neo question it files, and everything that happens to the answer, is
    `tests/test_alarm_escalation.py`. What stays true here: the flag is up and no message
    is queued to the worker — the supervisor never speaks to one.
    """
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path,
                     description="FORCE_SUPERVISOR_ESCALATE")
    _drain(daemon)

    alarm = _alarm(wo_id)
    assert alarm["status"] == "escalated"
    assert alarm["verdict"] == "escalate"
    assert not alarm["note"]

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        assert store.list_messages(wo_id) == []
    finally:
        store.close()


# -- the three failure shapes, and none of them is an ack ------------------------------


@pytest.mark.parametrize("token,expected_reason", [
    ("FORCE_SUPERVISOR_GARBAGE", supervisor.UNREADABLE_PREFIX),
    ("FORCE_SUPERVISOR_FAIL", "could not be reached"),
    ("FORCE_SUPERVISOR_NO_DECISION", supervisor.UNREADABLE_PREFIX),
])
def test_every_failure_leaves_the_alarm_unresolved_with_the_flag_up(
        started, catalog_file, monkeypatch, tmp_path, token, expected_reason):
    """THREE SHAPES ARRIVING BY TWO ROUTES, and that is the reason all three are here.

    Unreadable output and a well-formed object with no `decision` come back through
    `structured.request`'s `on_invalid`; a transport error does NOT — `ClaudeCliError`
    propagates untouched by design, because a call that never happened is not invalid
    output (kn-9b18a8eb). A fail-safe built on `on_invalid` alone raises out of the
    daemon's own thread pool on the middle case.

    An ack would be the worst possible default here: it makes a burning turn invisible.
    """
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path, description=token)

    _drain(daemon)  # in particular: this does not raise

    alarm = _alarm(wo_id)
    assert alarm["status"] == "failed"
    assert alarm["verdict"] is None
    assert expected_reason in alarm["verdict_reason"]

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        assert store.events_of_kind(wo_id, "alarm_reviewed") == []
    finally:
        store.close()


def test_a_reply_with_no_decision_raises_rather_than_defaulting():
    """`decision` is what says this is a supervisor verdict at all rather than some other
    JSON the model happened to emit, so its absence is a bad shape and not a default —
    `neo._validate_verdict` makes the identical call about `escalate`."""
    from jarvis import structured

    with pytest.raises(structured.InvalidOutput, match="decision"):
        supervisor._validate({"reason": "r", "note": "n", "question": ""}, 200)
    with pytest.raises(structured.InvalidOutput, match="decision"):
        supervisor._validate({"decision": "cancel the turn", "reason": "r"}, 200)


def test_the_fail_safe_escalates_and_marks_itself_failed():
    """A failure must never become an ack, and it must not be mistaken for a judgement
    either: `failed` is what puts the alarm at `failed` rather than at `escalated`."""
    verdict = supervisor._failed_verdict("well, it depends", 200)

    assert verdict["decision"] == "escalate"
    assert verdict["failed"] is True
    assert verdict["note"] == ""


# -- disabled by default, and that is a behaviour rather than a schema -----------------


def test_with_the_catalog_untouched_nothing_is_judged_and_nothing_is_spent(
        started, monkeypatch, tmp_path, fake_claude):
    """SHIPS OFF. The baseline is the tree §1 left — `wo_alarms` rows and the additive
    `alarm_id` payload key land whether the supervisor is on or not — so this is asserted
    as BEHAVIOUR: no calls, no spend rows, no verdict.

    On CALL COUNTS specifically. A test that merely reached the daemon here would pass
    having exercised the fallback and proved nothing.
    """
    daemon = started()          # no `_enable`: the shipped catalog, verbatim
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    before = len(fake_claude.calls)
    _drain(daemon)

    assert len(fake_claude.calls) == before
    assert _supervisor_calls(fake_claude) == []
    assert _agent_calls("supervisor") == []

    alarm = _alarm(wo_id)
    assert alarm["status"] == "raised"
    for column in ("verdict", "verdict_reason", "note", "decided_at",
                   "neo_question_id", "review_feedback", "reviewed_at"):
        assert alarm[column] is None, column
    assert alarm["review_status"] == "unreviewed"

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        (event,) = store.events_of_kind(wo_id, "cost_alarm")
        payload = json.loads(event["payload"])
        assert payload["kind"] == "long-turn"
        assert payload["seq"] == 1
        assert "still being billed" in payload["reason"]
        assert store.get_work_order(wo_id)["needs_attention"] == 1
    finally:
        store.close()


def test_nothing_in_the_module_hard_codes_a_threshold():
    """THE MAGIC-NUMBER GUARD, modelled on `test_inspection.py`'s. The supervisor JUDGES,
    so every number it judges by is policy and belongs in `catalog.SupervisorConfig`
    where `jarvis config set` can reach it per project (kn-67cdb54b).

    Kept non-vacuous by asserting the allow-list is small — a growing exemption list is
    how this test stops meaning anything.
    """
    tree = ast.parse(Path(supervisor.__file__).read_text())
    allowed = {0, 1, 60}  # list indices, and seconds-per-minute — a unit, not a setting
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)}

    assert not (literals - allowed), (
        f"undeclared numeric literal(s) {sorted(literals - allowed)} in supervisor.py — "
        "a threshold belongs in catalog.SupervisorConfig, not in the code that reads it")


def test_every_supervisor_setting_reaches_the_config_console(tmp_path):
    """`config_version.resolve` is reflective, so a field added to `SupervisorConfig`
    becomes a `jarvis config set` key with no edit to the console — but only if it is on
    the dataclass. This fails if a number goes back to being a module constant."""
    from jarvis import config_version

    cat = catalog.parse_catalog({"os": {}, "projects": [{"name": "p",
                                                         "path": str(tmp_path)}]})
    resolved = config_version.resolve(cat)

    for field_name in vars(catalog.SupervisorConfig()):
        assert f"os.supervisor.{field_name}" in resolved, field_name
        assert f"projects.p.supervisor.{field_name}" in resolved, field_name


def test_the_off_switch_is_a_safety_key_and_inherits_field_by_field(tmp_path):
    """Turning the supervisor off fleet-wide removes a reviewer, which is the same class
    of act as `os.neo.enabled` — and a project may turn it on while the fleet is off,
    which is the expected first configuration, so the tick may not short-circuit on the
    OS block alone."""
    from jarvis import config_version

    assert "os.supervisor.enabled" in catalog.SAFETY_KEYS

    cat = catalog.parse_catalog({
        "os": {"supervisor": {"model": "haiku"}},
        "projects": [{"name": "watched", "path": str(tmp_path),
                      "supervisor": {"enabled": True}}],
    })
    spec = cat.project("watched")

    assert cat.os.supervisor.enabled is False       # the fleet default stands
    assert spec.supervisor.enabled is True          # its own
    assert spec.supervisor.model == "haiku"         # inherited from os
    assert spec.supervisor.max_age_hours == 24      # inherited from the shipped default
    assert config_version.resolve(cat)[
        "projects.watched.supervisor.enabled"] is True


# -- the claim machinery ---------------------------------------------------------------


def test_the_stale_cutoff_exceeds_the_call_timeout(tmp_path):
    """A cutoff at or below the timeout re-claims an alarm out from under a call that is
    still running: the same alarm is judged twice and the second verdict overwrites the
    first. Refused in the parser, not left to a comment."""
    shipped = catalog.SupervisorConfig()
    assert shipped.stale_reviewing_seconds > shipped.timeout

    with pytest.raises(catalog.CatalogError, match="stale_reviewing_seconds"):
        catalog.parse_catalog({
            "os": {"supervisor": {"timeout": shipped.stale_reviewing_seconds}},
            "projects": []})


def _one_alarm(started) -> tuple[ProjectStore, dict]:
    """A work order with one `raised` alarm, written straight to the store.

    NOT through `_burning` here: that helper patches `time.time` for the rest of the
    test, and these two tests are entirely about which clock a row was written on.
    """
    wo = ops.create_work_order("proj_a", "the long one")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    return store, store.add_alarm(wo["id"], "long-turn", 1, "still being billed")


def test_a_stranded_review_is_returned_to_the_queue_then_given_up_on(
        started, monkeypatch):
    """`claim_next_alarm` sets `reviewing` and, without this, nothing ever sets it back:
    a daemon restart mid-drain would park the alarm for ever behind an attention flag
    with no explanation. `NeoStore.claim_next` shipped that way once (bl-3f5f1464).

    THE CLOCK IS MOVED WHILE THE ROW IS CREATED, not while it is read. Asserting
    `claim_next_alarm()` returns None twice would be vacuous, and `monkeypatch.undo()`
    inside the body would revert the `jarvis_home` and `catalog_file` fixtures too — so
    the patch is scoped and the reclaim then runs on the real clock (kn-2261aa98).
    """
    started()
    store, _ = _one_alarm(started)
    try:
        long_ago = db.now() - CFG.stale_reviewing_seconds - 60
        with monkeypatch.context() as m:
            m.setattr("jarvis.db.now", lambda: long_ago)
            claimed = store.claim_next_alarm()
        assert claimed["status"] == "reviewing"
        assert claimed["claimed_at"] == long_ago

        # Reclaimed, on the real clock, up to the ceiling...
        for attempt in range(1, CFG.max_review_attempts + 1):
            assert _reclaim(store) == {"requeued": [claimed["id"]], "failed": []}
            assert store.get_alarm(claimed["id"])["attempts"] == attempt
            with monkeypatch.context() as m:
                m.setattr("jarvis.db.now", lambda: long_ago)
                assert store.claim_next_alarm()["id"] == claimed["id"]

        # ... and then given up on: out of the queue, never looping in it.
        assert _reclaim(store) == {"requeued": [], "failed": [claimed["id"]]}
        row = store.get_alarm(claimed["id"])
        assert row["status"] == "failed"
        assert "stranded in reviewing" in row["verdict_reason"]
        assert store.claim_next_alarm() is None
    finally:
        store.close()


def test_the_tick_rescues_a_stranded_alarm_and_judges_it_in_the_same_pass(
        started, catalog_file, monkeypatch, tmp_path):
    """THE RECLAIM IS WIRED INTO THE TICK, not merely available on the store.

    Removing `reclaim_stale_alarms()` from `supervisor_tick` leaves every store-level
    test green — the drain simply never sees the row again and the alarm sits in
    `reviewing` behind an attention flag for ever, which is exactly how `NeoStore
    .claim_next` parked a question permanently (bl-3f5f1464).

    It also pins the ORDERING: the reclaim runs BEFORE the queued count is read, so an
    alarm rescued on this tick is judged on this tick rather than on the next one.
    """
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        (row,) = store.alarms_of(wo_id)
        long_ago = db.now() - CFG.stale_reviewing_seconds - 60
        with monkeypatch.context() as m:
            # Patched while CREATING the claim, so `claimed_at` is genuinely in the past;
            # the reclaim below then runs on the unpatched clock. `monkeypatch.undo()`
            # would revert the `jarvis_home` and `catalog_file` fixtures too and every
            # surface would render as an empty OS with no error anywhere (kn-2261aa98).
            m.setattr("jarvis.db.now", lambda: long_ago)
            assert store.claim_next_alarm()["id"] == row["id"]
    finally:
        store.close()

    _drain(daemon)   # ONE tick

    alarm = _alarm(wo_id)
    assert alarm["status"] == "acked"
    assert alarm["attempts"] == 1        # it was rescued, not claimed fresh


def test_a_fresh_claim_is_not_reclaimed(started):
    """The other half of the cutoff, and the one that would let a live call be judged
    twice if the comparison were inverted."""
    started()
    store, _ = _one_alarm(started)
    try:
        claimed = store.claim_next_alarm()
        assert _reclaim(store) == {"requeued": [], "failed": []}
        assert store.get_alarm(claimed["id"])["status"] == "reviewing"
    finally:
        store.close()


def test_an_alarm_past_the_review_window_is_skipped_rather_than_judged(
        started, catalog_file, monkeypatch, tmp_path, fake_claude):
    """Spend the user can no longer prevent is the noise the mechanism was tuned to
    avoid. Skipped OUT of the queue with a reason, never left in it — an alarm nothing
    will look at again must not be re-claimed on every tick for ever."""
    _enable(catalog_file, max_age_hours=1)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        (row,) = store.alarms_of(wo_id)
        store.conn.execute("UPDATE wo_alarms SET ts=? WHERE id=?",
                           (db.now() - 6 * 3600, row["id"]))
    finally:
        store.close()

    _drain(daemon)

    alarm = _alarm(wo_id)
    assert alarm["status"] == "skipped"
    assert "review window" in alarm["verdict_reason"]
    assert _supervisor_calls(fake_claude) == []
    assert _agent_calls("supervisor") == []


def test_an_alarm_on_a_settled_order_is_still_judged(
        started, catalog_file, monkeypatch, tmp_path):
    """The spend is a fact and the user still deserves the note. Only AGE excludes an
    alarm; the work order's status never does."""
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.update_work_order(wo_id, status="completed")
    finally:
        store.close()

    _drain(daemon)

    assert _alarm(wo_id)["status"] == "acked"


# -- the evidence packet ---------------------------------------------------------------


def test_the_evidence_packet_is_capped_and_says_so(started, monkeypatch, tmp_path):
    """A DELIBERATELY HUGE SESSION. The alarm is often ABOUT a 300k re-write, so an
    instrument that pasted the conversation in would be one of the largest calls the OS
    makes — measuring the fire by adding to it. `worker_brief.CORE_BUDGET_CHARS` is the
    precedent for both the constant and this test."""
    daemon = started()
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    wo = ops.create_work_order("proj_a", "the enormous one", description="x" * 40_000)
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        at = turn["started_at"]
        rows = []
        for i in range(400):
            rows.append(_prompt_row(at + i * 10, "y" * 2_000))
            rows.append(_assistant_row(at + i * 10 + 5, f"m{i}", write=400_000))
        (root / "-proj" / f"{wo['id']}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        for i in range(30):
            store.queue_message(wo["id"], "z" * 5_000)
        alarm = store.add_alarm(wo["id"], "big-rewrite", 1, "q" * 3_000)

        packet = supervisor.build_evidence(
            store, store.get_work_order(wo["id"]), alarm,
            CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    assert len(packet) < CFG.evidence_budget_chars
    # THE OMISSION IS STATED. A judge that cannot see it was shown a fraction weighs the
    # fraction as the whole.
    assert "omitted" in packet
    # The alarm and the order come first, so the two things the verdict is ABOUT are
    # never the sections that fall off the end.
    assert packet.startswith("# The alarm")
    assert "# The work order" in packet


def test_the_packet_carries_the_alarm_the_order_and_what_the_worker_last_said(
        started, monkeypatch, tmp_path):
    """The four sections the design names, and the transcript that is NOT among them."""
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path, title="write the design doc",
                     description="a long brief about the console")
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.queue_message(wo_id, "still drafting section four")
        (alarm,) = store.alarms_of(wo_id)
        packet = supervisor.build_evidence(store, store.get_work_order(wo_id), alarm,
                                           CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    assert "long-turn" in packet
    assert "write the design doc" in packet
    assert "a long brief about the console" in packet
    assert "turn 1:" in packet and "generating" in packet
    assert "still drafting section four" in packet


def test_the_system_prompt_is_byte_stable_across_reviews(jarvis_home):
    """Consecutive reviews share a cached prompt prefix — the property
    `neo.build_system_prompt` is built for and the reason the drain is FIFO."""
    from jarvis.neo_store import NeoStore

    store = NeoStore()
    try:
        first = supervisor.build_system_prompt(store, "proj_a")
        second = supervisor.build_system_prompt(store, "proj_a")
    finally:
        store.close()

    assert first == second
    assert first.startswith(supervisor.SUPERVISOR_PERSONA)


def test_jarvis_cost_has_a_label_for_the_supervisor_kind():
    """Without it `jarvis cost` prints the bare kind, which is a user-visible defect in a
    surface nobody would think to check while writing an agent."""
    from jarvis import agent_usage

    assert "supervisor" in agent_usage.KIND_LABELS
    assert agent_usage.KIND_LABELS["supervisor"] != "supervisor"


# -- what the supervisor may never do, pinned in code ----------------------------------


def test_the_supervisor_never_names_a_command_that_acts_on_a_work_order():
    """THE VERDICT VOCABULARY IS `{ack, escalate}`, AND THIS IS WHERE THAT LIVES.

    NOT an import walk. `tests/test_neo_panel.py::test_neo_never_imports_the_panel` walks
    `ast.Import`/`ast.ImportFrom`, which is decorative here: `supervisor.py` must
    legitimately import `ops` for `ack_attention`, so `ops.cancel_work_order` would sail
    straight through one. The attribute and name walk is what has teeth.

    The tempting "helpful" move on a ninety-minute turn is to kill it, and killing a turn
    destroys work with no other record.
    """
    tree = ast.parse(Path(supervisor.__file__).read_text())

    forbidden = {"cancel", "cancel_work_order", "set_status", "send_message",
                 "queue_message"}
    named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not (named & forbidden), sorted(named & forbidden)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [a.name for a in node.names] + [node.module or ""]
    assert not any("worker_session" in name.split(".") for name in imported), imported


def test_the_pin_would_catch_the_move_it_forbids():
    """A guard nobody has ever seen fail is a guard nobody knows works. The same walk,
    over source that does the forbidden thing."""
    tree = ast.parse("from . import ops\ndef go(wo):\n    ops.cancel_work_order(wo)\n")
    named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "cancel_work_order" in named
