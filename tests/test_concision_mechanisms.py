"""The mechanisms that make the house style take effect, rather than be available.

Design: docs/superpowers/specs/2026-09-19-concision-enforced.md.

`tests/test_concision.py` beside this file is the DELIVERY suite, and delivery was never
the problem: the August assets reached every worker correctly and were invoked 0 times
in 142 sessions while the median finish summary grew from 40 to 344 words. These pin the
two mechanisms that replaced asking — the `SessionStart` injection and the `--summary`
refusal.

Neither these nor those can show that worker behaviour CHANGED. That is
`evals/llm/test_house_style_ab.py`, and kn-fe226ab1 is why it has to exist: a rule added
to the contract measured 0/5, twice, while every free test around it stayed green.
"""

import pytest

from jarvis import concision, hooks


def _finish(command: str, **env):
    return hooks.finish_summary_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-conc01", **env})


# -- the refusal (SS5.2) ----------------------------------------------------------------

def test_an_overlong_summary_is_refused_and_told_where_the_detail_goes():
    """The refusal has to be actionable in one read. A worker told only "too long"
    shortens by deleting, which is the outcome the cap exists to prevent — the detail
    has somewhere to go and the message must name it."""
    decision = _finish(f'jarvis wo finish wo-conc01 --summary "{"word " * 200}"')

    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "200 words" in reason and "cap is 120" in reason
    assert "final message of this turn" in reason
    assert "i-have-adhd" in reason      # the skill load this denial exists to trigger
    assert "--evidence" in reason       # the other place detail legitimately goes


def test_a_summary_within_the_cap_is_not_this_hooks_business():
    assert _finish(
        'jarvis wo finish wo-conc01 --summary "Fixed the poll. PR 401."') is None


def test_the_cap_is_reachable_around_by_nothing_the_auto_allow_covers():
    """`preflight_decision` auto-allows every `jarvis …` command so background workers
    do not stall on a permission prompt. That allow runs AFTER this check on purpose:
    put it first and the cap is dead code — the same ordering trap the PR title and
    body checks already sit in front of."""
    payload = {"tool_name": "Bash", "tool_input": {
        "command": f'jarvis wo finish wo-conc01 --summary "{"w " * 300}"'}}

    decision = hooks.preflight_decision(payload, {"JARVIS_WO_ID": "wo-conc01"})

    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    "jarvis wo list",                                   # not a finish
    'jarvis wo finish wo-conc01 --abandon "spike"',     # a finish with no summary
    'echo "jarvis wo finish --summary long"',           # the words, not the command
    'jarvis wo finish wo-conc01 --summary "unclosed',   # shlex cannot parse it
])
def test_commands_this_hook_has_no_opinion_about(command):
    """Narrow on purpose, for `gh_pr_create_args`' reason: a hook firing on commands it
    does not really understand costs more than the leak it prevents. The unparseable
    case is deliberately in this list — refusing what it cannot read would block a
    finish over a quoting bug the worker cannot see from the error."""
    assert _finish(command) is None


def test_a_finish_reached_through_a_cd_chain_or_an_absolute_path_still_counts():
    """Workers routinely chain `cd <worktree> && jarvis …`, and `jarvis` is not always
    on PATH by its bare name — the same discovery that made `gh_pr_create_args` match
    `/snap/bin/gh` after the first real PR went out untitled."""
    long = f'"{"word " * 200}"'
    assert _finish(f'cd /tmp && jarvis wo finish wo-conc01 --summary {long}') is not None
    assert _finish(f'/usr/local/bin/jarvis wo finish wo-conc01 --summary {long}') is not None


def test_the_cap_is_a_project_setting_and_zero_switches_it_off():
    long = f'jarvis wo finish wo-conc01 --summary "{"word " * 60}"'

    assert _finish(long) is None                                     # 60 under the 120
    assert _finish(long, JARVIS_SUMMARY_MAX_WORDS="30") is not None  # 60 over a tighter
    assert _finish(long, JARVIS_SUMMARY_MAX_WORDS="0") is None       # off for a project


def test_an_unparseable_cap_falls_back_rather_than_blocking_every_finish():
    """A typo in a catalog must not be able to deny every `finish` in the fleet, nor to
    silently switch the rule off. Both failure directions are worse than the default."""
    assert concision.summary_cap({"JARVIS_SUMMARY_MAX_WORDS": "banana"}) == 120
    assert concision.summary_cap({}) == 120


def test_an_interactive_session_is_never_refused():
    """The cap governs dispatched workers. A session the user opened themselves is
    theirs, and the OS does not get to refuse their `finish` — the same JARVIS_WO_ID
    guard `pr_body_decision` uses."""
    assert hooks.finish_summary_decision(
        {"tool_name": "Bash", "tool_input": {
            "command": f'jarvis wo finish wo-x --summary "{"w " * 300}"'}},
        {}) is None


# -- the injection (SS5.1) --------------------------------------------------------------

def test_the_house_style_carries_both_skills_and_the_rails():
    """What the digest must hold to be worth injecting: the compression half, the
    shaping half, and the rails that stop it trading correctness for brevity (SS4.2)."""
    text = concision.house_style()

    assert text.startswith(concision.HOUSE_STYLE_BEGIN)
    assert text.endswith(concision.HOUSE_STYLE_END)
    assert "caveman" in text and "i-have-adhd" in text
    for compression in ("Drop articles", "No narration of your own tool calls"):
        assert compression in text, f"compression rule missing: {compression!r}"
    for shaping in ("First line is the answer", "Say each thing ONCE"):
        assert shaping in text, f"shaping rule missing: {shaping!r}"
    for rail in ("Exact error strings", "not/never/no/only/except",
                 "Security warnings", "Failing test output"):
        assert rail in text, f"correctness rail missing from the house style: {rail!r}"


def test_the_house_style_claims_what_a_worker_WRITES_and_not_only_what_it_says():
    """The August miss in one line. `outputStyle: Concise` reached every worker and
    governs what a session SAYS; everything measured as too long was something a worker
    WROTE through a CLI call. The digest has to close that gap in so many words or it
    repeats the same failure with more machinery behind it."""
    text = concision.house_style()

    for surface in ("work-order summaries", "commit messages", "PR bodies",
                    "code comments"):
        assert surface in text, f"house style does not claim {surface!r}"


def test_the_digest_stays_small_enough_to_ride_every_turn():
    """It is injected once per turn, so its size is a running cost paid for the life of
    every work order. The two skills spell the same rules out in ~2,900 tokens; the
    whole design of a digest is that it does not (SS5.1). A future edit that pushes it
    past this is quietly choosing a different mechanism."""
    assert concision.word_count(concision.house_style()) < 500


def test_the_ab_markers_are_present_and_cut_cleanly():
    """`evals/llm/test_house_style_ab.py` builds its WITHOUT arm by cutting between
    these markers, so the arms stay byte-equal everywhere else. kn-fe226ab1: an A/B
    that re-composes its arms, or adds the rule to both, measures nothing — and passes
    at 100% while doing it."""
    text = concision.house_style()

    assert text.count(concision.HOUSE_STYLE_BEGIN) == 1
    assert text.count(concision.HOUSE_STYLE_END) == 1
    prompt = f"before\n{text}\nafter"
    start = prompt.index(concision.HOUSE_STYLE_BEGIN)
    end = prompt.index(concision.HOUSE_STYLE_END) + len(concision.HOUSE_STYLE_END)
    assert prompt[:start] + prompt[end:] == "before\n\nafter"


# -- the subagent injection (SS5.1, the SessionStart twin) -------------------------------

STANDING = "Run `uv run pytest tests/ evals/` before opening a PR."


def _subagent(agent_type="general-purpose", **env):
    return hooks.handle_hook(
        {"hook_event_name": "SubagentStart", "session_id": "sess-1",
         "agent_id": "agent-1", "agent_type": agent_type, "cwd": "/tmp"},
        env)


def test_a_subagent_is_handed_the_house_style_and_the_project_standing_prompt():
    """The gap this closes, measured on Claude Code 2.1.278: a Task subagent inherits
    CLAUDE.md, skills, settings and hooks, and NONE of `--append-system-prompt`, the
    `--agent` persona or anything `SessionStart` injected — `SessionStart` never fires
    for one (0 of 18 subagent transcripts carry the marker). So both halves have to be
    in the text, not merely a dict coming back."""
    out = _subagent(JARVIS_WO_ID="wo-conc01",
                    JARVIS_APPEND_SYSTEM_PROMPT=STANDING)["hookSpecificOutput"]

    assert out["hookEventName"] == "SubagentStart"
    context = out["additionalContext"]
    assert concision.HOUSE_STYLE_BEGIN in context
    assert "Say each thing ONCE" in context
    assert "Failing test output" in context
    assert STANDING in context


def test_every_agent_type_gets_it():
    """No matcher in `settings.base.json`, so the shipped `jarvis-architect` /
    `jarvis-test-lead` seats are covered too: their definitions say what to think about,
    not how to write, and the standing prompt is the project's either way."""
    for agent_type in ("general-purpose", "Explore", "jarvis-architect"):
        context = _subagent(agent_type, JARVIS_WO_ID="wo-conc01",
                            JARVIS_APPEND_SYSTEM_PROMPT=STANDING
                            )["hookSpecificOutput"]["additionalContext"]
        assert STANDING in context, f"{agent_type} got no standing prompt"


def test_a_project_with_no_standing_prompt_still_gets_the_style():
    """The standing half is optional and its absence must not swallow the style half —
    nor leave a heading with nothing under it."""
    context = _subagent(JARVIS_WO_ID="wo-conc01"
                        )["hookSpecificOutput"]["additionalContext"]

    assert context == concision.house_style()


def test_a_session_the_user_opened_gets_nothing():
    """Same `JARVIS_WO_ID` guard as the refusal above: the OS does not inject its worker
    contract into a subagent of a session it was not asked to run."""
    assert _subagent(JARVIS_APPEND_SYSTEM_PROMPT=STANDING) is None


def test_the_hook_reads_the_standing_prompt_from_the_environment():
    """`concision` must not import `catalog`: this hook fires on every Task call and a
    catalog parse is ~60ms against a ~155ms process (module docstring). An import in
    this path is a defect, not a style point — so the source is asserted, not assumed."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(concision))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}
    assert not any((m or "").endswith("catalog") for m in imported), imported
    assert concision.subagent_context({}) == concision.house_style()
