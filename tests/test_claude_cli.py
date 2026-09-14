"""Isolation plumbing for one-shot headless calls.

`run_headless` spawns a real `claude -p`, which by default is a *fully tooled* session
that inherits the working directory: it can read the repo, load its CLAUDE.md as project
instructions, and shell out. That is right for Neo answering a question and wrong for a
persona eval, where the subject must reason from the prompt alone.

Both levers are tested here because both are load-bearing and neither is obvious from the
call site.
"""

from __future__ import annotations

from pathlib import Path

from jarvis import claude_cli


def _argv(fake_claude) -> list[str]:
    assert fake_claude.calls, "the fake claude binary was never invoked"
    return fake_claude.calls[-1]["argv"]


def test_tools_flag_is_omitted_by_default(fake_claude, tmp_path) -> None:
    """Neo relies on the default: a normal session with its tools intact."""
    claude_cli.run_headless("hi", cwd=tmp_path)
    assert "--tools" not in _argv(fake_claude)


def test_tools_can_be_disabled_entirely(fake_claude, tmp_path) -> None:
    """`--tools ""` is the only lever that actually removes the tools.

    `--allowedTools`/`--disallowedTools` govern *permission*, not availability: under
    `permissions.defaultMode: auto` a subject passed `--disallowedTools Bash` still runs
    Bash. Verified against the real CLI before this was written.
    """
    claude_cli.run_headless("hi", cwd=tmp_path, tools="")
    argv = _argv(fake_claude)
    assert "--tools" in argv, "tools='' must reach the CLI as an explicit --tools flag"
    assert argv[argv.index("--tools") + 1] == ""


def test_stripping_tools_also_strips_the_mcp_servers(fake_claude, tmp_path) -> None:
    """`--tools ""` removes the BUILT-INS and leaves every configured MCP server's
    schemas in the request — measured: a seat-shaped call on a machine with the Google
    Drive connector listed eleven of its verbs when asked what tools it had. "Judges the
    prompt and only the prompt" needs both flags, and neither is a caller's to remember.
    Spec §4.
    """
    claude_cli.run_headless("hi", cwd=tmp_path, tools="")
    assert "--strict-mcp-config" in _argv(fake_claude)


def test_a_tooled_call_keeps_its_mcp_servers(fake_claude, tmp_path) -> None:
    """The negative half: Neo and Neo's panel pass `tools=None` and must not lose
    Serena. Without this assertion the line above would pass just as well if it stripped
    MCP from every headless call in the OS."""
    claude_cli.run_headless("hi", cwd=tmp_path)
    assert "--strict-mcp-config" not in _argv(fake_claude)
    claude_cli.run_headless("hi", cwd=tmp_path, tools="Read,Bash")
    assert "--strict-mcp-config" not in _argv(fake_claude)


def test_named_tools_are_passed_through(fake_claude, tmp_path) -> None:
    claude_cli.run_headless("hi", cwd=tmp_path, tools="Read,Bash")
    argv = _argv(fake_claude)
    assert argv[argv.index("--tools") + 1] == "Read,Bash"


def test_a_small_system_prompt_rides_in_argv(fake_claude, tmp_path) -> None:
    claude_cli.run_headless("hi", cwd=tmp_path, system_prompt="be brief")
    argv = _argv(fake_claude)
    assert argv[argv.index("--append-system-prompt") + 1] == "be brief"
    assert "--append-system-prompt-file" not in argv


def test_a_system_prompt_past_the_argv_ceiling_goes_by_file(fake_claude, tmp_path) -> None:
    """`MAX_ARG_STRLEN` is 128 KiB PER ARGUMENT whatever `ARG_MAX` says, and crossing it
    is an `OSError` out of `execve` that no CLI can report. The panel's packet is on the
    wrong side of that line at `diff_chars=150000` (spec §5, §6), so this is the door it
    goes through — and the fake proves the text arrives, not merely that a flag was
    passed.
    """
    big = "x" * (claude_cli.SYSTEM_PROMPT_ARGV_LIMIT + 1) + "\nTHE_PREFIX_MARKER"

    claude_cli.run_headless("hi", cwd=tmp_path, system_prompt=big)

    argv = _argv(fake_claude)
    assert "--append-system-prompt" not in argv
    written = Path(argv[argv.index("--append-system-prompt-file") + 1])
    assert not written.exists(), "the temporary file is cleaned up after the call"
    assert "THE_PREFIX_MARKER" in fake_claude.calls[-1]["system_prompt_seen"]


def test_call_runs_in_the_requested_directory(fake_claude, tmp_path) -> None:
    """cwd decides which CLAUDE.md the subject picks up — see neo.answer_question."""
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    claude_cli.run_headless("hi", cwd=neutral)
    assert fake_claude.calls[-1]["cwd"] == str(neutral.resolve())
