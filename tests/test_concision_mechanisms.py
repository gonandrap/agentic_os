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


# -- pass a reference, never a payload (SS5 of the bounded-model-inputs spec) -------------

def _payload(command: str, **env):
    return hooks.payload_reference_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-conc01", **env})


def _reason(decision) -> str:
    assert decision is not None
    out = decision["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def test_an_oversized_wo_send_is_refused_and_told_what_to_pass_instead():
    """A `wo send` body goes WHOLE into the target worker's next turn, so a pasted diff
    is paid for by a session that never asked for it. The refusal has to name the size,
    the cap and the reference that would have done the job — a worker told only "too
    long" splits the paste in two."""
    reason = _reason(_payload(f'jarvis wo send wo-target "{"x" * 9000}"'))

    assert "9000 characters" in reason
    assert "6000" in reason
    assert "wo send" in reason
    for alternative in ("pull request URL", "commit SHA", "line range",
                        "command that reproduces it"):
        assert alternative in reason, f"refusal does not offer {alternative!r}"


def test_an_oversized_wo_assume_is_refused():
    reason = _reason(_payload(f'jarvis wo assume wo-conc01 "{"y" * 7000}"'))
    assert "7000 characters" in reason and "wo assume" in reason


def test_an_oversized_question_is_refused_before_the_command_runs():
    """`ops.ask_question` already refuses this after the fact. The hook is the same rule
    one layer earlier, and that layer is the only one that runs before a substitution in
    the argument has been expanded into the worker's own context."""
    from jarvis import sections

    reason = _reason(_payload(f'jarvis wo ask wo-conc01 "{"q" * 4500}"'))
    assert "4500 characters" in reason
    assert str(sections.QUESTION_MAX_CHARS) in reason


def test_the_hooks_question_cap_is_the_same_number_as_ops_enforces():
    """Two layers, ONE rule. A second number here would mean a question the hook let
    through and `ops.ask_question` refused, or the reverse."""
    from jarvis import sections

    assert concision.QUESTION_MAX_CHARS == sections.QUESTION_MAX_CHARS
    assert _payload(f'jarvis wo ask wo-conc01 "{"q" * 3999}"') is None
    assert _payload(f'jarvis wo ask wo-conc01 "{"q" * 4001}"') is not None


@pytest.mark.parametrize("substitution,producer", [
    ("$(git diff)", "git diff"),
    ("`git diff`", "git diff"),
    ("$(git log --oneline -20)", "git log"),
    ("$(cat src/jarvis/hooks.py)", "cat"),
    ("$(gh pr diff 828)", "gh pr diff"),
    ("$(gh pr view 828 --json body)", "gh pr view"),
    ("$(curl -s https://example.com)", "curl"),
    ("$(tail -n 500 /tmp/log)", "tail"),
])
def test_an_unbounded_substitution_is_refused_however_short_the_command(substitution,
                                                                       producer):
    """The half `ops` cannot do: by the time `ops.ask_question` sees the text the shell
    has already expanded the substitution into this worker's argv AND its context, so
    the flood is paid for whatever `ops` then decides. Length is no defence — `$(git
    diff)` is 11 characters before the shell runs it."""
    reason = _reason(_payload(f'jarvis wo send wo-target "see {substitution}"'))

    assert producer in reason
    assert "before" in reason and "context" in reason


@pytest.mark.parametrize("substitution", [
    "$(git rev-parse HEAD)",
    "$(date)",
    "$(pwd)",
    "$(git branch --show-current)",
])
def test_a_bounded_substitution_is_not_this_hooks_business(substitution):
    """A commit SHA, a branch name and a date are exactly what the rule asks a worker to
    pass instead. Refusing them would leave no way to write the reference."""
    assert _payload(f'jarvis wo send wo-target "at {substitution}"') is None


def test_a_quoted_payload_still_reaches_the_check():
    """kn-21d73ac2: the shell strips the quotes before anything downstream sees the
    text, so a denylist applied to raw text fails open. Parsed words only."""
    assert _payload('jarvis wo send wo-x "$(git diff)"') is not None
    assert _payload("jarvis wo send wo-x '$(git diff)'") is not None
    assert _payload("jarvis wo send wo-x $(git diff)") is not None


def test_the_payload_check_is_reachable_around_by_nothing_the_auto_allow_covers():
    """Same ordering trap as the `--summary` cap above: `preflight_decision` auto-allows
    every `jarvis …` command, so a check wired after it is dead code."""
    payload = {"tool_name": "Bash", "tool_input": {
        "command": f'jarvis wo send wo-target "{"x" * 9000}"'}}

    decision = hooks.preflight_decision(payload, {"JARVIS_WO_ID": "wo-conc01"})

    assert decision is not None
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("command", [
    'jarvis wo send wo-x "PR 828 is green; see src/jarvis/hooks.py:825-871."',
    "jarvis wo list",
    'echo "jarvis wo send wo-x $(git diff)"',       # the words, not the command
])
def test_commands_the_payload_check_has_no_opinion_about(command):
    assert _payload(command) is None


def test_a_send_reached_through_a_cd_chain_or_an_absolute_path_still_counts():
    long = f'"{"x" * 9000}"'
    assert _payload(f'cd /tmp && jarvis wo send wo-x {long}') is not None
    assert _payload(f'/usr/local/bin/jarvis wo send wo-x {long}') is not None


def test_the_message_cap_is_a_project_setting_and_zero_switches_it_off():
    body = f'jarvis wo send wo-x "{"x" * 5000}"'

    assert _payload(body) is None                                     # under the 6000
    assert _payload(body, JARVIS_MESSAGE_MAX_CHARS="1000") is not None
    assert _payload(body, JARVIS_MESSAGE_MAX_CHARS="0") is None       # off for a project


def test_an_unparseable_message_cap_falls_back_rather_than_blocking_every_message():
    """Both failure directions are worse than the default: a typo in a catalog must not
    deny every message in the fleet, nor silently switch the rule off."""
    assert concision.message_cap({"JARVIS_MESSAGE_MAX_CHARS": "banana"}) == 6000
    assert concision.message_cap({}) == 6000
    assert concision.message_cap({"JARVIS_MESSAGE_MAX_CHARS": ""}) == 6000
    # A negative is a typo, not a request to switch the cap off; `0` is how off is asked
    # for, and clamping to 0 here would be the silent switch-off.
    assert concision.message_cap({"JARVIS_MESSAGE_MAX_CHARS": "-5"}) == 6000


def test_an_interactive_session_is_never_refused_a_payload():
    assert hooks.payload_reference_decision(
        {"tool_name": "Bash", "tool_input": {
            "command": f'jarvis wo send wo-x "{"x" * 9000}"'}},
        {}) is None


def test_the_parser_imports_only_the_standard_library_and_sections():
    """The hook runs on EVERY Bash call in every worker, so `catalog`, `ops` or `store`
    here is a defect and not a style point: a catalog parse is ~60ms against a ~155ms
    hook process. `.sections` is admissible because it imports `re` and nothing else."""
    import ast
    import inspect
    import sys

    tree = ast.parse(inspect.getsource(concision))
    local: set[str] = set()
    external: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            (local if node.level else external).add(node.module or "")
        elif isinstance(node, ast.Import):
            external |= {a.name.split(".")[0] for a in node.names}

    assert local <= {"sections"}, f"concision reached into the OS: {local}"
    for module in external:
        assert module in sys.stdlib_module_names, f"not standard library: {module}"


# -- fail shut, and cover the abbreviations argparse accepts (round-1 review of SS5) ------

def _preflight(command: str, **env):
    return hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-conc01", **env})


@pytest.mark.parametrize("command", [
    'jarvis wo send wo-x "$(git diff)',
    "jarvis wo send wo-x 'unclosed",
    'jarvis wo assume wo-x "$(git diff)',
    'jarvis wo ask wo-x "why is this "quoted\' wrong',
    'cd /tmp && jarvis wo send wo-x "$(cat src/jarvis/hooks.py)',
])
def test_an_unparseable_jarvis_command_is_refused_rather_than_guessed_at(command):
    """kn-21d73ac2's fail-open direction, one layer out: `_segments` returns nothing when
    `shlex.split` raises, so an unbalanced quote used to walk straight past the payload
    check and let the shell expand whatever it decided the words were."""
    reason = _reason(_preflight(command))

    assert "quot" in reason                     # names the cause
    assert "jarvis" in reason


def test_an_unparseable_non_jarvis_command_is_not_this_checks_business():
    """The predicate refuses, so matching widely is the safe direction — but not so
    widely that every half-typed shell line in the fleet is denied by this check."""
    assert _payload('grep -r "unclosed src/') is None
    assert _payload('echo "unbalanced') is None
    assert concision.unparseable_jarvis_command('grep -r "unclosed src/') is False
    assert concision.unparseable_jarvis_command('jarvis wo send wo-x "unclosed') is True
    assert concision.unparseable_jarvis_command('jarvis wo list') is False


@pytest.mark.parametrize("spelling", ["--summ", "--s", "--summar"])
def test_an_abbreviated_summary_option_is_capped_like_the_full_spelling(spelling):
    """`jarvis` uses argparse with the default `allow_abbrev=True`, so the CLI accepts
    any unambiguous prefix. kn-21d73ac2 names this exact fail-open: matching the option
    name exactly leaves `--summ "<500 words>"` unchecked."""
    long = " ".join(["word"] * 500)

    reason = _reason(_preflight(f'jarvis wo finish wo-x {spelling} "{long}"'))
    assert "500 words" in reason and "120" in reason

    reason = _reason(_preflight(f'jarvis wo finish wo-x {spelling}="{long}"'))
    assert "500 words" in reason

    # Under the cap the abbreviation is ordinary work: `preflight_decision` allows every
    # `jarvis …` chain, so what is asserted is that it is not DENIED.
    allowed = _preflight(f'jarvis wo finish wo-x {spelling} "short enough"')
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert concision.finish_summary(f'jarvis wo finish wo-x {spelling}="ok"') == "ok"


def test_an_abbreviation_is_only_honoured_while_it_stays_unambiguous():
    """The rule argparse applies, pinned directly: a prefix two options share selects
    neither. None of `wo finish`'s four options collide, so this drives the matcher with
    a synthetic sibling set rather than pretending one does."""
    siblings = ("--summary", "--summarise", "--pr")

    assert concision._option_matches("--summary", "--summary", siblings)
    assert concision._option_matches("--summaris", "--summarise", siblings)
    assert not concision._option_matches("--summ", "--summary", siblings)
    assert not concision._option_matches("--summ", "--summarise", siblings)
    assert not concision._option_matches("--", "--summary", siblings)
    assert not concision._option_matches("--x", "--summary", siblings)

    # And what the four real ones mean today: `--p` is `--pr` and nothing else.
    assert concision._WO_FINISH_OPTIONS == (
        "--summary", "--pr", "--evidence", "--abandon")
    assert concision._option_matches("--p", "--pr", concision._WO_FINISH_OPTIONS)
    assert not concision._option_matches("--p", "--summary",
                                         concision._WO_FINISH_OPTIONS)


def test_an_abbreviated_option_still_consumes_its_value_in_the_positional_walk():
    """The text argument is found by POSITION, so an option whose value was mistaken for
    a positional would cap the wrong word — or nothing."""
    long = "x" * 9000

    assert _reason(_preflight(f'jarvis wo send wo-x "{long}" --sou jarvis'))
    assert _reason(_preflight(f'jarvis wo send --proj jarvis_os wo-x "{long}"'))
    assert concision.jarvis_payload_args(
        'jarvis wo send --proj jarvis_os wo-x "body"') == [
        ("wo send", "message", "body")]


@pytest.mark.parametrize("payload", ["$(git diff)", "$(cat src/jarvis/hooks.py)"])
def test_quoting_a_payload_changes_nothing_about_the_decision(payload):
    """A rule a quote can move is a rule a worker can step around by accident."""
    bare = _preflight(f"jarvis wo send wo-x {payload}")
    double = _preflight(f'jarvis wo send wo-x "{payload}"')
    single = _preflight(f"jarvis wo send wo-x '{payload}'")

    assert _reason(bare) == _reason(double) == _reason(single)
