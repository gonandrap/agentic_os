"""`jarvis wo why` — the diagnosis report (docs/superpowers/specs/2026-09-24-order-observability.md)"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jarvis import agent_usage, cli, db, invariants, ops, usage
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def validating_catalog(tmp_path, project):
    """The one fleet shape where a round can be forced: the panel is ON"""
    data = {
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "validation": {"enabled": True},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "validating-catalog.json"
    path.write_text(json.dumps(data))
    return path


def an_order(daemon, title: str = "build the exporter") -> dict:
    """A dispatched work order — one tick, one worker"""
    wo = ops.create_work_order("proj_a", title)
    daemon.tick()
    return wo


def at(monkeypatch, ts: float):
    """Write the next store rows as if the clock read `ts`"""
    monkeypatch.setattr(db, "now", lambda: ts)


# -- 1. the blocker and the status label agree


def test_assumptions_blocker_agrees_with_the_user_blockers(started, project):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default (fixture decision)")

    d = ops.diagnose(wo["id"])

    assert d["wo_id"] == wo["id"]
    assert d["project"] == "proj_a"
    assert d["blocker"]["what"] == "assumptions"
    assert d["needs_you"] and "assumption" in d["needs_you"][0].lower()
    # The point of the report: on an order where both sources can see the same fact they say the same...
    assert d["disagreements"] == []
    assert d["status_label"]


# -- 2. where two disagree, both are reported


def test_disagreement_reported_when_the_blocker_does_not_need_the_user(started,
                                                                      monkeypatch):
    """`true_blockers` wants the user; `waiting_on` says a turn is in flight"""
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default (fixture decision)")
    monkeypatch.setattr(ops, "waiting_on", lambda store, w, now=None: {
        "what": "turn_running", "stalled": False,
        "detail": "a turn is in flight — the worker is working"})

    d = ops.diagnose(wo["id"])

    assert len(d["disagreements"]) == 1
    sentence = d["disagreements"][0]
    assert "waiting_on" in sentence and "true_blockers" in sentence
    assert "turn_running" in sentence
    # Neither answer is dropped and neither is crowned
    assert d["blocker"]["what"] == "turn_running"
    assert d["needs_you"]


def test_disagreement_reported_when_nothing_needs_the_user(started, monkeypatch):
    """The other direction: `waiting_on` names a user wait `true_blockers` does not"""
    wo = an_order(started)
    monkeypatch.setattr(ops, "waiting_on", lambda store, w, now=None: {
        "what": "assumptions", "stalled": False,
        "detail": f"1 assumption(s) await your review — `jarvis wo review {wo['id']}`"})
    monkeypatch.setattr(invariants, "true_blockers", lambda store, w, now=None: [])

    d = ops.diagnose(wo["id"])

    assert len(d["disagreements"]) == 1
    sentence = d["disagreements"][0]
    assert "waiting_on" in sentence and "true_blockers" in sentence
    assert "assumptions" in sentence
    assert d["needs_you"] == []


# -- 3. absent is never zero


def test_an_order_with_no_turn_reports_none_and_says_so(started, project):
    wo = ops.create_work_order("proj_a", "not dispatched yet")  # no tick: no worker

    d = ops.diagnose(wo["id"])

    assert d["clock"]["last_turn"] is None
    assert d["clock"]["last_turn"] != 0
    assert d["clock"]["turn_open"] is False
    assert "note" in d["clock"] and d["clock"]["note"]
    # And the report says out loud that it read the record rather than the process
    assert "jarvis watch" in d["clock"]["live_note"]


# -- 4. no transcript means the residual is unmeasurable, not nil


def test_missing_transcript_leaves_the_residual_none(started):
    wo = ops.create_work_order("proj_a", "never dispatched, never transcribed")

    d = ops.diagnose(wo["id"])

    residual = d["holds"]["unexplained"]
    assert residual["seconds"] is None
    assert residual["seconds"] != 0
    assert residual["residual"] is True
    assert "transcript" in residual["note"]


# -- 5. a call that never reached the model decided nothing


#: Words that would turn an unreachable call into a judgement it never made
JUDGEMENT_WORDS = ("decision", "decided", "escalat", "refus", "verdict", "answered it")


def test_a_failed_os_call_is_reported_as_unreachable(started):
    wo = an_order(started)
    agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                       label="should this export CSV", model="sonnet", ok=False)
    agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                       label="should this export CSV", model="sonnet", ok=True)

    calls = ops.diagnose(wo["id"])["os_calls"]

    assert calls["calls"] == 2 and calls["failed"] == 1
    outcomes = sorted(r["outcome"] for r in calls["rows"])
    assert outcomes == ["answered", "unreachable"]
    assert "unreachable" in calls["note"]
    unreachable = [r for r in calls["rows"] if r["outcome"] == "unreachable"]
    blob = json.dumps({"rows": unreachable, "note": calls["note"]}).lower()
    for word in JUDGEMENT_WORDS:
        assert word not in blob, word


# -- 6. only commands the OS would accept right now


def commands(d: dict) -> list[str]:
    return [c["command"] for c in d["commands"]]


def test_validation_force_is_withheld_with_its_refusal(started):
    wo = an_order(started)

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis validation force") for c in commands(d))
    assert d["refusals"] and any("force" in r for r in d["refusals"])


def test_validation_force_is_offered_when_it_would_be_accepted(jarvis_home, fake_claude,
                                                               validating_catalog,
                                                               project):
    ops.start_os(str(validating_catalog), foreground=True)
    wo = ops.create_work_order("proj_a", "delivered behind a pull request")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "waiting_pr_merge",
                         pr_url="https://example.invalid/pr/1")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert any(c.startswith(f"jarvis validation force {wo['id']}")
               for c in commands(d))
    assert not any("force" in r for r in d["refusals"])


def test_unblock_is_offered_bare_when_a_dependency_is_dead(started, project):
    """A stranded order: the default form cuts the dead edge, so it is the one offered"""
    first = ops.create_work_order("proj_a", "the thing it builds on")
    second = ops.create_work_order("proj_a", "the dependent", depends_on=[first["id"]])
    store = ProjectStore(project)
    try:
        store.set_status(first["id"], "cancelled")
    finally:
        store.close()

    d = ops.diagnose(second["id"])

    assert f"jarvis wo unblock {second['id']}" in commands(d)
    assert f"jarvis wo unblock {second['id']} --all" not in commands(d)


def test_unblock_needs_all_when_the_dependency_is_still_live(started, project):
    """A live edge: `unblock_work_order` refuses the bare form, so only --all shows"""
    first = ops.create_work_order("proj_a", "the thing it builds on")
    second = ops.create_work_order("proj_a", "the dependent", depends_on=[first["id"]])

    d = ops.diagnose(second["id"])

    assert f"jarvis wo unblock {second['id']} --all" in commands(d)
    assert f"jarvis wo unblock {second['id']}" not in commands(d)
    [why] = [c["why"] for c in d["commands"]
             if c["command"].endswith("--all")]
    assert "LIVE" in why


# -- 6b. a capped list is never presented as the whole of it


def test_os_calls_report_their_cap_and_the_counts_as_a_floor(started):
    wo = an_order(started)
    for _ in range(3):
        agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                           label="a question", model="sonnet", ok=True)

    calls = ops._diagnose_os_calls(wo["id"], limit=2, shown=1)

    assert calls["calls"] == 2 and calls["limit"] == 2 and calls["capped"] is True
    assert "capped at the newest 2 calls" in calls["note"]
    assert "floor" in calls["note"]
    # The human listing's arithmetic, done here so the renderer only slices
    assert calls["shown_limit"] == 1 and calls["more"] == 1


def test_an_uncapped_list_says_nothing_about_a_cap(started):
    wo = an_order(started)
    agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                       label="a question", model="sonnet", ok=True)

    calls = ops.diagnose(wo["id"])["os_calls"]

    assert calls["capped"] is False and calls["more"] == 0
    assert calls["limit"] == ops.OS_CALLS_LIMIT
    assert "capped" not in calls["note"]


def test_ack_is_withheld_while_assumptions_are_pending(started):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default (fixture decision)")

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis wo ack") for c in commands(d))
    # The command that IS the way through is the one `waiting_on` already names
    assert f"jarvis wo review {wo['id']}" in d["blocker"]["detail"]


# -- 7. the holds, with the open one marked


def test_holds_are_listed_and_by_cause_sums_to_them(started, project, monkeypatch):
    wo = ops.create_work_order("proj_a", "held twice")
    now = db.now()
    store = ProjectStore(project)
    try:
        at(monkeypatch, now - 600)
        store.add_event(wo["id"], "question_asked", {"neo_question_id": 7})
        at(monkeypatch, now - 300)
        store.add_event(wo["id"], "neo_answered", {"neo_question_id": 7})
        at(monkeypatch, now - 100)
        store.add_event(wo["id"], "gate_requested", {"approval_id": 3})
    finally:
        store.close()
    monkeypatch.setattr(db, "now", lambda: now)

    held = ops.diagnose(wo["id"])["holds"]

    assert len(held["episodes"]) == 2
    assert [e["cause"] for e in held["episodes"]] == ["neo_question", "gate"]
    assert held["open"] is not None and held["open"]["cause"] == "gate"
    assert [e for e in held["episodes"] if e["open"]] == [held["open"]]
    total = sum(e["seconds"] for e in held["episodes"])
    assert sum(held["by_cause"].values()) == pytest.approx(total, abs=2.0)
    # Biggest first, so a reader's eye lands on the hold that cost the afternoon
    assert list(held["by_cause"]) == ["neo_question", "gate"]


def test_the_report_closes_a_synthesised_hold_the_round_ended(started, project,
                                                              monkeypatch):
    """`jarvis wo why` is 2d1's reader: `_diagnose_holds` calls `holds.held` (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)"""
    from jarvis.holds import PAUSE_USAGE_LIMIT
    from jarvis.project_store import VALIDATION_HELD_CAUSE

    wo = ops.create_work_order("proj_a", "held, then judged")
    now = db.now()
    store = ProjectStore(project)
    try:
        at(monkeypatch, now - 7200)
        store.add_event(wo["id"], "validation_failed",
                        {"round": 1, "cause": VALIDATION_HELD_CAUSE})
        at(monkeypatch, now - 3600)
        store.add_event(wo["id"], "validation_passed", {"round": 1})
    finally:
        store.close()
    monkeypatch.setattr(db, "now", lambda: now)

    held = ops.diagnose(wo["id"])["holds"]

    assert held["open"] is None
    assert [e["cause"] for e in held["episodes"]] == [PAUSE_USAGE_LIMIT]
    assert held["episodes"][0]["seconds"] == pytest.approx(3600, abs=2.0)


# -- 8. a settled order diagnoses cleanly


def test_a_completed_order_says_how_it_settled(started, project, settle_turns):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        # The dispatch turn is a real process
        assert settle_turns(store)
        store.set_status(wo["id"], "completed")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert d["status"] == "completed"
    assert d["blocker"]["what"] == "completed"
    assert "nothing is running to nudge" in d["blocker"]["detail"]
    assert d["needs_you"] == []
    assert d["clock"]["last_status"]["status"] == "completed"
    assert not any(c.startswith("jarvis wo resume-auto") for c in commands(d))


# -- 8b. the scheduled actor rides on the debug surface


def test_the_diagnosis_carries_the_scheduled_actor(started, project):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        row = store.get_work_order(wo["id"])
        actor = ops.scheduled_actor(store, row)
        assert actor is not None, "the premise: this row HAS a daemon pass coming for it"
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert d["actor"] == {"pass_name": actor.pass_name, "why": actor.why,
                          "predicate": actor.predicate}


def test_a_row_with_no_actor_reports_the_key_present_and_none(started, project):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
        row = store.get_work_order(wo["id"])
        assert ops.scheduled_actor(store, row) is None, \
            "the premise: `needs_review` has no daemon pass — the user is the actor"
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    # Absent must not be how "no actor" is spelled (issue #227's rule)
    assert "actor" in d
    assert d["actor"] is None


def test_a_settled_order_reports_no_actor_rather_than_raising(started, project,
                                                              settle_turns):
    """`scheduled_actor` is keyed on OPEN_STATUSES and raises off them by design"""
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        assert settle_turns(store)
        store.set_status(wo["id"], "completed")
    finally:
        store.close()

    assert ops.diagnose(wo["id"])["actor"] is None


def test_cli_prints_the_actor_under_waiting_on(started, project, capsys):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        actor = ops.scheduled_actor(store, store.get_work_order(wo["id"]))
        assert actor is not None
    finally:
        store.close()

    assert cli.main(["wo", "why", wo["id"]]) == 0
    out = capsys.readouterr().out

    assert actor.pass_name in out and actor.predicate in out and actor.why in out
    assert out.index("waiting on:") < out.index(actor.pass_name)


def test_cli_says_when_no_daemon_pass_will_act(started, project, capsys):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    assert cli.main(["wo", "why", wo["id"]]) == 0

    assert "no daemon pass will act on this order" in capsys.readouterr().out


# -- 9. the CLI hands the dict over unchanged


def test_cli_json_round_trips_the_ops_dict(started, capsys):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default (fixture decision)")

    assert cli.main(["wo", "why", wo["id"], "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    fresh = ops.diagnose(wo["id"])

    assert set(printed) == set(fresh)
    for key in ("wo_id", "project", "title", "status", "blocker", "needs_you",
                "notes", "disagreements", "commands", "refusals"):
        assert printed[key] == fresh[key], key


def test_cli_human_rendering_names_the_blocker(started, capsys):
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default (fixture decision)")

    for _ in range(ops.OS_CALLS_SHOWN + 2):
        agent_usage.record("neo_answer", wo_id=wo["id"], project="proj_a",
                           label="a question", model="sonnet", ok=True)

    assert cli.main(["wo", "why", wo["id"]]) == 0
    out = capsys.readouterr().out

    assert wo["id"] in out
    assert f"jarvis wo review {wo['id']}" in out
    # The listing is cut at the payload's depth and the remainder comes from the payload too
    assert out.count("  answered  ") == ops.OS_CALLS_SHOWN
    assert "… and 2 older call(s)" in out


# -- 10. every command offered is one the OS would accept


def test_ack_is_offered_and_the_real_command_accepts_it(started, project):
    """The offer and the acceptance agree — one predicate, `ops.ack_refusal`"""
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        store.flag_attention(wo["id"], "worker session disappeared")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert f"jarvis wo ack {wo['id']}" in commands(d)
    assert not any("acknowledging would bury it" in r for r in d["refusals"])
    result = ops.ack_attention(wo["id"], project_name="proj_a")
    assert wo["id"] in result["acknowledged"]


def test_a_withheld_ack_is_explained(started, project):
    """§6.5: a command not offered says why, like a withheld `validation force`"""
    wo = an_order(started)
    ops.assume(wo["id"], "exporting CSV by default (fixture decision)")

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis wo ack") for c in commands(d))
    assert any("acknowledging would bury it" in r for r in d["refusals"])
    with pytest.raises(ops.OpsError, match="acknowledging would bury it"):
        ops.ack_attention(wo["id"], project_name="proj_a")


def test_no_attention_flag_means_no_ack_offer_and_no_refusal(started, project):
    """Nothing to ack and nothing to refuse either"""
    wo = ops.create_work_order("proj_a", "nobody flagged this")

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis wo ack") for c in commands(d))
    assert not any("acknowledging would bury it" in r for r in d["refusals"])


def test_resume_auto_is_offered_when_nothing_is_coming_by_itself(started, project):
    wo = ops.create_work_order("proj_a", "stalled on a prompt")  # no tick: no turn
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "waiting_input")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert d["blocker"]["what"] == "prompt" and d["blocker"]["stalled"] is True
    assert f"jarvis wo resume-auto {wo['id']}" in commands(d)


def test_resume_auto_is_withheld_from_an_idle_manager(started, project):
    """`manager_idle` is in `NUDGE_IS_WRONG`: the nudge buys one turn of nothing"""
    wo = ops.create_work_order("proj_a", "the feature's manager")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "idle")
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert d["blocker"]["what"] == "manager_idle"
    assert not any(c.startswith("jarvis wo resume-auto") for c in commands(d))


def test_resume_auto_is_withheld_when_a_nudge_is_actively_wrong(started, monkeypatch):
    """The `NUDGE_IS_WRONG` arm itself, which store state cannot reach"""
    wo = an_order(started)
    monkeypatch.setattr(ops, "waiting_on", lambda store, w, now=None: {
        "what": "message_stuck", "stalled": True,
        "detail": "a message is queued undelivered"})

    d = ops.diagnose(wo["id"])

    assert not any(c.startswith("jarvis wo resume-auto") for c in commands(d))


def test_an_escalated_gate_offers_both_verdicts(started, project):
    wo = an_order(started)
    store = ProjectStore(project)
    try:
        # The command text is never read by the report (kn-1791a5e6: a gate's command is a privileged line a...
        approval = store.add_approval(wo["id"], "release", "<the proposed command>")
        store.mark_approval_escalated(approval["id"], "Neo would not decide this one")
        assert store.escalated_approvals(wo["id"])
    finally:
        store.close()

    d = ops.diagnose(wo["id"])

    assert f'jarvis gate approve {approval["id"]} --reason "…"' in commands(d)
    assert f'jarvis gate deny {approval["id"]} --reason "…"' in commands(d)


# -- 11. a found transcript gives the residual a number


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _transcript(session: str, rows: list[dict]) -> str:
    """One session file where `usage.index_sessions` looks"""
    directory = Path(os.environ[usage.TRANSCRIPT_ROOT_ENV]) / "-proj"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{session}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))
    return session


def test_a_found_transcript_reports_a_residual_number(started, project):
    wo = ops.create_work_order("proj_a", "transcribed")
    session = _transcript("why-session", [
        {"type": "user", "timestamp": _stamp(1000), "promptSource": "sdk",
         "message": {"content": "go"}},
        {"type": "assistant", "timestamp": _stamp(1060),
         "message": {"id": "m1", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "text", "text": "ok"}]}},
    ])
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], session_id=session)
    finally:
        store.close()

    residual = ops.diagnose(wo["id"])["holds"]["unexplained"]

    assert residual["residual"] is True
    assert residual["seconds"] is not None and residual["seconds_human"]
    assert "transcript" not in residual["note"]


def test_a_live_turn_reports_the_clock_open(started):
    wo = an_order(started)  # the dispatch turn is a real process, still running

    d = ops.diagnose(wo["id"])

    assert d["clock"]["turn_open"] is True
    assert d["clock"]["last_turn"]["state"] == "running"


# -- 12. absent notes are absent, not blank


def test_an_order_with_no_notes_reports_all_three_as_none(started):
    wo = ops.create_work_order("proj_a", "nothing parked, nothing paused")

    notes = ops.diagnose(wo["id"])["notes"]

    assert notes == {"parked": None, "pause": None, "fleet_hold": None}


# -- 13. a failed order is offered the retry, and only a failed one


def _failed(started, project, session: bool = True) -> dict:
    wo = an_order(started) if session else ops.create_work_order("proj_a", "never opened")
    store = ProjectStore(project)
    try:
        # The turn DIED — without ending it the diagnosis is `turn_running`
        turn = store.latest_turn(wo["id"])
        if turn is not None:
            store.finish_turn(turn["id"], "failed")
        store.set_status(wo["id"], "failed")
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def test_a_failed_order_is_offered_the_retry_and_its_blocker_names_it(started, project):
    wo = _failed(started, project)

    d = ops.diagnose(wo["id"])

    assert d["blocker"]["what"] == "failed"
    assert d["blocker"]["stalled"] is False
    assert f"jarvis wo retry {wo['id']}" in d["blocker"]["detail"]
    # FIRST: on a failed order it is the only command that moves the work, and the list is read top down
    assert d["commands"][0]["command"] == f"jarvis wo retry {wo['id']}"
    assert not any(f"wo retry {wo['id']}" in r for r in d["refusals"])


def test_a_failed_order_with_no_session_reports_the_retry_as_refused(started, project):
    wo = _failed(started, project, session=False)

    d = ops.diagnose(wo["id"])

    assert not any("wo retry" in c["command"] for c in d["commands"])
    assert any("no session to relaunch" in r for r in d["refusals"])


def test_no_healthy_order_is_told_why_it_cannot_be_retried(started):
    """The `elif` guard: a running order does not want a line explaining a mechanism that does not apply..."""
    wo = an_order(started)

    d = ops.diagnose(wo["id"])

    assert not any("wo retry" in c["command"] for c in d["commands"])
    assert not any("wo retry" in r for r in d["refusals"])
