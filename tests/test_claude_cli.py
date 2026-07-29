"""Isolation plumbing for one-shot headless calls.

`run_headless` spawns a real `claude -p`, which by default is a *fully tooled* session
that inherits the working directory: it can read the repo, load its CLAUDE.md as project
instructions, and shell out. That is right for Neo answering a question and wrong for a
persona eval, where the subject must reason from the prompt alone.

Both levers are tested here because both are load-bearing and neither is obvious from the
call site.
"""

from __future__ import annotations

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


def test_named_tools_are_passed_through(fake_claude, tmp_path) -> None:
    claude_cli.run_headless("hi", cwd=tmp_path, tools="Read,Bash")
    argv = _argv(fake_claude)
    assert argv[argv.index("--tools") + 1] == "Read,Bash"


def test_call_runs_in_the_requested_directory(fake_claude, tmp_path) -> None:
    """cwd decides which CLAUDE.md the subject picks up — see neo.answer_question."""
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    claude_cli.run_headless("hi", cwd=neutral)
    assert fake_claude.calls[-1]["cwd"] == str(neutral.resolve())
