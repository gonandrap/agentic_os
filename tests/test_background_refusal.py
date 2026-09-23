"""Backgrounding is refused at the tool call, not asked for in the briefing.

§4 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. The contract has
forbidden it in prose since TEMPLATE_VERSION v10; wo-2df8828c backgrounded anyway and
wo-d81fcc15 did it twice, the second time for 62 hours of wall clock (issue #575).
kn-6dcaf055 names the class: a rule the OS only asks for is not enforced.

The refusal is conditional on the DECLARED transport (§3), never on a hardcoded belief
that a turn is one-shot.
"""

from __future__ import annotations

from jarvis import claude_cli, hooks

HEADLESS = {"JARVIS_WO_ID": "wo-bg01",
            claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_HEADLESS}


def _bash(command: str = "", env=None, **tool_input):
    return hooks.background_task_decision(
        {"tool_name": "Bash", "tool_input": {"command": command, **tool_input}},
        HEADLESS if env is None else env)


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def test_run_in_background_denied_on_headless():
    result = _bash("pytest tests/", run_in_background=True)

    assert _decision(result) == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "FOREGROUND" in reason            # the correction
    assert "claude -p" in reason             # the reason, in the same line


def test_trailing_ampersand_denied():
    assert _decision(_bash("uv run pytest tests/ &")) == "deny"
    assert _decision(_bash("sleep 600 &  ")) == "deny"


def test_nohup_setsid_disown_denied():
    for command in ("nohup ./server.sh", "setsid ./server.sh", "./server.sh; disown"):
        assert _decision(_bash(command)) == "deny", command


def test_double_ampersand_and_redirects_allowed():
    """The whole difficulty of the shell case. Every one of these is a foreground
    command, and denying any of them would break the worker's ordinary shell."""
    for command in ("cd /tmp && pytest", "make 2>&1 | tee log", "cmd &> out.txt",
                    "cmd &>> out.txt", "a && b && c"):
        assert _bash(command) is None, command


def test_ampersand_inside_quotes_allowed():
    for command in ('grep "a & b" file', "echo 'run me &'", "pytest  # run this &"):
        assert _bash(command) is None, command


def test_a_backgrounding_word_as_an_argument_is_not_a_job():
    """In command position only. Denying `grep -r nohup src/` would make the rule
    something the worker learns to work around rather than obey."""
    for command in ("grep -r nohup src/", "rg setsid", "cat notes-about-disown.md"):
        assert _bash(command) is None, command


def test_allowed_when_transport_is_background():
    """`spawn_background` is supervisor-owned: the job outlives the call and the
    notification arrives. Hardcoding one-shot-ness would make this a lie."""
    env = {"JARVIS_WO_ID": "wo-bg01",
           claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_BACKGROUND}

    assert _bash("pytest &", env=env) is None
    assert _bash("pytest", env=env, run_in_background=True) is None


def test_no_op_without_transport_env():
    """Absent key = not a Jarvis worker, exactly as `JARVIS_WO_ID` behaves everywhere."""
    assert _bash("pytest &", env={}) is None


def test_subagent_call_denied_too():
    """PreToolUse fires for a subagent's calls under the parent session, carrying
    `agent_type` — so the seats are covered without a second mechanism."""
    result = hooks.background_task_decision(
        {"tool_name": "Bash", "agent_type": "jarvis-implementer",
         "tool_input": {"command": "pytest &"}}, HEADLESS)

    assert _decision(result) == "deny"


def test_the_refusal_is_reachable_around_by_nothing_the_auto_allow_covers():
    """Ordering: the check sits before the `jarvis …` auto-allow, the same trap the PR
    checks and the summary cap already sit in front of."""
    payload = {"tool_name": "Bash",
               "tool_input": {"command": "jarvis wo show wo-bg01 &"}}

    assert _decision(hooks.preflight_decision(payload, HEADLESS)) == "deny"
