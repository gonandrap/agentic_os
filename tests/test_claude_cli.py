"""Isolation plumbing for one-shot headless calls.

`run_headless` spawns a real `claude -p`, which by default is a *fully tooled* session
that inherits the working directory: it can read the repo, load its CLAUDE.md as project
instructions, and shell out. That is right for Neo answering a question and wrong for a
persona eval, where the subject must reason from the prompt alone.

Both levers are tested here because both are load-bearing and neither is obvious from the
call site.
"""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

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
    """REPLACING, not appending: the caller's persona instead of the CLI's default
    context. Spec: docs/superpowers/specs/2026-09-25-a-headless-call-that-starts-from-nothing.md
    """
    claude_cli.run_headless("hi", cwd=tmp_path, system_prompt="be brief")
    argv = _argv(fake_claude)
    assert argv[argv.index("--system-prompt") + 1] == "be brief"
    assert "--append-system-prompt" not in argv
    assert "--append-system-prompt-file" not in argv


def test_sessions_are_never_persisted(fake_claude, tmp_path) -> None:
    """Nothing in `src/` reads `HeadlessResult.session_id`, tooled or not."""
    claude_cli.run_headless("hi", cwd=tmp_path, tools="")
    assert "--no-session-persistence" in _argv(fake_claude)
    claude_cli.run_headless("hi", cwd=tmp_path)
    assert "--no-session-persistence" in _argv(fake_claude)


def test_a_prompt_only_call_loses_settings_mcp_and_skills(fake_claude, tmp_path) -> None:
    """`tools=""` means judge the prompt and only the prompt, so the user's settings
    sources (hence hooks and plugins), MCP servers and skills all go."""
    claude_cli.run_headless("hi", cwd=tmp_path, tools="")
    argv = _argv(fake_claude)
    assert "--strict-mcp-config" in argv
    assert not Path(argv[argv.index("--mcp-config") + 1]).exists(), "temp file cleaned up"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in argv


def test_the_mcp_config_the_prompt_only_call_names_has_no_servers() -> None:
    with claude_cli._empty_mcp_config() as flags:
        assert json.loads(Path(flags[1]).read_text()) == {"mcpServers": {}}


def test_a_tooled_call_keeps_settings_mcp_and_skills(fake_claude, tmp_path) -> None:
    """A tooled callee runs under the permissions the setting sources supply: stripping
    them changes what it is ALLOWED to do, not what it reads (Neo's carve-out, q670)."""
    for tools in (None, "Read,Bash"):
        claude_cli.run_headless("hi", cwd=tmp_path, tools=tools)
        argv = _argv(fake_claude)
        assert "--mcp-config" not in argv
        assert "--setting-sources" not in argv
        assert "--disable-slash-commands" not in argv


def test_an_oversize_system_prompt_is_split_and_arrives_whole(fake_claude, tmp_path) -> None:
    """CLI 2.1.282 has no `--system-prompt-file`, so the caller's OWN bytes split across
    `--system-prompt` and `--append-system-prompt-file`. No stub: measured, a stub that
    talks about the prompt reads to the model as an injection attempt."""
    big = "é" * claude_cli.SYSTEM_PROMPT_ARGV_LIMIT + "\nTHE_PREFIX_MARKER"

    claude_cli.run_headless("hi", cwd=tmp_path, system_prompt=big)

    argv = _argv(fake_claude)
    assert "--append-system-prompt" not in argv
    head = argv[argv.index("--system-prompt") + 1]
    assert 0 < len(head.encode()) <= claude_cli.SYSTEM_PROMPT_ARGV_LIMIT
    written = Path(argv[argv.index("--append-system-prompt-file") + 1])
    assert not written.exists(), "the temporary file is cleaned up after the call"
    assert fake_claude.calls[-1]["system_prompt_seen"] == big


def test_the_environment_escape_hatch_appends_and_takes_a_settings_file(
        fake_claude, tmp_path) -> None:
    """Only `evals/llm/test_navigation_judgment.py`, whose SUBJECT is the default
    environment, may keep it."""
    settings = tmp_path / "eval-settings.json"
    settings.write_text("{}")
    claude_cli.run_headless("hi", cwd=tmp_path, system_prompt="be brief",
                            settings=settings, keep_default_context=True)
    argv = _argv(fake_claude)
    assert argv[argv.index("--append-system-prompt") + 1] == "be brief"
    assert "--system-prompt" not in argv
    assert argv[argv.index("--settings") + 1] == str(settings)


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


def test_the_fake_records_a_split_system_prompt_whole(fake_claude, tmp_path) -> None:
    """Both flag families arrive together on the oversize path, so the recorder has to
    CONCATENATE in CLI order; assigning per family read every split prompt back as one
    half. Spec: docs/superpowers/specs/2026-09-25-a-headless-call-that-starts-from-nothing.md
    """
    tail = tmp_path / "tail.md"
    tail.write_text("TAIL")
    subprocess.run([claude_cli.claude_bin(), "-p", "hi", "--output-format", "json",
                    "--system-prompt", "HEAD", "--append-system-prompt-file", str(tail)],
                   cwd=tmp_path, check=True, capture_output=True)
    assert fake_claude.calls[-1]["system_prompt_seen"] == "HEADTAIL"

# -- the USER prompt's second door -----------------------------------------------------
# docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md


#: Past `MAX_ARG_STRLEN` itself, not merely past `PROMPT_ARGV_LIMIT`: the prompts that
#: motivated this (a 151.7K `auto_review` confirmation) are on the far side of the real
#: `execve` ceiling, and a test built at the door's own threshold would not be.
OVER_EXECVE = 131_072 + 1


def test_a_user_prompt_past_the_argv_ceiling_goes_by_stdin(fake_claude, tmp_path) -> None:
    """errno 7 out of `execve` is not a CLI failure — there is no CLI yet to report it."""
    prompt = "x" * OVER_EXECVE + "\nTHE_USER_PROMPT_MARKER"

    claude_cli.run_headless(prompt, cwd=tmp_path)

    record = fake_claude.calls[-1]
    argv = record["argv"]
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "--output-format", (
        f"the prompt is still in argv; argv={argv[:4]}")
    assert prompt not in argv
    assert record["prompt"] == prompt


def test_a_multibyte_prompt_arrives_byte_identical_under_a_c_locale(
        fake_claude, monkeypatch, tmp_path) -> None:
    """`text=True` encodes stdin with the LOCALE encoding, so a daemon under `LANG=C`
    would raise `UnicodeEncodeError` on the first non-ASCII prompt to take this door."""
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("LC_ALL", "C")
    prompt = "héllo — ünïcode ✓\n" * 9000
    assert len(prompt.encode()) > OVER_EXECVE

    claude_cli.run_headless(prompt, cwd=tmp_path)

    assert fake_claude.calls[-1]["prompt"] == prompt


def test_the_stdin_door_is_explicitly_utf8(monkeypatch, tmp_path) -> None:
    """The pin for the line above, at the seam: locale-dependent encoding here is a
    fleet-wide failure that only shows on a host whose locale the suite does not have."""
    seen: dict = {}

    class _Done:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _capture(argv, **kwargs):
        seen.update(kwargs)
        return _Done()

    monkeypatch.setattr(claude_cli.subprocess, "run", _capture)
    prompt = "ü" * OVER_EXECVE

    claude_cli.run_headless(prompt, cwd=tmp_path)

    assert seen["encoding"] == "utf-8"
    assert seen["input"] == prompt


def test_a_small_user_prompt_still_rides_in_argv(fake_claude, tmp_path) -> None:
    """The no-change pin: ~30 test files read `argv[argv.index("-p") + 1]`, and the argv
    door is the one that survives a CLI build that reads its prompt differently."""
    claude_cli.run_headless("hi", cwd=tmp_path)
    argv = _argv(fake_claude)
    assert argv[argv.index("-p") + 1] == "hi"
    assert fake_claude.calls[-1]["prompt"] == "hi"


def test_a_worker_turn_past_the_ceiling_takes_stdin_from_a_prompt_file(
        fake_claude, tmp_path) -> None:
    """The dispatch brief is spec text plus a knowledge index and grows monotonically."""
    prompt = "y" * OVER_EXECVE + "\nTHE_BRIEF_MARKER"
    gate = fake_claude.hold_turns()
    outfile = tmp_path / "1.json"

    claude_cli.spawn_turn(prompt, cwd=tmp_path, session_id="s-big", outfile=outfile,
                          errfile=tmp_path / "1.err")

    promptfile = tmp_path / "1.prompt"
    assert promptfile.exists(), "the turn's prompt file must outlive spawn_turn"
    assert promptfile.read_text() == prompt
    record = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"])[-1]
    assert "--" not in record["argv"], (
        "no prompt in argv means nothing to fence; a bare -- is a token the fake would "
        f"have to special-case; argv={record['argv']}")
    assert prompt not in record["argv"]
    assert record["prompt"] == prompt
    gate.unlink()


def test_errno_e2big_is_deterministic_not_an_outage(monkeypatch, tmp_path) -> None:
    """A retry of a call `execve` refused is only a delay."""
    def _boom(argv, **kwargs):
        raise OSError(errno.E2BIG, "Argument list too long")

    monkeypatch.setattr(claude_cli.subprocess, "run", _boom)

    with pytest.raises(claude_cli.InputTooLargeError) as caught:
        claude_cli.run_headless("hi", cwd=tmp_path)

    assert isinstance(caught.value, claude_cli.ClaudeCliError)
    assert "too large" in str(caught.value)
    assert "not found" not in str(caught.value)


def test_any_other_oserror_is_still_classified(monkeypatch, tmp_path) -> None:
    """Today ANY `OSError` from `subprocess.run` escapes `claude_cli` unclassified, so
    `neo.drain_queue` never sees it and the whole drain tick is abandoned."""
    def _boom(argv, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(claude_cli.subprocess, "run", _boom)

    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        claude_cli.run_headless("hi", cwd=tmp_path)

    assert not isinstance(caught.value, claude_cli.InputTooLargeError)


# -- how big was the prompt we sent? ---------------------------------------------------


def test_the_input_size_lands_on_the_result_and_in_the_envelope(fake_claude,
                                                                tmp_path) -> None:
    """Measurement only, no cap. Spec §3,
    docs/superpowers/specs/2026-09-26-bounded-model-inputs.md.

    BOTH places, because both are read: `agent_usage.record` takes the sizes off a
    `HeadlessResult` when a caller hands it one, and off the plain `usage` dict when the
    caller passes `usage=result.usage` — which is what every seat, the panel and
    `worker_session` do.
    """
    result = claude_cli.run_headless_result("hello there", system_prompt="be brief",
                                            cwd=tmp_path)

    assert (result.prompt_chars, result.system_prompt_chars) == (11, 8)
    assert result.usage["prompt_chars"] == 11
    assert result.usage["system_prompt_chars"] == 8


def test_the_call_is_timed_and_the_latency_rides_on_both(fake_claude, tmp_path) -> None:
    """The only latency the OS records on a default fleet (the panel ships disabled), so
    it is measured here, one layer below every caller. Spec §3,
    docs/specs/2026-10-01-neo-observability.md.

    BOTH places for `prompt_chars`' reason: `agent_usage.record` reads the dataclass when
    a caller hands it one and the envelope when the caller passes `usage=result.usage`.
    """
    result = claude_cli.run_headless_result("hello there", cwd=tmp_path)

    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.usage["latency_ms"] == result.latency_ms


def test_no_system_prompt_measures_zero_not_none(fake_claude, tmp_path) -> None:
    """`len(system_prompt or "")`: a call with no system prompt sent no system prompt."""
    result = claude_cli.run_headless_result("hi", cwd=tmp_path)

    assert result.system_prompt_chars == 0 and result.prompt_chars == 2


def test_output_that_is_not_json_still_carries_the_sizes(fake_claude, monkeypatch,
                                                         tmp_path) -> None:
    """The fallback path builds its own `HeadlessResult` and would otherwise report 0 —
    the input was measured before the call, so nothing about the reply can unmeasure it.
    """
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **k: "not json at all")

    result = claude_cli.run_headless_result("hello there", system_prompt="be brief",
                                            cwd=tmp_path)

    assert result.usage is None                       # nothing to account
    assert (result.prompt_chars, result.system_prompt_chars) == (11, 8)
