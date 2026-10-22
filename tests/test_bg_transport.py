"""A worker conversation as ONE background session (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""

import json
import subprocess
import time

import pytest

from jarvis import claude_cli


def _wait_idle(session_id: str, timeout: float = 10.0) -> claude_cli.BgEntry:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = claude_cli.bg_roster().get(session_id)
        if entry is not None and entry.idle:
            return entry
        time.sleep(0.02)
    raise AssertionError(f"{session_id} never went idle")


# -- the primitives, against the fake's model of the measured CLI


def test_spawn_learns_the_session_id_the_cli_minted(fake_claude, tmp_path):
    entry = claude_cli.spawn_bg_turn("do the thing", tmp_path, "[WO wo-bg01] t",
                                     model="claude-fake-1")
    assert entry.id == entry.session_id[:8]
    assert entry.alive
    assert _wait_idle(entry.session_id).session_id == entry.session_id
    assert fake_claude.turns[entry.session_id] == ["do the thing"]


def test_spawn_never_passes_the_print_only_budget_flag(fake_claude, tmp_path):
    claude_cli.spawn_bg_turn("x", tmp_path, "n", max_budget_usd=3.0)
    argv = fake_claude.wait_calls(lambda c: "--bg" in c["argv"])[0]["argv"]
    assert "--max-budget-usd" not in argv


def test_resume_continues_the_same_session_bare(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n", model="claude-fake-1")
    _wait_idle(first.session_id)

    again = claude_cli.resume_bg_turn(first.session_id, "two", tmp_path)

    assert again.session_id == first.session_id
    _wait_idle(first.session_id)
    assert fake_claude.turns[first.session_id] == ["one", "two"]
    resumes = [c["argv"] for c in fake_claude.calls if "--resume" in c["argv"]]
    # BARE: any flag would have started a copy (fact 6)
    assert resumes == [["--bg", "--resume", first.session_id, "--", "two"]]
    # …and the live original was stopped first (fact 5)
    assert any(c["argv"][:1] == ["stop"] for c in fake_claude.calls)
    assert len([s for s in fake_claude.sessions if s.get("worker")]) == 1


def test_a_resume_that_forks_is_taken_down_and_refused(fake_claude, tmp_path, monkeypatch):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    # A stop that never takes: the original stays alive, so the resume would fork
    monkeypatch.setattr(claude_cli, "stop_bg", lambda short, wait=0: True)

    with pytest.raises(claude_cli.ClaudeCliError, match="started a copy"):
        claude_cli.resume_bg_turn(first.session_id, "two", tmp_path)

    assert [s["sessionId"] for s in fake_claude.sessions
            if s.get("worker")] == [first.session_id]


def test_respawn_keeps_the_id_and_sends_nothing(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    assert claude_cli.stop_bg(first.id)
    assert not claude_cli.bg_roster()[first.session_id].alive

    assert claude_cli.respawn_bg(first.id)

    back = claude_cli.bg_roster()[first.session_id]
    assert back.alive and back.idle
    assert fake_claude.turns[first.session_id] == ["one"]


def test_remove_deletes_the_session(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    assert claude_cli.remove_bg(first.id)
    assert first.session_id not in claude_cli.bg_roster()


def test_an_oversized_prompt_goes_by_file(fake_claude, tmp_path):
    big = "x" * (claude_cli.PROMPT_ARGV_LIMIT + 1)
    promptfile = tmp_path / "turn.prompt"
    claude_cli.spawn_bg_turn(big, tmp_path, "n", promptfile=promptfile)
    assert promptfile.read_text() == big
    argv = fake_claude.wait_calls(lambda c: "--bg" in c["argv"])[0]["argv"]
    assert str(promptfile) in argv[-1] and len(argv[-1]) < 1000


# -- the fake keeps faith with the measured CLI, or every test above proves nothing


def _claude(fake_claude, *args, cwd):
    return subprocess.run([str(fake_claude.dir / "claude"), *args], cwd=cwd,
                          capture_output=True, text=True, check=True)


def test_the_fake_forks_a_live_session_and_a_flagged_resume(fake_claude, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    live = _claude(fake_claude, "--bg", "--resume", first.session_id, "--", "x", cwd=tmp_path)
    assert "already running" in live.stderr
    claude_cli.stop_bg(first.id)
    flagged = _claude(fake_claude, "--bg", "--resume", first.session_id, "--model", "m",
                      "--", "y", cwd=tmp_path)
    assert "saved options" in flagged.stderr
    assert len({s["sessionId"] for s in fake_claude.sessions if s.get("worker")}) == 3


def test_the_fake_writes_a_transcript_jarvis_can_price(fake_claude, tmp_path):
    from jarvis import usage

    first = claude_cli.spawn_bg_turn("one", tmp_path, "n")
    _wait_idle(first.session_id)
    assert usage.cost_between(first.session_id, 0, time.time() + 60) > 0
    said = usage.said_in_session(first.session_id)
    assert said and said[-1][1] == "final: one"
    json.dumps(said)
