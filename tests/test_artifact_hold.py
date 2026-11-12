"""An artifact-owning order's session stays live (docs/superpowers/specs/2026-11-10-artifact-session-hold.md §4)"""

import json

import pytest

from jarvis import claude_cli, hooks, invariants, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.project_store import ProjectStore

URL = "https://claude.ai/artifact/AbCdEf123"


@pytest.fixture()
def bg_fleet(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    data = json.loads(catalog_file.read_text())
    data["os"]["defaults"]["transport"] = "background"
    catalog_file.write_text(json.dumps(data))
    monkeypatch.setattr(worker_session, "BG_DEAD_GRACE", 0.0)
    monkeypatch.setattr(worker_session, "BG_BUDGET_EVERY", 0.0)
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    from jarvis.daemon import Daemon

    return {"project": catalog.projects[0], "store": ProjectStore(project),
            "daemon": Daemon(catalog)}


def _settled(bg_fleet, settle_turns, fake_claude, *, artifact=True, via_subagent=False,
             session_id=None):
    """An order whose one turn published (or not) and has settled"""
    store, project = bg_fleet["store"], bg_fleet["project"]
    gate = fake_claude.hold_turns()
    wo = store.get_work_order(ops.create_work_order("proj_a", "report")["id"])
    worker_session.start(store, project, wo, "go")
    wo = store.get_work_order(wo["id"])
    art = None
    if artifact:
        art = store.record_artifact(wo["id"], URL, title="Report",
                                    session_id=session_id or wo["session_id"],
                                    via_subagent=via_subagent)
    gate.unlink()
    assert settle_turns(store)
    return store.get_work_order(wo["id"]), art


def _argv(fake_claude, verb):
    return [c for c in fake_claude.calls if c["argv"][:1] == [verb]]


def _hold(bg_fleet):
    bg_fleet["daemon"].hold_artifact_sessions(bg_fleet["project"], bg_fleet["store"])


# -- §4.1 keep it

def test_a_settled_turn_of_a_held_order_leaves_the_session_alive(bg_fleet, fake_claude,
                                                                   settle_turns):
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    assert not _argv(fake_claude, "stop")
    assert claude_cli.bg_roster()[wo["session_id"]].alive


def test_a_settled_turn_of_an_unheld_order_stops_it(bg_fleet, fake_claude, settle_turns):
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude, artifact=False)
    assert _argv(fake_claude, "stop")
    assert not claude_cli.bg_roster()[wo["session_id"]].alive


def test_a_completed_held_order_keeps_its_session_until_decided(bg_fleet, fake_claude,
                                                                  settle_turns):
    store = bg_fleet["store"]
    wo, art = _settled(bg_fleet, settle_turns, fake_claude)
    store.set_status(wo["id"], "completed")

    worker_session.poll(store)
    assert wo["session_id"] in claude_cli.bg_roster()

    store.set_artifact_decision(art["id"], "keep", by="user")
    worker_session.poll(store)
    assert wo["session_id"] not in claude_cli.bg_roster()


# -- §4.2 / §4.3 check and revive

def test_nothing_held_reads_no_roster(bg_fleet, fake_claude, settle_turns):
    _settled(bg_fleet, settle_turns, fake_claude, artifact=False)
    before = len(_argv(fake_claude, "agents"))
    _hold(bg_fleet)
    assert len(_argv(fake_claude, "agents")) == before


def test_a_live_held_session_is_left_alone(bg_fleet, fake_claude, settle_turns):
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    _hold(bg_fleet)
    assert not _argv(fake_claude, "respawn")
    assert not _argv(fake_claude, "stop")
    assert len(bg_fleet["store"].list_turns(wo["id"])) == 1


def test_a_dropped_held_session_is_revived_once_per_check(bg_fleet, fake_claude,
                                                           settle_turns):
    store = bg_fleet["store"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    fake_claude.drop(wo["session_id"])

    _hold(bg_fleet)
    assert len(_argv(fake_claude, "respawn")) == 1
    assert claude_cli.bg_roster()[wo["session_id"]].alive
    _hold(bg_fleet)
    assert len(_argv(fake_claude, "respawn")) == 1
    assert [e["kind"] for e in store.list_events(wo["id"], limit=200)].count(
        worker_session.ARTIFACT_REVIVED_EVENT) == 1
    assert len(store.list_turns(wo["id"])) == 1  # no turn sent to revive it


def test_three_failed_revives_raise_attention_and_stop_trying(bg_fleet, fake_claude,
                                                              settle_turns):
    store = bg_fleet["store"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    fake_claude.drop(wo["session_id"])
    fake_claude.respawn_fails()

    for _ in range(4):
        _hold(bg_fleet)

    assert len(_argv(fake_claude, "respawn")) == 3
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"]
    assert URL in fresh["attention_reason"]
    assert fresh["attention_reason"] in invariants.true_blockers(store, fresh)
    [violation] = [v for v in invariants.check_artifact_held(store)]
    assert violation.invariant == invariants.INV_ARTIFACT_HELD


def test_an_arm_turn_goes_once_for_a_subagent_publish(bg_fleet, fake_claude, settle_turns):
    store = bg_fleet["store"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude, via_subagent=True)

    _hold(bg_fleet)
    arm = store.latest_turn(wo["id"], include_artifact=True)
    assert arm["kind"] == worker_session.ARTIFACT_ARM_TURN
    assert URL in arm["prompt"] and "ArtifactComments" in arm["prompt"]
    assert settle_turns(store)
    _hold(bg_fleet)
    arms = [t for t in store.list_turns(wo["id"])
            if t["kind"] == worker_session.ARTIFACT_ARM_TURN]
    assert len(arms) == 1
    # Never the order's reply
    assert store.latest_turn(wo["id"])["kind"] == "dispatch"


def test_an_arm_turn_never_goes_while_the_session_is_busy(bg_fleet, fake_claude,
                                                          settle_turns):
    store = bg_fleet["store"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude, session_id="someone-else")
    fake_claude.wake(wo["session_id"], stay_busy=True)

    _hold(bg_fleet)
    assert not [t for t in store.list_turns(wo["id"])
                if t["kind"] == worker_session.ARTIFACT_ARM_TURN]


# -- §4.5 comment-woken turns

@pytest.mark.parametrize("status", ["waiting_pr_merge", "needs_review", "idle"])
def test_a_woken_session_gets_one_reply_turn_that_settles_nothing(
        bg_fleet, fake_claude, settle_turns, status):
    store, daemon, project = bg_fleet["store"], bg_fleet["daemon"], bg_fleet["project"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    store.set_status(wo["id"], status)
    daemon.settle_turns(project, store)  # whatever the sweep does with no reply is the baseline
    status = store.get_work_order(wo["id"])["status"]
    summary = store.get_work_order(wo["id"]).get("result_summary")
    fake_claude.wake(wo["session_id"], text="LFU means Least Frequently Used.",
                     stay_busy=True)

    _hold(bg_fleet)
    _hold(bg_fleet)
    replies = [t for t in store.list_turns(wo["id"])
               if t["kind"] == worker_session.ARTIFACT_REPLY_TURN]
    assert len(replies) == 1 and replies[0]["state"] == "running"
    assert not _argv(fake_claude, "stop")

    fake_claude.settle_wake(wo["session_id"])
    daemon.settle_turns(project, store)

    reply = store.get_turn(replies[0]["id"])
    assert reply["state"] == "done"
    assert reply["cost_usd"] and reply["cost_usd"] > 0
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == status
    assert fresh.get("result_summary") == summary
    assert any(e["kind"] == "artifact_reply" for e in store.list_events(wo["id"], limit=200))
    assert claude_cli.bg_roster()[wo["session_id"]].alive  # held: not stopped


def test_a_reply_over_its_cap_is_stopped_and_raises_attention(bg_fleet, fake_claude,
                                                              settle_turns):
    store = bg_fleet["store"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    store.set_status(wo["id"], "needs_review")
    fake_claude.wake(wo["session_id"], cost=1.50, stay_busy=True)
    _hold(bg_fleet)

    worker_session.poll(store)

    assert _argv(fake_claude, "stop")
    reply = store.latest_turn(wo["id"], include_artifact=True)
    assert reply["state"] == "failed" and "maximum budget" in reply["error"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert fresh["needs_attention"] and URL in fresh["attention_reason"]
    assert fresh["attention_reason"] in invariants.true_blockers(store, fresh)


def test_catalog_parses_the_reply_cap():
    from jarvis.catalog import CatalogError, parse_catalog

    base = {"os": {}, "projects": []}
    assert parse_catalog(base).os.artifacts.reply_budget_usd == 1.00
    base["os"]["artifacts"] = {"reply_budget_usd": 0.25}
    assert parse_catalog(base).os.artifacts.reply_budget_usd == 0.25
    base["os"]["artifacts"] = {"reply_budget_usd": 0}
    with pytest.raises(CatalogError, match="reply_budget_usd must be a number > 0"):
        parse_catalog(base)


def test_a_queued_message_waits_while_a_reply_runs(bg_fleet, fake_claude, settle_turns):
    store = bg_fleet["store"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    fake_claude.wake(wo["session_id"], stay_busy=True)

    # Before the hold check records it, the roster alone holds delivery
    hold = worker_session.delivery_hold(store, store.get_work_order(wo["id"]))
    assert hold is not None and hold.kind == worker_session.HOLD_TURN_IN_FLIGHT
    _hold(bg_fleet)
    hold = worker_session.delivery_hold(store, store.get_work_order(wo["id"]))
    assert hold is not None and hold.kind == worker_session.HOLD_TURN_IN_FLIGHT


# -- §4.5.5 the fence

FENCED = [("Bash", {"command": "jarvis wo finish x --summary y"}), ("Bash", {"command": "ls"}),
          ("Edit", {"file_path": "/tmp/x", "old_string": "a", "new_string": "b"}),
          ("Write", {"file_path": "/tmp/x", "content": "a"}), ("Read", {"file_path": "/tmp/x"}),
          ("Agent", {"prompt": "go"}), ("Artifact", {"file_path": "/tmp/x.html"}),
          ("mcp__x__y", {})]
ALLOWED = [("ArtifactComments", {"action": "read"}), ("ToolSearch", {"query": "x"})]


def _decide(project, wo, tool, tool_input):
    payload = {"tool_name": tool, "tool_input": tool_input, "session_id": wo["session_id"],
               "cwd": str(project.path)}
    return hooks.preflight_decision(payload, {"JARVIS_PROJECT_PATH": str(project.path)})


def _denied_by_fence(decision):
    return decision is not None and hooks.COMMENT_FENCE_REASON in json.dumps(decision)


@pytest.mark.parametrize("tool,tool_input", FENCED)
def test_the_fence_denies_everything_else_in_a_held_session(bg_fleet, fake_claude,
                                                            settle_turns, tool, tool_input):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    # No OS turn running: the seconds before the reply is recorded
    assert _denied_by_fence(_decide(project, wo, tool, tool_input))
    # ...and during the recorded reply
    fake_claude.wake(wo["session_id"], stay_busy=True)
    _hold(bg_fleet)
    assert store.latest_turn(wo["id"], include_artifact=True)["kind"] == "artifact_reply"
    assert _denied_by_fence(_decide(project, wo, tool, tool_input))


@pytest.mark.parametrize("tool,tool_input", ALLOWED)
def test_the_fence_allows_artifact_comments(bg_fleet, fake_claude, settle_turns, tool,
                                            tool_input):
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    assert not _denied_by_fence(_decide(bg_fleet["project"], wo, tool, tool_input))


@pytest.mark.parametrize("tool,tool_input", FENCED)
def test_an_os_started_turn_of_a_held_order_is_not_fenced(bg_fleet, fake_claude,
                                                          settle_turns, tool, tool_input):
    store, project = bg_fleet["store"], bg_fleet["project"]
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude)
    gate = fake_claude.hold_turns()
    worker_session.send(store, project, store.get_work_order(wo["id"]), "more")
    try:
        assert not _denied_by_fence(_decide(project, wo, tool, tool_input))
    finally:
        gate.unlink()


@pytest.mark.parametrize("tool,matched", [
    ("Read", True), ("Grep", True), ("WebFetch", True), ("SendMessage", True),
    ("mcp__x__y", True), ("mcp__plugin_serena_serena__find_symbol", True),
    ("mcp__serena__replace_symbol_body", True), ("ArtifactComments", False),
    ("ToolSearch", False), ("mcp__plugin_serena_serena__activate_project", False)])
def test_the_hook_matcher_reaches_every_fenced_tool(tool, matched):
    import re
    from pathlib import Path

    settings = json.loads(
        (Path(hooks.__file__).parent / "assets" / "settings.base.json").read_text())
    matcher = settings["hooks"]["PreToolUse"][0]["matcher"]
    assert bool(re.fullmatch(matcher, tool)) is matched


def test_an_unheld_order_is_not_fenced(bg_fleet, fake_claude, settle_turns):
    wo, _ = _settled(bg_fleet, settle_turns, fake_claude, artifact=False)
    assert not _denied_by_fence(_decide(bg_fleet["project"], wo, "Bash", {"command": "ls"}))
