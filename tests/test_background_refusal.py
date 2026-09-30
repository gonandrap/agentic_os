"""Backgrounding is refused at the tool call, not asked for in the briefing.

§4 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md. The contract has
forbidden it in prose since TEMPLATE_VERSION v10; wo-2df8828c backgrounded anyway and
wo-d81fcc15 did it twice, the second time for 62 hours of wall clock (issue #575).
kn-6dcaf055 names the class: a rule the OS only asks for is not enforced.

The refusal is conditional on the DECLARED transport (§3), never on a hardcoded belief
that a turn is one-shot.

§3 and §4 of docs/superpowers/specs/2026-09-29-a-lead-must-not-block-past-its-cache.md
invert it for three shell shapes and one seat call: those are refused in the FOREGROUND
and permitted when backgrounded, because a call blocking past the 5-minute prompt cache
re-sends the whole conversation at the write rate (issue 868, ~$52 over 25 work orders).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from jarvis import claude_cli, hooks

HEADLESS = {"JARVIS_WO_ID": "wo-bg01",
            claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_HEADLESS}


def _bash(command: str = "", env=None, **tool_input):
    return hooks.background_task_decision(
        {"tool_name": "Bash", "tool_input": {"command": command, **tool_input}},
        HEADLESS if env is None else env)


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _fg(command: str = "", env=None, **tool_input):
    return hooks.long_foreground_decision(
        {"tool_name": "Bash", "tool_input": {"command": command, **tool_input}},
        HEADLESS if env is None else env)


def _seat(subagent_type: str, tool_name: str = "Agent", env=None, **tool_input):
    return hooks.long_foreground_decision(
        {"tool_name": tool_name,
         "tool_input": {"subagent_type": subagent_type, "prompt": "do it",
                        **tool_input}},
        HEADLESS if env is None else env)


def test_run_in_background_denied_on_headless():
    # `./server.sh` and not the suite: §4 of the 2026-09-29 spec carves the three long
    # shapes out, and what stays refused is the job nobody polls for.
    result = _bash("./server.sh", run_in_background=True)

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
    # Under `spawn_background` the gap is the supervisor's problem and §3 has no subject.
    assert _fg("uv run pytest tests/", env=env) is None
    assert _seat("jarvis-implementer", env=env) is None


def test_no_op_without_transport_env():
    """Absent key = not a Jarvis worker, exactly as `JARVIS_WO_ID` behaves everywhere."""
    assert _bash("pytest &", env={}) is None
    assert _fg("uv run pytest tests/", env={}) is None
    assert _seat("jarvis-implementer", env={}) is None


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


# -- §3: the foreground refusal, the other half of the same matcher --------------------


def test_foreground_whole_suite_run_denied():
    """The three spellings the fleet uses. A 20-minute blocking call is one 1.25x
    re-write of the whole conversation on the next call (kn-356c724b, issue 868)."""
    for command in ("uv run pytest tests/", "pytest", "python -m pytest tests/ evals/"):
        assert _decision(_fg(command)) == "deny", command


def test_targeted_test_run_allowed():
    """The false positives that would make the rule something a lead works around
    rather than obeys — kn-27fed9d2 item 7. Each of these is seconds."""
    for command in ("pytest tests/test_hooks.py",
                    "pytest tests/test_hooks.py::test_one",
                    "pytest --collect-only", "pytest --version",
                    "pytest -k expr tests/test_hooks.py"):
        assert _fg(command) is None, command


def test_foreground_ci_watch_denied():
    for command in ("gh run watch", "gh pr checks 42 --watch"):
        assert _decision(_fg(command)) == "deny", command
    assert "DO NOT WAIT FOR CI" in _reason(_fg("gh run watch"))


def test_ci_read_without_watch_allowed():
    for command in ("gh pr checks 42", "gh run view 7", "gh run list"):
        assert _fg(command) is None, command


def test_long_sleep_and_poll_loops_denied():
    """A loop's wall clock is not bounded by its sleep argument, however small."""
    for command in ("sleep 900", "sleep 20m",
                    "while true; do sleep 30; gh pr checks; done",
                    "until gh pr checks; do sleep 60; done"):
        assert _decision(_fg(command)) == "deny", command


def test_short_sleep_and_bounded_loop_allowed():
    """`sleep 200` is the sanctioned check-in wait: this threshold is what makes the
    polling rhythm legal, and `timeout N` is a bound, not an unbounded wait."""
    for command in ("sleep 200", "sleep 0.5", "for i in 1 2 3; do sleep 2; done",
                    "timeout 200 bash -c 'while :; do sleep 5; done'"):
        assert _fg(command) is None, command


def test_long_shape_inside_quotes_or_comment_allowed():
    """`_mask_shell_text`, the same masking the shell half of §4 depends on."""
    for command in ('grep -r "sleep 900" src/', "echo 'gh run watch'",
                    "pytest tests/test_x.py  # not the whole suite"):
        assert _fg(command) is None, command


def test_suite_deny_text_points_at_the_pinned_rule():
    """The text must not read as permission to run the suite locally: the pinned entry
    says a worker must not, and backgrounding is the second-best exit."""
    reason = _reason(_fg("uv run pytest tests/"))

    assert "targeted" in reason
    assert reason.index("targeted") < reason.index("run_in_background")


def test_same_shapes_allowed_when_backgrounded():
    """One matcher, two call sites: §3 refuses the shape in the foreground and §4
    permits it backgrounded. A second spelling of "long" would leave a lead with no
    legal way to run it at all (kn-d4d5a967)."""
    for command in ("uv run pytest tests/", "pytest",
                    "python -m pytest tests/ evals/", "gh run watch",
                    "gh pr checks 42 --watch", "sleep 900", "sleep 20m",
                    "while true; do sleep 30; gh pr checks; done",
                    "until gh pr checks; do sleep 60; done"):
        assert _bash(command, run_in_background=True) is None, command
        assert _fg(command, run_in_background=True) is None, command


def test_other_backgrounding_still_denied():
    """A job nobody polls for, which is the orphan class §4 of the 2026-09-23 spec
    closed. Today's text, unchanged."""
    for command, extra in (("nohup ./server.sh &", {}),
                           ("./server.sh", {"run_in_background": True})):
        result = _bash(command, **extra)
        assert _decision(result) == "deny", command
        assert "FOREGROUND" in _reason(result)
        assert "claude -p" in _reason(result)


def test_an_ampersanded_long_shape_is_told_the_rhythm_not_the_foreground():
    """Two wrong turns of advice otherwise: today's text would send the lead at the
    foreground call `long_foreground_decision` refuses next. The SHAPE may be
    backgrounded; the `&` may not."""
    result = _bash("uv run pytest tests/ &")

    assert _decision(result) == "deny"
    reason = _reason(result)
    assert "run_in_background" in reason
    assert "FOREGROUND" not in reason
    assert "`&`" in reason and "BashOutput" in reason


def test_foreground_long_seat_agent_denied():
    """The 55-write, $33.5 half of the bill, and the larger one."""
    for seat in hooks.LONG_SEATS:
        for tool in ("Agent", "Task"):
            result = _seat(seat, tool_name=tool)
            assert _decision(result) == "deny", (seat, tool)
            assert "run_in_background" in _reason(result)
            assert "TaskOutput" in _reason(result)


def test_backgrounded_long_seat_allowed():
    for seat in hooks.LONG_SEATS:
        assert _seat(seat, run_in_background=True) is None, seat


def test_other_subagent_type_allowed_in_foreground():
    """A short seat blocking for four minutes is cheap, and taxing every delegation
    with a hook refusal would be the false positive that trains the rule away."""
    for seat in ("jarvis-architect", "jarvis-test-lead", "general-purpose"):
        assert _seat(seat) is None, seat


def test_long_foreground_precedes_the_jarvis_auto_allow():
    """The ordering IS the enforcement: the auto-allow waves every `jarvis …` command
    through, so a refusal placed after it passes a unit test and does nothing in
    production."""
    source = inspect.getsource(hooks.preflight_decision)

    assert "long_foreground_decision" in source
    assert source.index("long_foreground_decision") < source.index(
        "background_task_decision")
    assert source.index("long_foreground_decision") < source.index(
        "is_jarvis_command_chain")
    # And it runs: a carved-out shape inside a `jarvis` chain is still refused.
    assert _decision(hooks.preflight_decision(
        {"tool_name": "Bash",
         "tool_input": {"command": "jarvis wo show wo-bg01 && sleep 900"}},
        HEADLESS)) == "deny"


def test_settings_matcher_names_agent():
    """Without this the `Agent` arm is dead code in production and no behavioural test
    would say so (§3b)."""
    settings = json.loads(
        (Path(hooks.__file__).parent / "assets" / "settings.base.json").read_text())
    matcher = settings["hooks"]["PreToolUse"][0]["matcher"]

    assert "Agent" in matcher.split("|")
    assert "Task" in matcher.split("|")
    assert "Bash" in matcher.split("|")
