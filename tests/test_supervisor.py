"""The supervisor: the agent that answers a cost alarm, and the tick that runs it (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""

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

#: The shipped supervisor defaults
CFG = catalog.SupervisorConfig()


def _reclaim(store):
    """`reclaim_stale_alarms` at the shipped bounds — both now come from the catalog"""
    return store.reclaim_stale_alarms(CFG.stale_reviewing_seconds,
                                      CFG.max_review_attempts)


def _stamp(at: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


def _prompt_row(at: float, text: str) -> dict:
    """A worker prompt in a Claude Code transcript"""
    return {"type": "user", "timestamp": _stamp(at), "message": {"content": text},
            "promptSource": "sdk"}


def _assistant_row(at: float, mid: str, write: int = 0) -> dict:
    return {"type": "assistant", "timestamp": _stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 0,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": 0, "output_tokens": 1},
                        "content": [{"type": "text", "text": "ok"}]}}


# -- fixtures


def _enable(catalog_file: Path, **overrides) -> None:
    """Turn the supervisor on in the shipped test catalog"""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True, **overrides}
    catalog_file.write_text(json.dumps(data))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


def _burning(daemon, monkeypatch, tmp_path, title="the long one", description=""):
    """One work order with a live `long-turn` alarm raised against it the real way"""
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
                # AFTER the turn row, as `claude` writes it: a transcript turn older than the dispatch is the...
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
    """Run one supervisor tick and wait for its thread to finish"""
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
    """Every `claude` invocation that was a supervisor review"""
    return [call for call in fake_claude.calls
            if supervisor.SUPERVISOR_PERSONA.splitlines()[0]
            in (call.get("system_prompt_seen") or "")]


def _agent_calls(kind: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    store = CentralStore()
    try:
        return [dict(r) for r in store.conn.execute(
            "SELECT * FROM agent_calls WHERE kind=?", (kind,)).fetchall()]
    finally:
        store.close()


# -- the ack, which is the whole point


def test_an_explicable_alarm_is_acked_without_dismissing_the_users_blockers(
        started, catalog_file, monkeypatch, tmp_path, fake_claude):
    """The done condition of §2, and the assertion that makes it mean something"""
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        # A blocker that survives a re-derivation, so the ack has something to remember
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
        # THE DISCRIMINATING ASSERTIONS
        assert wo["needs_attention"] == 1
        assert wo["attention_reason"] == "worker failed — review and retry"
        assert wo["acknowledged_blockers"] is None
        assert store.events_of_kind(wo_id, "acknowledged") == []
        (event,) = store.events_of_kind(wo_id, "alarm_reviewed")
        payload = json.loads(event["payload"])
        assert payload["alarm_id"] == alarm["id"]
        assert payload["verdict"] == "ack"
        assert payload["note"] == alarm["note"]
    finally:
        store.close()

    # Exactly one, and of the right kind: `jarvis cost <wo-id>` is where the user finds out what...
    (row,) = _agent_calls("supervisor")
    assert row["wo_id"] == wo_id and row["project"] == "proj_a"
    assert len(_supervisor_calls(fake_claude)) == 1


def test_the_ack_reaches_the_user_as_an_inbox_row_carrying_the_note(
        started, catalog_file, monkeypatch, tmp_path):
    """The notification the user gets INSTEAD of an attention item"""
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
    """`ops.ack_attention` REFUSES an order with a decision waiting for the user"""
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
    """The verdict half of §3's escalation, on the order that raised the alarm"""
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


# -- the three failure shapes, and none of them is an ack


@pytest.mark.parametrize("token", [
    "FORCE_SUPERVISOR_GARBAGE",
    "FORCE_SUPERVISOR_NO_DECISION",
])
def test_an_unreadable_reply_leaves_the_alarm_unresolved_with_the_flag_up(
        started, catalog_file, monkeypatch, tmp_path, token):
    """A MODEL THAT REPLIED, UNUSABLY"""
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path, description=token)

    _drain(daemon)  # in particular: this does not raise

    alarm = _alarm(wo_id)
    assert alarm["status"] == "failed"
    assert alarm["verdict"] is None
    assert supervisor.UNREADABLE_PREFIX in alarm["verdict_reason"]

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        assert store.events_of_kind(wo_id, "alarm_reviewed") == []
    finally:
        store.close()


def test_an_unreachable_supervisor_requeues_the_alarm_rather_than_failing_it(
        started, catalog_file, monkeypatch, tmp_path):
    """A CALL THAT NEVER HAPPENED (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §4.)"""
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path,
                     description="FORCE_SUPERVISOR_FAIL")

    _drain(daemon)  # in particular: this does not raise

    alarm = _alarm(wo_id)
    assert alarm["status"] == "raised", "a transport fault spent the alarm"
    assert alarm["attempts"] == 1, "the retry machinery was bypassed, not spent"
    assert alarm["verdict"] is None, "a call that never happened recorded a verdict"
    assert "could not be reached" in alarm["verdict_reason"]

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        # The flag is still up — the RAISE put it there and nothing here takes it down
        assert store.get_work_order(wo_id)["needs_attention"] == 1
        assert store.events_of_kind(wo_id, "alarm_reviewed") == []
    finally:
        store.close()


def test_a_reply_with_no_decision_raises_rather_than_defaulting():
    """`decision` is what says this is a supervisor verdict at all rather than some other JSON the model..."""
    from jarvis import structured

    with pytest.raises(structured.InvalidOutput, match="decision"):
        supervisor._validate({"reason": "r", "note": "n", "question": ""}, 200)
    with pytest.raises(structured.InvalidOutput, match="decision"):
        supervisor._validate({"decision": "cancel the turn", "reason": "r"}, 200)


def test_the_fail_safe_escalates_and_marks_itself_failed():
    """A failure must never become an ack"""
    verdict = supervisor._failed_verdict("well, it depends", 200)

    assert verdict["decision"] == "escalate"
    assert verdict["failed"] is True
    assert verdict["note"] == ""


# -- disabled by default, and that is a behaviour rather than a schema


def test_with_the_catalog_untouched_nothing_is_judged_and_nothing_is_spent(
        started, monkeypatch, tmp_path, fake_claude):
    """SHIPS OFF"""
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
    """THE MAGIC-NUMBER GUARD, modelled on `test_inspection.py`'s"""
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
    """`config_version.resolve` is reflective"""
    from jarvis import config_version

    cat = catalog.parse_catalog({"os": {}, "projects": [{"name": "p",
                                                         "path": str(tmp_path)}]})
    resolved = config_version.resolve(cat)

    for field_name in vars(catalog.SupervisorConfig()):
        for path in (f"os.supervisor.{field_name}",
                     f"projects.p.supervisor.{field_name}"):
            leaves = [k for k in resolved if k.startswith(f"{path}.")]
            assert path in resolved or leaves, field_name


def test_the_off_switch_is_a_safety_key_and_inherits_field_by_field(tmp_path):
    """Turning the supervisor off fleet-wide removes a reviewer"""
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


# -- the claim machinery


def test_the_stale_cutoff_exceeds_the_call_timeout(tmp_path):
    """A cutoff at or below the timeout re-claims an alarm out from under a call that is still running:..."""
    shipped = catalog.SupervisorConfig()
    assert shipped.stale_reviewing_seconds > shipped.timeout

    with pytest.raises(catalog.CatalogError, match="stale_reviewing_seconds"):
        catalog.parse_catalog({
            "os": {"supervisor": {"timeout": shipped.stale_reviewing_seconds}},
            "projects": []})


def _one_alarm(started) -> tuple[ProjectStore, dict]:
    """A work order with one `raised` alarm, written straight to the store"""
    wo = ops.create_work_order("proj_a", "the long one")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    return store, store.add_alarm(wo["id"], "long-turn", 1, "still being billed")


def test_a_stranded_review_is_returned_to_the_queue_then_given_up_on(
        started, monkeypatch):
    """`claim_next_alarm` sets `reviewing` and"""
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

        # ... and then given up on: out of the queue, never looping in it
        assert _reclaim(store) == {"requeued": [], "failed": [claimed["id"]]}
        row = store.get_alarm(claimed["id"])
        assert row["status"] == "failed"
        assert "stranded in reviewing" in row["verdict_reason"]
        assert store.claim_next_alarm() is None
    finally:
        store.close()


def test_the_tick_rescues_a_stranded_alarm_and_judges_it_in_the_same_pass(
        started, catalog_file, monkeypatch, tmp_path):
    """THE RECLAIM IS WIRED INTO THE TICK, not merely available on the store"""
    _enable(catalog_file)
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path)

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        (row,) = store.alarms_of(wo_id)
        long_ago = db.now() - CFG.stale_reviewing_seconds - 60
        with monkeypatch.context() as m:
            # Patched while CREATING the claim, so `claimed_at` is genuinely in the past
            m.setattr("jarvis.db.now", lambda: long_ago)
            assert store.claim_next_alarm()["id"] == row["id"]
    finally:
        store.close()

    _drain(daemon)   # ONE tick

    alarm = _alarm(wo_id)
    assert alarm["status"] == "acked"
    assert alarm["attempts"] == 1        # it was rescued, not claimed fresh


def test_a_fresh_claim_is_not_reclaimed(started):
    """The other half of the cutoff, and the one that would let a live call be judged twice if the..."""
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
    """Spend the user can no longer prevent is the noise the mechanism was tuned to avoid"""
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
    """The spend is a fact and the user still deserves the note"""
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


# -- the evidence packet


def _wo_subject(store, wo_id: str) -> dict:
    return {"kind": "work_order", "row": store.get_work_order(wo_id)}


def _fo_subject(store, fo_id: str) -> dict:
    return {"kind": "feature_order", "row": store.get_feature_order(fo_id)}


def test_the_evidence_packet_is_capped_and_says_so(started, monkeypatch, tmp_path):
    """A DELIBERATELY HUGE SESSION"""
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
            store, _wo_subject(store, wo["id"]), alarm,
            CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    assert len(packet) < CFG.evidence_budget_chars
    # THE OMISSION IS STATED
    assert "omitted" in packet
    # The alarm and the order come first
    assert packet.startswith("# The alarm")
    assert "# The work order" in packet


def test_the_packet_carries_the_alarm_the_order_and_what_the_worker_last_said(
        started, monkeypatch, tmp_path):
    """The four sections the design names, and the transcript that is NOT among them"""
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path, title="write the design doc",
                     description="a long brief about the console")
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.queue_message(wo_id, "still drafting section four")
        (alarm,) = store.alarms_of(wo_id)
        packet = supervisor.build_evidence(store, _wo_subject(store, wo_id), alarm,
                                           CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    assert "long-turn" in packet
    assert "write the design doc" in packet
    assert "a long brief about the console" in packet
    assert "turn 1:" in packet and "generating" in packet
    assert "still drafting section four" in packet


def _synthetic_row(at: float, mid: str) -> dict:
    """The zero-token assistant message Claude Code writes ITSELF"""
    return {"type": "assistant", "timestamp": _stamp(at),
            "message": {"id": mid, "model": usage.SYNTHETIC_MODEL,
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0, "output_tokens": 0},
                        "content": [{"type": "text", "text": "…"}]}}


def test_the_packet_says_a_turn_cost_nothing_rather_than_leaving_it_to_be_inferred(
        started, monkeypatch, tmp_path):
    """ISSUE 227, THE LAYER THAT REACHED THE USER. wo-f1ce0f24 turn 3 made no API call in 65 minutes"""
    daemon = started()
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    wo = ops.create_work_order("proj_a", "the dead turn")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        (root / "-proj" / f"{wo['id']}.jsonl").write_text("".join(
            json.dumps(r) + "\n" for r in [
                _prompt_row(FIXED_TURN_AT, "You are the worker agent for wo-1"),
                _synthetic_row(FIXED_TURN_AT + 10, "s1"),
                _prompt_row(FIXED_TURN_AT + 3900, "the same message again"),
                _assistant_row(FIXED_TURN_AT + 4031, "m1"),
            ]))
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        packet = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), None,
                                           CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    stalled = next(l for l in packet.splitlines() if l.startswith("- turn 1:"))
    assert "NO API CALL WAS EVER MADE — it cost nothing." in stalled
    assert stalled.index("NO API CALL") < stalled.index("3900s wall")
    assert "0 API calls costing 0 tokens / $0.00" in stalled
    assert "3900s unaccounted" in stalled and "0s generating" in stalled
    # The turn that answered the re-delivered message in 131 seconds is not swallowed
    assert "1 API call costing" in packet


def test_the_packet_never_calls_a_request_in_flight_a_stall(
        started, monkeypatch, tmp_path):
    """Spec §1: this is the line that fed the judge"""
    daemon = started()
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    wo = ops.create_work_order("proj_a", "the slow turn")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        # A prompt and nothing after it: the request has completed no block yet
        (root / "-proj" / f"{wo['id']}.jsonl").write_text(
            json.dumps(_prompt_row(FIXED_TURN_AT,
                                   "You are the worker agent for wo-1")) + "\n")
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        packet = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), None,
                                           CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    line = next(l for l in packet.splitlines() if l.startswith("- turn 1:"))
    assert "NO API CALL" not in line and "cost nothing" not in line
    assert "awaiting the model since" in line
    assert "this is not a stall" in line


#: A clock and a transcript that do not move
FIXED_NOW = 1_780_000_000.0
FIXED_TURN_AT = FIXED_NOW - 3600.0


def _fixed_transcript(path: Path) -> None:
    """Two turns, both settled, at absolute times"""
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        _prompt_row(FIXED_TURN_AT, "You are the worker agent for wo-1"),
        _assistant_row(FIXED_TURN_AT + 30, "m1"),
        _prompt_row(FIXED_TURN_AT + 120, "carry on"),
        _assistant_row(FIXED_TURN_AT + 150, "m2"),
    ]))


#: THE WORK-ORDER PACKET, COMMITTED
EXPECTED_WORK_ORDER_PACKET = """\
# The alarm
kind: long-turn
reason: turn 1 has run for 2 hours
raised on turn 1, 0 minute(s) ago

# The work order
{wo_id} [running] on claude-opus-5
this session is an ordinary work order
scheduled_actor: Daemon.settle_turns - the settler sweeps this status and a turn is on record (or the claim is still inside its grace)
title: write the design doc
brief: a long brief about the console

# Where it has been
now: running (the recorded spans stop short of it)
- pending for 0 minute(s)
(it has never been through validation)

# The session, turn by turn
- turn 1: 120s wall (30s generating, 0s blocked, 0s tools, 90s idle, 0s unaccounted), 1 API call costing 1 tokens / $0.00, context peak 0
    started by [dispatch] You are the worker agent for wo-1
- turn 2: 30s wall (30s generating, 0s blocked, 0s tools, 0s idle, 0s unaccounted), 1 API call costing 1 tokens / $0.00, context peak 0
    started by [a message] carry on

# What was last said about this order
- you → worker: still drafting section four"""


def test_the_work_order_packet_is_byte_for_byte_what_it_has_always_been(
        started, monkeypatch, tmp_path):
    """Reshaping `build_evidence`'s arguments into a subject dict must not move one byte of what it..."""
    daemon = started()
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    monkeypatch.setattr(db, "now", lambda: FIXED_NOW)

    wo = ops.create_work_order("proj_a", "write the design doc",
                               description="a long brief about the console")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        _fixed_transcript(root / "-proj" / f"{wo['id']}.jsonl")
        store.update_work_order(wo["id"], status="running", session_id=wo["id"],
                                model="claude-opus-5")
        store.queue_message(wo["id"], "still drafting section four")
        alarm = store.add_alarm(wo["id"], "long-turn", 1, "turn 1 has run for 2 hours")
        packet = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), alarm,
                                           CFG, daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    assert packet == EXPECTED_WORK_ORDER_PACKET.format(wo_id=wo["id"])


def test_the_work_order_packet_names_a_blocker_and_stays_silent_without_one(
        started, monkeypatch, tmp_path):
    """WHAT ALARM al-4bb82f7e ASKED FOR: an order correctly waiting on a dependency used to read as..."""
    daemon = started()
    monkeypatch.setattr(db, "now", lambda: FIXED_NOW)
    inspect_cfg = daemon.catalog.projects[0].inspect

    wo = ops.create_work_order("proj_a", "write the design doc")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        plain = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), None,
                                          CFG, inspect_cfg)
        waits_on = store.create_work_order("the thing it waits on",
                                           status="running")["id"]
        blocked = store.create_work_order("the blocked one",
                                          depends_on=[waits_on])["id"]
        packet = supervisor.build_evidence(store, _wo_subject(store, blocked), None,
                                           CFG, inspect_cfg)
    finally:
        store.close()

    assert not [line for line in plain.splitlines() if line.startswith("blocked")]
    (line,) = [line for line in packet.splitlines() if line.startswith("blocked:")]
    assert line == f"blocked: {supervisor.BLOCKED_SENTENCES['dependency']}"
    # `+ 2` since §7: this row also derives a scheduled actor, which sits at `+ 1`
    assert packet.splitlines().index(line) == \
           packet.splitlines().index(next(l for l in packet.splitlines()
                                          if l.startswith("this session is"))) + 2


def test_the_work_order_packet_names_the_scheduled_actor_and_stays_silent_without_one(
        started, monkeypatch):
    """§7 of docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md"""
    from jarvis import health

    daemon = started()
    monkeypatch.setattr(db, "now", lambda: FIXED_NOW)
    inspect_cfg = daemon.catalog.projects[0].inspect

    wo = ops.create_work_order("proj_a", "write the design doc")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        actor = health.actor(store, _wo_subject(store, wo["id"]))
        packet = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), None,
                                           CFG, inspect_cfg)
        store.update_work_order(wo["id"], status="budget_exhausted")
        spent = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), None,
                                          CFG, inspect_cfg)
        # The `OPEN_STATUSES` gate is what makes a settled row safe here: without it `ops.SCHEDULED_ACTORS`...
        store.update_work_order(wo["id"], status="completed")
        settled = supervisor.build_evidence(store, _wo_subject(store, wo["id"]), None,
                                            CFG, inspect_cfg)
    finally:
        store.close()

    assert actor is not None
    (line,) = [l for l in packet.splitlines() if l.startswith("scheduled_actor:")]
    assert line == f"scheduled_actor: {actor.pass_name} - {actor.why}"
    assert packet.splitlines().index(line) == \
           packet.splitlines().index(next(l for l in packet.splitlines()
                                          if l.startswith("this session is"))) + 1
    assert not [l for l in spent.splitlines() if l.startswith("scheduled_actor")]
    assert settled.startswith("# The work order")
    assert not [l for l in settled.splitlines() if l.startswith("scheduled_actor")]


def test_the_actor_line_sits_above_the_blocker_line(started, monkeypatch):
    """A parked pull request derives BOTH"""
    from jarvis import health

    daemon = started()
    monkeypatch.setattr(db, "now", lambda: FIXED_NOW)
    inspect_cfg = daemon.catalog.projects[0].inspect

    wo = ops.create_work_order("proj_a", "write the design doc")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        store.update_work_order(wo["id"], status="waiting_pr_merge",
                                pr_url="https://example.invalid/pr/1")
        subject = _wo_subject(store, wo["id"])
        actor = health.actor(store, subject)
        blocked = health.blocker(store, subject, CFG)
        packet = supervisor.build_evidence(store, subject, None, CFG, inspect_cfg)
    finally:
        store.close()

    assert actor is not None and actor.pass_name == "Daemon.poll_pull_requests"
    assert blocked == "pull-request"
    lines = packet.splitlines()
    at = lines.index(next(l for l in lines if l.startswith("this session is")))
    assert lines[at + 1] == f"scheduled_actor: {actor.pass_name} - {actor.why}"
    assert lines[at + 2] == f"blocked: {supervisor.BLOCKED_SENTENCES['pull-request']}"


def test_the_actor_verdict_never_reaches_the_blocker(started, monkeypatch):
    """`health.blocker` stays PURE (§7"""
    from jarvis import health

    started()
    monkeypatch.setattr(db, "now", lambda: FIXED_NOW)

    wo = ops.create_work_order("proj_a", "write the design doc")
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        subject = _wo_subject(store, wo["id"])
        assert health.actor(store, subject) is not None
        assert health.blocker(store, subject, CFG) is None
        fo = store.create_feature_order("a feature", "do the thing")
        assert health.actor(store, {"kind": "feature_order", "row": fo}) is None
    finally:
        store.close()


def test_each_transport_blocker_reads_its_sentence_from_the_hold_cause():
    """Three ids, three sentences, and the words come from `holds.HOLD_CAUSES`"""
    from jarvis import health, holds

    for blocker_id, cause in health.transport_blockers().items():
        assert holds.HOLD_CAUSES[cause] in supervisor.BLOCKED_SENTENCES[blocker_id]
    assert len({supervisor.BLOCKED_SENTENCES[b]
                for b in health.transport_blockers()}) == 3
    assert set(supervisor.BLOCKED_SENTENCES) == {*health.BLOCKERS, health.CHILDREN}


def test_the_packet_says_when_it_was_read_from_a_seal_and_says_nothing_when_it_was_not(
        started, monkeypatch, tmp_path):
    """§4 of docs/superpowers/specs/2026-09-27-order-autopsy-durability.md: every surface PRINTS which..."""
    from jarvis import autopsy

    daemon = started()
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    monkeypatch.setattr(db, "now", lambda: FIXED_NOW)

    wo = ops.create_work_order("proj_a", "write the design doc")
    name, path, _row = ops.find_work_order(wo["id"])
    store = ProjectStore(path)
    try:
        _fixed_transcript(root / "-proj" / f"{wo['id']}.jsonl")
        store.update_work_order(wo["id"], status="completed", session_id=wo["id"],
                                model="claude-opus-5")
        inspect_cfg = daemon.catalog.projects[0].inspect
        derived = supervisor._session_lines(store.get_work_order(wo["id"]), inspect_cfg,
                                            store)
        autopsy.seal(name, path, store.get_work_order(wo["id"]))
        (root / "-proj" / f"{wo['id']}.jsonl").unlink()
        sealed = supervisor._session_lines(store.get_work_order(wo["id"]), inspect_cfg,
                                           store)
    finally:
        store.close()

    assert not any("SEALED" in line for line in derived)
    assert "SEALED autopsy" in sealed[0] and autopsy.NORMAL in sealed[0]
    # The turns themselves are the same reading: the seal is a substitute, not a summary
    assert sealed[1:] == derived


# -- the evidence packet for a FEATURE order


def _feature(store, *, children: list[dict], title="the console feature",
             status="executing", carrier=True) -> str:
    """A feature and its child tree, written straight to the store"""
    fo = store.create_feature_order(title, description="widen the console")
    plan_wo_id = None
    if carrier:
        plan_wo_id = store.create_work_order("plan the console feature",
                                             kind="planner", parent_id=fo["id"])["id"]
    store.update_feature_order(fo["id"], status=status, plan_wo_id=plan_wo_id)
    for spec in children:
        child = store.create_work_order(
            spec["title"], parent_id=fo["id"], kind="worker",
            spec_section=spec.get("spec_section"),
            depends_on=spec.get("depends_on"))
        store.update_work_order(child["id"], status=spec["status"],
                                pr_url=spec.get("pr_url"))
        spec["id"] = child["id"]
    return fo["id"]


def test_the_feature_packet_names_the_tree_the_rulings_and_the_verdicts(
        started, project):
    """The four reads the feature page already makes, in the judge's own packet"""
    started()
    store = ProjectStore(project)
    try:
        children = [{"status": "failed", "title": "the broken one",
                     "spec_section": "2 — the parser"},
                    {"status": "completed", "title": "the finished one",
                     "pr_url": "https://example.invalid/pr/1"}]
        fo_id = _feature(store, children=children, status="executing")
        store.supersede_children(fo_id, [children[0]["id"]],
                                 note="the parser was dropped on purpose")
        round_ = store.open_validation_round(fo_id=fo_id, fingerprint="f1")
        store.close_validation_round(round_["id"], outcome="rejected",
                                     reason="the plan skips the migration")
        alarm = store.add_alarm(store.carrier_for_feature(fo_id)["id"],
                                "failing-children", 1, "two children have failed")

        packet = supervisor.build_evidence(store, _fo_subject(store, fo_id), alarm, CFG)
    finally:
        store.close()

    assert "# The feature" in packet
    assert f"{fo_id} [executing]" in packet
    assert children[0]["id"] in packet
    assert "2 — the parser" in packet
    assert "the parser was dropped on purpose" in packet
    assert "round 1: rejected — the plan skips the migration" in packet
    # The catch for a fall-through into the work-order branch
    assert "# The work order" not in packet


def test_a_twelve_child_tree_is_clipped_to_the_worst_children_and_says_how_many(
        started, project):
    """`worker_brief.CORE_BUDGET_CHARS`' pin, applied to the one section that scales with the feature"""
    started()
    store = ProjectStore(project)
    try:
        pad = "x" * 180
        children = (
            [{"status": "failed", "title": f"broken {i} {pad}"} for i in range(3)]
            + [{"status": "pending", "title": f"blocked {pad}",
                "depends_on": []}]
            + [{"status": "running", "title": f"in flight {pad}"}]
            + [{"status": "completed", "title": f"done {i} {pad}"} for i in range(7)]
        )
        fo_id = _feature(store, children=children)
        store.update_work_order(children[3]["id"],
                                depends_on=db.to_json([children[0]["id"]]))
        alarm = store.add_alarm(store.carrier_for_feature(fo_id)["id"],
                                "failing-children", 1, "three children have failed")

        packet = supervisor.build_evidence(store, _fo_subject(store, fo_id), alarm, CFG)
    finally:
        store.close()

    assert len(packet) < CFG.evidence_budget_chars
    assert len(packet) > 1000
    tree = packet.split("# The child tree\n", 1)[1].split("\n\n", 1)[0]
    assert tree.endswith("…and 7 further children, all completed")
    assert "and 7 further children" in packet
    # Worst first is the whole point: the three failures survive the clip and a completed child does not
    for failed in children[:3]:
        assert failed["id"] in packet
    assert children[-1]["id"] not in packet


def test_a_feature_with_no_carrier_says_so_rather_than_raising(started, project):
    started()
    store = ProjectStore(project)
    try:
        fo_id = _feature(store, children=[], carrier=False)
        wo = ops.create_work_order("proj_a", "an unrelated order")
        alarm = ProjectStore(ops.find_work_order(wo["id"])[1]).add_alarm(
            wo["id"], "stalled-feature", 1, "nothing has moved for a week")
        packet = supervisor.build_evidence(store, _fo_subject(store, fo_id), alarm, CFG)
    finally:
        store.close()

    assert "# The feature" in packet
    assert supervisor.NO_CARRIER_LINE in packet
    assert "(this feature has no children yet)" in packet


def test_the_transcript_is_read_for_a_work_order_and_never_for_a_feature(
        started, project, monkeypatch, tmp_path):
    """A feature has no session"""
    from jarvis import inspection

    daemon = started()
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    reads: list[str] = []
    real = inspection.read_session
    monkeypatch.setattr(
        inspection, "read_session",
        lambda sid, cfg=None, **kw: (reads.append(sid), real(sid, cfg, **kw))[1])

    inspect_cfg = daemon.catalog.projects[0].inspect
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "the long one")
        _fixed_transcript(root / "-proj" / f"{wo['id']}.jsonl")
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        alarm = store.add_alarm(wo["id"], "long-turn", 1, "still going")
        supervisor.build_evidence(store, _wo_subject(store, wo["id"]), alarm, CFG,
                                  inspect_cfg)
        assert reads == [wo["id"]]

        fo_id = _feature(store, children=[{"status": "running", "title": "a child"}])
        supervisor.build_evidence(store, _fo_subject(store, fo_id), alarm, CFG,
                                  inspect_cfg)
    finally:
        store.close()

    assert reads == [wo["id"]]


def test_a_subject_level_finding_is_raised_on_no_particular_turn(started, project):
    """`NO_TURN` is `-1` and reaches the supervisor's OWN prompt"""
    from jarvis.project_store import NO_TURN

    started()
    store = ProjectStore(project)
    try:
        wo = ops.create_work_order("proj_a", "the carrier")
        finding = store.add_alarm(wo["id"], "failing-children", NO_TURN, "a symptom")
        cost = store.add_alarm(wo["id"], "long-turn", 1, "a long turn")
        subject = _wo_subject(store, wo["id"])
        health_packet = supervisor.build_evidence(store, subject, finding, CFG)
        cost_packet = supervisor.build_evidence(store, subject, cost, CFG)
    finally:
        store.close()

    assert "raised on no particular turn" in health_packet
    assert "turn -1" not in health_packet
    assert "raised on turn 1," in cost_packet


def test_the_system_prompt_is_byte_stable_across_reviews(jarvis_home):
    """Consecutive reviews share a cached prompt prefix"""
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
    """Without it `jarvis cost` prints the bare kind"""
    from jarvis import agent_usage

    assert "supervisor" in agent_usage.KIND_LABELS
    assert agent_usage.KIND_LABELS["supervisor"] != "supervisor"


# -- what the supervisor may never do, pinned in code


def test_the_supervisor_never_names_a_command_that_acts_on_a_work_order():
    """THE VERDICT VOCABULARY IS `{ack, escalate}`, AND THIS IS WHERE THAT LIVES"""
    tree = ast.parse(Path(supervisor.__file__).read_text())

    forbidden = {"cancel", "cancel_work_order", "set_status", "send_message",
                 "queue_message"}
    named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    named |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not (named & forbidden), sorted(named & forbidden)

    # `worker_session` is the machinery that LAUNCHES AND KILLS turns, and none of it belongs here (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §2)
    allowed = {"RATE_LIMIT_FALLBACK_DELAY"}
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [a.name for a in node.names] + [node.module or ""]
                 if isinstance(node, ast.ImportFrom) else [])
        if any("worker_session" in name.split(".") for name in names):
            assert isinstance(node, ast.ImportFrom), ast.dump(node)
            assert {a.name for a in node.names} <= allowed, [a.name for a in node.names]


def test_the_pin_would_catch_the_move_it_forbids():
    """A guard nobody has ever seen fail is a guard nobody knows works"""
    tree = ast.parse("from . import ops\ndef go(wo):\n    ops.cancel_work_order(wo)\n")
    named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "cancel_work_order" in named
