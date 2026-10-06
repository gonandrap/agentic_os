"""LLM-graded navigation evals: when an agent is asked about particular code, does it
reach for Serena's symbol index or does it grep?

Every other eval in this suite grades TEXT — what the model says it would do. This one
grades TOOL CALLS, because the question is not what the agent claims but what it invokes,
and those come apart exactly where it matters: a model that has been told "prefer Serena"
will happily say so in prose and then run `grep -rn` because grep is what it thought of
first.

**How it observes.** A `PreToolUse` hook records every tool call, and the payload carries
`agent_type` for a subagent's calls while carrying no such key for the lead's own. So the
recorder attributes each call to the seat that made it, and a seat scenario can assert on
that seat's tools without the lead's own bookkeeping calls polluting the result. This is
the same discriminator the per-seat gate work rests on.

**Why the seats and the worker briefing are graded separately.** They are held to the
posture by different mechanisms and can fail independently:

  * the SEATS hold it by capability — `tools:` is an allowlist, and Serena is in it
    (probed live: a seat naming only Read/Grep/Glob reports SERENA-UNAVAILABLE). Grep is
    still granted, deliberately, because these seats run in arbitrary adopted projects
    and most have no Serena index — so the ranking here is still a behavioural claim, not
    a structural one.
  * a WORKER holds it by prose alone. It needs `Grep` and `Bash` to do its job, so the
    tool cannot be withheld and the briefing's wording is the whole of the mechanism.
    That makes this the eval that would catch a rewording regression, which is precisely
    the failure `test_worker_judgment.py` was written for after "prefer recording an
    assumption" passed every structural check and produced zero `jarvis wo ask` calls.

Opt-in (spends tokens, needs a logged-in Claude Code with Serena available):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_navigation_judgment.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import claude_cli, hooks
from jarvis.bootstrap import jarvis_hook_command
from jarvis.catalog import ProjectSpec
from jarvis.dispatch import (bash_first_env, build_worker_prompt, serena_allow_rules,
                             tool_search_env)
from jarvis.navigation import (NAV_COMMANDS, SOURCE_SUFFIXES, is_symbol_call,
                               navigates_source)

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

ASSETS = Path(__file__).resolve().parents[2] / "src" / "jarvis" / "assets"

# The BUILT-IN search tools. Kept as its own set because the negative control is scoped
# by the question, not by the tool — but on its own this set is blind to the real defect:
# the fleet called `Grep` ZERO times over 276 transcripts and greps through `Bash`
# (4,271 `grep`, 3,902 `sed -n`, 999 `cat`). §5.2 item 2 of
# docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md — so the command
# string is classified below, rather than every `Bash` call being failed.
TEXT_SEARCH_TOOLS = {"Grep", "Glob"}


def is_serena(tool: str) -> bool:
    return tool.startswith("mcp__serena__") or tool.startswith("mcp__plugin_serena_serena__")


def text_search_for_code(calls: list[dict[str, str]]) -> list[str]:
    """Every call in this log that went looking for CODE by text."""
    found = []
    for c in calls:
        if c["tool"] in TEXT_SEARCH_TOOLS:
            found.append(c["tool"])
        elif c["tool"] == "Bash" and navigates_source(c.get("command") or "",
                                                     SOURCE_SUFFIXES, NAV_COMMANDS):
            found.append(f"Bash({c['command']})")
    return found


def symbol_tools(tools: list[str]) -> list[str]:
    """Calls that answer a SYMBOL question rather than a text one.

    §7 of docs/specs/2026-10-02-serena-the-cheap-path.md: the predicate is the leaf's, so
    `read_memory`/`list_memories` are bookkeeping and do NOT count.

    `search_for_pattern` is excluded on purpose — it is Serena's *text* search, so
    counting it would let a run that merely swapped one text search for another pass as a
    win, the easiest way for an eval like this to become vacuous. `LSP` IS counted: a
    pyright go-to-definition answers the question with the same authority Serena does,
    and the property is "navigate by symbol index, not by text", never "use this vendor".
    """
    return [t for t in tools if is_symbol_call(t)]


def first_navigation_call(calls: list[dict[str, str]]) -> dict[str, str] | None:
    """The first call in the ORDERED log that navigated source, either way.

    The done-when is "no source-navigation Bash call precedes the first symbol call", NOT
    "the first tool call is a symbol call": `activate_project`, `list_memories` and
    `read_memory` are bookkeeping and legitimately come first, and `search_for_pattern`
    is Serena's text search, which counts as text and never as a symbol call.
    """
    for c in calls:
        if is_symbol_call(c.get("tool") or ""):
            return c
        if c.get("tool") == "Bash" and navigates_source(c.get("command") or "",
                                                       SOURCE_SUFFIXES, NAV_COMMANDS):
            return c
    return None


# -- the scratch repo the subject is asked about -----------------------------------------

MODULE = '''\
"""Order pricing."""


def apply_discount(order, pct):
    """Reduce every line's price by `pct`."""
    for line in order.lines:
        line.price = line.price * (100 - pct) / 100
    return order


def total_for(order):
    """The order's total, after discounts."""
    return sum(line.price for line in order.lines)
'''

CALLER = '''\
from pricing import apply_discount, total_for


def checkout(order, coupon):
    if coupon:
        apply_discount(order, coupon.pct)
    return total_for(order)


def preview(order):
    return total_for(order)
'''

# The negative control's target lives in a NON-SOURCE file: §6's hook must not refuse a
# literal-word search in a `.md`.
NOTES = '''\
# Pricing notes

A coupon is applied before the total is computed.
'''

# The Bash COMMAND is recorded, not just the tool name: without it a `Bash(grep -rn …)`
# is indistinguishable from `Bash(pytest …)` and the whole suite is blind to the measured
# failure (spec §5.2 item 2).
RECORDER = '''\
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
with open(os.environ["JARVIS_TOOL_LOG"], "a") as f:
    f.write(json.dumps({"tool": d.get("tool_name") or "",
                        "agent": d.get("agent_type") or "",
                        "command": ((d.get("tool_input") or {}).get("command") or "")})
            + "\\n")
print("{}")
'''

# §5's activation line, which a real spawn gets from `settings.base.json`'s SessionStart /
# SubagentStart hooks. It CALLS THE SHIPPED HELPER and only supplies the envelope: the
# shipped `jarvis _hook` cannot serve SessionStart here because its branch resolves a work
# order out of the project store and this eval has none, so it would inject nothing.
ACTIVATION = '''\
import json, sys
from pathlib import Path
from jarvis.hooks import serena_activation_context
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": d.get("hook_event_name") or "SessionStart",
    "additionalContext": serena_activation_context(Path(d.get("cwd") or ".")),
}}))
'''


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    """A tiny Serena-indexed python repo, outside this one.

    Outside deliberately: run from inside the Jarvis checkout, the subject would load this
    repo's CLAUDE.md — which itself says to prefer Serena — and the eval would be grading
    that file instead of the thing under test.
    """
    root = tmp_path_factory.mktemp("nav-eval")
    (root / "pricing.py").write_text(MODULE)
    (root / "checkout.py").write_text(CALLER)
    (root / "notes.md").write_text(NOTES)
    (root / ".serena").mkdir()
    (root / ".serena" / "project.yml").write_text(
        'project_name: "nav-eval"\n'
        'language_servers:\n- python\n'
        'ignore_all_files_in_gitignore: false\n'
    )
    # §6 (specs/2026-10-02-serena-the-cheap-path.md): py_nav_decision resolves project root by `.jarvis/`, so without it the hook never fires.
    (root / ".jarvis").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    return root


#: The arms, each a `(bash_first, tool_search, py_nav_hook)` triple — ONE dimension and not
#: a grid. `off`/`cli`/`off` is the fleet's PRE-FLIP default; `strict` PINS the steer rather
#: than leaving it to the per-session statsig cohort draw, which is the only way that arm is
#: a measurement.
#: `tool-search` is the FLIPPED-DEFAULT SPAWN — §4's deferral off plus §6's hook live, with
#: §5's activation wired in every arm, because §7's ordering assertion measures the
#: COMBINATION of §4, §5 and §6 and not §4 alone. ASSERTED.
STEER_ARMS = [
    pytest.param(("off", "cli", "off"), id="no-steer"),
    pytest.param(("strict", "cli", "off"), id="steer",
                 marks=pytest.mark.xfail(
                     strict=False,
                     reason="recorded, not asserted: the steer is a vendor system-prompt "
                            "block whose variant is drawn per session by statsig, and CI "
                            "must not go red on a cohort draw inside a vendor binary. "
                            "The arm's job is to show the two arms differ, which is the "
                            "measurement that `off` is the right default (spec §5.2)")),
    pytest.param(("off", "off", "on"), id="tool-search"),
]


def run_and_record(repo: Path, prompt: str, agents: dict[str, Path] | None = None,
                   system_prompt: str | None = None,
                   timeout: int = 420, bash_first: str = "off",
                   tool_search: str = "cli", py_nav_hook: str = "off",
                   permission_mode: str = "auto",
                   outcome: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Run one headless turn and return every tool call it made, with its agent_type.

    `auto` AND THE BASH-FIRST ENV ARE THE SUBJECT, not incidental settings. This used to
    hardcode `acceptEdits` while `dispatch` spawns every worker under `auto`, so no arm of
    this suite had ever seen the steer the suite exists to catch (spec §5.2 item 1). The
    env is taken from `dispatch.bash_first_env` rather than retyped, so the arm cannot
    drift from what a real spawn writes.
    """
    log = repo / "tool-log.jsonl"
    log.write_text("")
    recorder = repo / "record_tool.py"
    recorder.write_text(RECORDER)
    activation = repo / "activation_context.py"
    activation.write_text(ACTIVATION)
    # `sys.executable`, not bare `python3`, so `jarvis` is importable in the hook process.
    activation_matchers = [{"hooks": [
        {"type": "command", "command": f"{sys.executable} {activation}", "timeout": 15}]}]
    settings = repo / "eval-settings.json"
    # §6's hook is gated on `JARVIS_WO_ID`, on `.serena/project.yml` and on `.jarvis/`, which the fixture has.
    hook_env = {"JARVIS_PY_NAV_HOOK": "on", "JARVIS_WO_ID": "wo-naveval"} \
        if py_nav_hook == "on" else {}
    matchers = [{"matcher": ".*", "hooks": [
        {"type": "command", "command": f"python3 {recorder}", "timeout": 15}]}]
    if py_nav_hook == "on":
        matchers.append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": jarvis_hook_command(), "timeout": 15}]})
    settings.write_text(json.dumps({
        # `JARVIS_SERENA` in every arm: it is the key the §5 activation gate reads in
        # production, and dispatch writes it for every worker.
        "env": {"JARVIS_TOOL_LOG": str(log), "JARVIS_SERENA": "1",
                **bash_first_env(bash_first),
                **tool_search_env(tool_search), **hook_env},
        # The SAME allow rules dispatch writes for a real worker, imported rather than
        # retyped. Naming a Serena tool in `tools:` makes it visible; permission is a
        # separate gate, and a headless turn cannot answer a prompt — probed live, a seat
        # without these had `activate_project` BLOCKED and gave up. An eval that granted
        # permissions dispatch does not would be measuring a worker the fleet never runs.
        "permissions": {"allow": serena_allow_rules()},
        "hooks": {"PreToolUse": matchers,
                  "SessionStart": activation_matchers,
                  "SubagentStart": activation_matchers},
    }))
    for name, src in (agents or {}).items():
        dest = repo / ".claude" / "agents" / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text())

    try:
        # `keep_default_context`: this eval's SUBJECT is the default environment — it
        # measures whether a worker carrying its CLAUDE.md and settings reaches for
        # Serena — so the persona is APPENDED here and only here.
        text = claude_cli.run_headless(prompt, system_prompt=system_prompt,
                                       settings=settings,
                                       permission_mode=permission_mode, model=MODEL,
                                       cwd=repo, timeout=timeout, tools=None,
                                       keep_default_context=True)
        # The ANSWER as well as the log: a hook-twin test asserts the turn produced one.
        if outcome is not None:
            outcome["text"] = text or ""
    except claude_cli.ClaudeCliError as exc:
        if outcome is not None:
            outcome["error"] = str(exc)
        pass  # the tool log is the measurement; a failed turn still made its calls

    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def seat_calls(calls: list[dict[str, str]], seat: str) -> list[str]:
    """Only what the SEAT invoked. The lead's own calls carry no `agent_type` at all, so
    they are excluded by construction rather than by guessing at their shape."""
    return [c["tool"] for c in calls if c.get("agent") == seat]


# -- the seats ---------------------------------------------------------------------------

SEAT_QUESTION = (
    "Use the Task tool to invoke the subagent type '{seat}' with this exact question, "
    "and report its answer verbatim:\n\n"
    "In this repository, where is the function `total_for` defined, and which functions "
    "call it? Answer with the file and the callers."
)


@pytest.mark.parametrize("seat", ["jarvis-architect", "jarvis-test-lead"])
@scenario("navigation", "seat-uses-serena")
def test_a_seat_finds_code_with_serena(repo, seat):
    """The seat has both Serena and Grep. Asked a symbol question — where is this defined,
    who calls it — it must reach for the symbol index."""
    calls = run_and_record(
        repo, SEAT_QUESTION.format(seat=seat),
        agents={seat: ASSETS / "agents" / f"{seat}.md"})

    used = seat_calls(calls, seat)
    assert used, f"{seat} was never invoked, or its tool calls were not recorded: {calls}"
    assert symbol_tools(used), (
        f"{seat} answered a symbol question without one symbol-index call: {used}"
    )


@pytest.mark.parametrize("seat", ["jarvis-architect", "jarvis-test-lead"])
@scenario("navigation", "seat-does-not-grep")
def test_a_seat_does_not_grep_for_code(repo, seat):
    """The assertion the user asked for outright: no `Grep`, no `Glob`, when the question
    is about particular code and the symbol index can answer it."""
    calls = run_and_record(
        repo, SEAT_QUESTION.format(seat=seat),
        agents={seat: ASSETS / "agents" / f"{seat}.md"})

    seat_log = [c for c in calls if c.get("agent") == seat]
    grepped = text_search_for_code(seat_log)
    assert not grepped, f"{seat} used {grepped} to find code that Serena had indexed"


# -- an ordinary worker, held by prose alone ----------------------------------------------


@pytest.fixture(scope="module")
def worker_briefing(repo) -> str:
    spec = ProjectSpec(name="nav_eval", path=repo, description="pricing")
    return build_worker_prompt(
        {"id": "wo-nav01", "title": "Change how discounts are applied",
         "description": "Adjust the discount maths in the pricing module.",
         "kind": "worker"},
        spec, [])


@pytest.mark.parametrize("arm", STEER_ARMS)
@scenario("navigation", "worker-uses-serena")
def test_a_worker_finds_code_with_serena(repo, worker_briefing, arm):
    """No capability restriction is possible here — a worker needs Grep and Bash. The
    briefing's wording is the entire mechanism, so this is what would catch a rewording
    that quietly drops the ranking.

    Three arms. `no-steer` and `tool-search` are ASSERTED; `steer` is recorded only — see
    STEER_ARMS for why CI must not go red on it.
    """
    bash_first, tool_search, py_nav_hook = arm
    calls = run_and_record(
        repo,
        "Where is `total_for` defined in this repository, and which functions call it? "
        "Do not change any files; just answer.",
        system_prompt=worker_briefing, bash_first=bash_first, tool_search=tool_search,
        py_nav_hook=py_nav_hook)

    used = [c["tool"] for c in calls]
    assert used, f"the worker made no tool calls at all: {calls}"
    assert symbol_tools(used), (
        f"the worker answered a symbol question without the symbol index: {used}"
    )


@pytest.mark.parametrize("arm", STEER_ARMS)
@scenario("navigation", "worker-does-not-grep")
def test_a_worker_does_not_grep_for_code(repo, worker_briefing, arm):
    """`Bash(grep -rn …)` counts here, not just `Grep` — see `navigates_source`. The same
    arms, and the same reason the `steer` one is recorded rather than asserted.
    """
    bash_first, tool_search, py_nav_hook = arm
    calls = run_and_record(
        repo,
        "Where is `apply_discount` defined in this repository, and which functions call "
        "it? Do not change any files; just answer.",
        system_prompt=worker_briefing, bash_first=bash_first, tool_search=tool_search,
        py_nav_hook=py_nav_hook)

    grepped = text_search_for_code(calls)
    assert not grepped, f"the worker used {grepped} to find code that Serena had indexed"


@pytest.mark.parametrize("arm", STEER_ARMS)
@scenario("navigation", "no-grep-before-the-first-symbol-call")
def test_no_source_grep_precedes_the_first_symbol_call(repo, worker_briefing, arm):
    """The feature's done-when, as an ORDER assertion rather than set membership (§7): a
    worker that greps first and reaches the index afterwards has already paid for the
    grep, and every other assertion here would pass that run.
    """
    bash_first, tool_search, py_nav_hook = arm
    calls = run_and_record(
        repo,
        "Where is `total_for` defined in this repository, and which functions call it? "
        "Do not change any files; just answer.",
        system_prompt=worker_briefing, bash_first=bash_first, tool_search=tool_search,
        py_nav_hook=py_nav_hook)

    first = first_navigation_call(calls)
    assert first is not None, f"the worker never navigated source at all: {calls}"
    assert is_symbol_call(first["tool"]), (
        f"a source-navigation Bash call came before the first symbol call: {first}"
    )


@scenario("navigation", "text-search-is-still-allowed")
def test_a_genuine_text_question_may_still_use_text_search(repo, worker_briefing):
    """The negative control, and it is the one that keeps this suite honest.

    "Never grep" is the wrong lesson and an easy one to teach by accident: text search is
    the RIGHT tool for a text question. A run that answers "which files mention the word
    coupon" through the symbol index is not a better run, and if this eval punished grep
    unconditionally it would be training exactly that.
    """
    calls = run_and_record(
        repo,
        "Which files in this repository contain the literal word 'coupon'? Just answer.",
        system_prompt=worker_briefing)

    used = [c["tool"] for c in calls]
    assert used, f"the worker made no tool calls at all: {calls}"
    searched = [t for t in used
                if t in TEXT_SEARCH_TOOLS or t == "Bash"
                or (is_serena(t) and t.endswith("search_for_pattern"))]
    assert searched, (
        f"a genuine text question should be answered by a text search, not refused "
        f"or routed through the symbol index: {used}"
    )


@scenario("navigation", "the-hook-allows-a-markdown-text-search")
def test_the_hook_does_not_refuse_a_literal_word_search_in_a_markdown_file(repo,
                                                                          worker_briefing):
    """The negative control's hook twin: §6's hook is suffix-gated, so a literal-word
    search in a named `.md` file must be allowed and must still produce an answer.

    Scoped to the named markdown file on purpose (Neo q1284): the name claims the SUFFIX
    gate, so it must exercise the suffix gate and not the sweep gate.
    """
    outcome: dict[str, str] = {}
    calls = run_and_record(
        repo,
        "Which lines of notes.md in this repository contain the literal word 'coupon'? "
        "Just answer.",
        system_prompt=worker_briefing, py_nav_hook="on", outcome=outcome)

    assert outcome.get("text"), f"the turn produced no answer: {outcome}, calls={calls}"
    # A PreToolUse recorder sees the ATTEMPT and not the verdict, so the shipped predicate
    # is re-applied to every recorded command.
    env = {"JARVIS_PY_NAV_HOOK": "on", "JARVIS_WO_ID": "wo-naveval"}
    denied = [c["command"] for c in calls if c["tool"] == "Bash"
              and hooks.py_nav_decision(
                  {"tool_name": "Bash", "tool_input": {"command": c["command"]},
                   "cwd": str(repo)}, env) is not None]
    assert not denied, f"the hook would have refused a markdown text search: {denied}"
