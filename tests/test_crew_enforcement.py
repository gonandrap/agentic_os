"""The lead delegates because the tool call is refused, not because it was asked to.

§7 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. §6 without this
is prose, and prose is the problem the spec opens with.

The discriminator is the ABSENCE of `agent_type` on the payload (src/jarvis/hooks.py:514):
PreToolUse carries it for a seat's calls and omits it for the lead's own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import claude_cli, hooks


@pytest.fixture()
def worktree(tmp_path) -> Path:
    wt = tmp_path / "proj" / ".claude" / "worktrees" / "wo-crew01"
    (wt / "src").mkdir(parents=True)
    (wt / ".jarvis").mkdir()
    (wt / "specs").mkdir()
    return wt


def _env(**over) -> dict:
    return {"JARVIS_WO_ID": "wo-crew01",
            "JARVIS_WO_KIND": "worker",
            "JARVIS_REQUIRE_CREW": "1",
            claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_HEADLESS,
            **over}


def _payload(worktree: Path, path: str, tool: str = "Write", **over) -> dict:
    return {"tool_name": tool, "cwd": str(worktree),
            "tool_input": {"file_path": str(worktree / path), "content": "x = 1"},
            **over}


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_lead_write_inside_worktree_denied(worktree):
    result = hooks.crew_edit_decision(_payload(worktree, "src/app.py"), _env())

    assert _decision(result) == "deny"
    assert "jarvis-implementer" in _reason(result)


def test_lead_edit_inside_worktree_denied(worktree):
    result = hooks.crew_edit_decision(
        _payload(worktree, "specs/thing.md", tool="Edit"), _env())

    assert _decision(result) == "deny"
    assert "jarvis-spec-writer" in _reason(result)   # a spec goes to the other seat


def test_seat_write_allowed(worktree):
    """The whole point: the seat the lead delegated to must be able to write."""
    payload = _payload(worktree, "src/app.py", agent_type="jarvis-implementer")

    assert hooks.crew_edit_decision(payload, _env()) is None


def test_dot_jarvis_path_exempt(worktree):
    """Generated state the lead owns, not code a seat should be writing."""
    assert hooks.crew_edit_decision(
        _payload(worktree, ".jarvis/notes.json"), _env()) is None


def test_path_outside_worktree_not_this_rule(worktree, tmp_path):
    payload = {"tool_name": "Write", "cwd": str(worktree),
               "tool_input": {"file_path": str(tmp_path / "elsewhere.py"),
                              "content": "x = 1"}}

    assert hooks.crew_edit_decision(payload, _env()) is None


def test_require_crew_false_disables(worktree):
    """A project turns it off with `jarvis config set <p> worker.require_crew false`
    and does not wait for a release."""
    assert hooks.crew_edit_decision(
        _payload(worktree, "src/app.py"), _env(JARVIS_REQUIRE_CREW="0")) is None


def test_planner_kind_unaffected(worktree):
    """A planner has its own team prose and no crew; the rule is the worker's."""
    assert hooks.crew_edit_decision(
        _payload(worktree, "src/app.py"), _env(JARVIS_WO_KIND="planner")) is None


def test_no_op_without_transport_env(worktree):
    assert hooks.crew_edit_decision(
        _payload(worktree, "src/app.py"),
        {"JARVIS_WO_ID": "wo-crew01", "JARVIS_WO_KIND": "worker",
         "JARVIS_REQUIRE_CREW": "1"}) is None


def test_under_review_narrowing_still_wins(worktree, monkeypatch):
    """A narrowed session is a STRICTER state: the gate's reason has to be the one the
    worker reads, or it argues with the wrong rule."""
    monkeypatch.setattr(hooks, "under_review_decision",
                        lambda payload, env: hooks._deny("under review"))

    result = hooks.preflight_decision(_payload(worktree, "src/app.py"), _env())

    assert _reason(result) == "under review"


def test_the_deny_reaches_the_lead_through_preflight(worktree):
    result = hooks.preflight_decision(_payload(worktree, "src/app.py"), _env())

    assert _decision(result) == "deny"
