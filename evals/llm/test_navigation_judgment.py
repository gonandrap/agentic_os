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
from typing import Any

import pytest

from jarvis import claude_cli, hooks
from jarvis.bootstrap import jarvis_hook_command
from jarvis.catalog import (DEFAULT_WORKER_BASH_FIRST, DEFAULT_WORKER_DOC_NAV_HOOK,
                            DEFAULT_WORKER_DOC_READ_LIMIT_LINES,
                            DEFAULT_WORKER_PY_NAV_HOOK, DEFAULT_WORKER_TOOL_SEARCH,
                            ProjectSpec)
from jarvis.central_store import CentralStore
from jarvis.dispatch import (bash_first_env, build_worker_prompt, serena_allow_rules,
                             tool_search_env)
from jarvis.navigation import (DOC_SUFFIXES, NAV_COMMANDS, SOURCE_SUFFIXES, dumps_doc,
                               is_spec_path, is_symbol_call, navigates_source)

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

    §7 of docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md: the predicate is the leaf's, so
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
# `Read`'s `file_path` and `limit` for the same reason one level on (§7.1): the negative
# assertion re-applies the shipped `hooks.doc_nav_decision` in Python to these payloads,
# and that predicate branches on `file_path` and on the PRESENCE of an integer `limit` —
# so a `limit` coerced to 0 or "" here would be graded as a refusal that never happened.
RECORDER = '''\
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
ti = d.get("tool_input") or {}
with open(os.environ["JARVIS_TOOL_LOG"], "a") as f:
    f.write(json.dumps({"tool": d.get("tool_name") or "",
                        "agent": d.get("agent_type") or "",
                        "command": (ti.get("command") or ""),
                        "file_path": ti.get("file_path"),
                        "limit": ti.get("limit")})
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


#: The arms, each a `(bash_first, tool_search, py_nav_hook, doc_nav_hook)` tuple.
#: `off`/`on`/`off`/`off` is
#: the SHIPPED DEFAULT — §4's deferral PINNED ON by Jarvis, with §5's activation wired in
#: every arm, because the ordering assertion measures the COMBINATION of §4 and §5.
#: §6's hook is OFF in EVERY arm, because that is what the fleet dispatches: it measures
#: zero contribution to first-call order, and on fleet-wide a worker cannot text-search
#: the tree at all (issue 936; wo-d2d777dc owns that fix). An arm running it live would
#: measure a spawn that never happens. ASSERTED.
#: `steer` pins the bash-first steer rather than leaving it to the per-session statsig
#: cohort draw, which is the only way that arm is a measurement; recorded, not asserted.
#: `tools-present` is the FALSIFIED configuration, kept as evidence — see its reason.
#: `doc_nav_hook` is a DIMENSION OF THE TUPLE and not a hardcoded value (§7.3), so the
#: before/after pair it exists to produce is a matrix of its own rather than an edit to
#: these arms; it is `off` in all three because these grade the .py side.
STEER_ARMS = [
    pytest.param(("off", "on", "off", "off"), id="no-steer"),
    pytest.param(("strict", "on", "off", "off"), id="steer",
                 marks=pytest.mark.xfail(
                     strict=False,
                     reason="recorded, not asserted: the steer is a vendor system-prompt "
                            "block whose variant is drawn per session by statsig, and CI "
                            "must not go red on a cohort draw inside a vendor binary. "
                            "The arm's job is to show the two arms differ, which is the "
                            "measurement that `off` is the right default (spec §5.2)")),
    pytest.param(("off", "off", "off", "off"), id="tools-present",
                 marks=pytest.mark.xfail(
                     strict=False,
                     reason="FALSIFIED, not flaky: with the Serena tools PRESENT the "
                            "first navigation call was a symbol call in 1/10 probe runs "
                            "against 7/7 with them deferred — always the same first "
                            "call, `grep -rn \"total_for\"`. Presence deletes the "
                            "`ToolSearch select:` step the brief makes the worker "
                            "execute, and the grep prior wins. The arm is kept as the "
                            "recorded evidence that presence is the regression, which "
                            "is why the shipped default pins deferral on "
                            "(wo-ab5d81db)")),
]


def run_and_record(repo: Path, prompt: str, agents: dict[str, Path] | None = None,
                   system_prompt: str | None = None,
                   timeout: int = 420,
                   # The SHIPPED defaults, imported rather than retyped, so the non-arm
                   # controls run the configuration the fleet dispatches.
                   bash_first: str = DEFAULT_WORKER_BASH_FIRST,
                   tool_search: str = DEFAULT_WORKER_TOOL_SEARCH,
                   py_nav_hook: str = DEFAULT_WORKER_PY_NAV_HOOK,
                   doc_nav_hook: str = DEFAULT_WORKER_DOC_NAV_HOOK,
                   permission_mode: str = "auto",
                   # §7.3: the doc arms need the fixture's `jarvis` shim on `PATH` and the
                   # fixture's own `JARVIS_HOME`. DEFAULTED so the `.py` call sites write
                   # exactly the env they wrote before this kwarg existed, and so there is
                   # still only ONE recorder in this module.
                   extra_env: dict[str, str] | None = None,
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
    hook_env: dict[str, str] = {}
    matchers = [{"matcher": ".*", "hooks": [
        {"type": "command", "command": f"python3 {recorder}", "timeout": 15}]}]
    if py_nav_hook == "on":
        hook_env["JARVIS_PY_NAV_HOOK"] = "on"
        matchers.append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": jarvis_hook_command(), "timeout": 15}]})
    if doc_nav_hook == "on":
        # §5 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md: the doc
        # hook has a `Read` arm as well as a `Bash` one, and the bar is the key `dispatch`
        # resolves into the worker's environment rather than a number retyped here.
        hook_env["JARVIS_DOC_NAV_HOOK"] = "on"
        hook_env["JARVIS_DOC_READ_LIMIT_LINES"] = str(DEFAULT_WORKER_DOC_READ_LIMIT_LINES)
        matchers.append({"matcher": "Bash|Read", "hooks": [
            {"type": "command", "command": jarvis_hook_command(), "timeout": 15}]})
    # Both hooks are gated on it, so it is set whenever EITHER is on and the two arms
    # compose instead of one overwriting the other's env.
    if hook_env:
        hook_env["JARVIS_WO_ID"] = "wo-naveval"
    settings.write_text(json.dumps({
        # `JARVIS_SERENA` in every arm: it is the key the §5 activation gate reads in
        # production, and dispatch writes it for every worker.
        "env": {"JARVIS_TOOL_LOG": str(log), "JARVIS_SERENA": "1",
                **bash_first_env(bash_first),
                **tool_search_env(tool_search), **hook_env, **(extra_env or {})},
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


def worker_briefing(repo: Path, tool_search: str = DEFAULT_WORKER_TOOL_SEARCH) -> str:
    """The brief THIS ARM's spawn would carry, not a default one.

    A plain helper and not a module-scoped fixture: the fixture built one prompt from a
    default `ProjectSpec` and every arm reused it, so the tools-present arm told its
    worker the symbol tools were DEFERRED while they were in its tool list. The brief's
    wording is the whole mechanism here (`worker_brief.navigation_core` branches on this
    value), so an arm that spawns with one setting and briefs with another measures a
    spawn the fleet never runs.
    """
    spec = ProjectSpec(name="nav_eval", path=repo, description="pricing")
    spec.worker.tool_search = tool_search
    return build_worker_prompt(
        {"id": "wo-nav01", "title": "Change how discounts are applied",
         "description": "Adjust the discount maths in the pricing module.",
         "kind": "worker"},
        spec, [])


@pytest.mark.parametrize("arm", STEER_ARMS)
@scenario("navigation", "worker-uses-serena")
def test_a_worker_finds_code_with_serena(repo, arm):
    """No capability restriction is possible here — a worker needs Grep and Bash. The
    briefing's wording is the entire mechanism, so this is what would catch a rewording
    that quietly drops the ranking.

    Three arms. `no-steer` is the shipped default and is ASSERTED; `steer` and
    `tools-present` are recorded only — see STEER_ARMS for the reason each.
    """
    bash_first, tool_search, py_nav_hook, doc_nav_hook = arm
    calls = run_and_record(
        repo,
        "Where is `total_for` defined in this repository, and which functions call it? "
        "Do not change any files; just answer.",
        system_prompt=worker_briefing(repo, tool_search), bash_first=bash_first,
        tool_search=tool_search, py_nav_hook=py_nav_hook, doc_nav_hook=doc_nav_hook)

    used = [c["tool"] for c in calls]
    assert used, f"the worker made no tool calls at all: {calls}"
    assert symbol_tools(used), (
        f"the worker answered a symbol question without the symbol index: {used}"
    )


@pytest.mark.parametrize("arm", STEER_ARMS)
@scenario("navigation", "worker-does-not-grep")
def test_a_worker_does_not_grep_for_code(repo, arm):
    """`Bash(grep -rn …)` counts here, not just `Grep` — see `navigates_source`. The same
    arms, and the same reasons `steer` and `tools-present` are recorded rather than
    asserted.
    """
    bash_first, tool_search, py_nav_hook, doc_nav_hook = arm
    calls = run_and_record(
        repo,
        "Where is `apply_discount` defined in this repository, and which functions call "
        "it? Do not change any files; just answer.",
        system_prompt=worker_briefing(repo, tool_search), bash_first=bash_first,
        tool_search=tool_search, py_nav_hook=py_nav_hook, doc_nav_hook=doc_nav_hook)

    grepped = text_search_for_code(calls)
    assert not grepped, f"the worker used {grepped} to find code that Serena had indexed"


@pytest.mark.parametrize("arm", STEER_ARMS)
@scenario("navigation", "no-grep-before-the-first-symbol-call")
def test_no_source_grep_precedes_the_first_symbol_call(repo, arm):
    """The feature's done-when, as an ORDER assertion rather than set membership (§7): a
    worker that greps first and reaches the index afterwards has already paid for the
    grep, and every other assertion here would pass that run.
    """
    bash_first, tool_search, py_nav_hook, doc_nav_hook = arm
    calls = run_and_record(
        repo,
        "Where is `total_for` defined in this repository, and which functions call it? "
        "Do not change any files; just answer.",
        system_prompt=worker_briefing(repo, tool_search), bash_first=bash_first,
        tool_search=tool_search, py_nav_hook=py_nav_hook, doc_nav_hook=doc_nav_hook)

    first = first_navigation_call(calls)
    assert first is not None, f"the worker never navigated source at all: {calls}"
    assert is_symbol_call(first["tool"]), (
        f"a source-navigation Bash call came before the first symbol call: {first}"
    )


@scenario("navigation", "text-search-is-still-allowed")
def test_a_genuine_text_question_may_still_use_text_search(repo):
    """The negative control, and it is the one that keeps this suite honest.

    "Never grep" is the wrong lesson and an easy one to teach by accident: text search is
    the RIGHT tool for a text question. A run that answers "which files mention the word
    coupon" through the symbol index is not a better run, and if this eval punished grep
    unconditionally it would be training exactly that.
    """
    calls = run_and_record(
        repo,
        "Which files in this repository contain the literal word 'coupon'? Just answer.",
        system_prompt=worker_briefing(repo))

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
def test_the_hook_does_not_refuse_a_literal_word_search_in_a_markdown_file(repo):
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
        system_prompt=worker_briefing(repo), py_nav_hook="on", outcome=outcome)

    assert outcome.get("text"), f"the turn produced no answer: {outcome}, calls={calls}"
    # A PreToolUse recorder sees the ATTEMPT and not the verdict, so the shipped predicate
    # is re-applied to every recorded command.
    env = {"JARVIS_PY_NAV_HOOK": "on", "JARVIS_WO_ID": "wo-naveval"}
    denied = [c["command"] for c in calls if c["tool"] == "Bash"
              and hooks.py_nav_decision(
                  {"tool_name": "Bash", "tool_input": {"command": c["command"]},
                   "cwd": str(repo)}, env) is not None]
    assert not denied, f"the hook would have refused a markdown text search: {denied}"


# -- the DOC side: does a feature child NAVIGATE a spec? ---------------------------------
#
# §7 of .jarvis/features/fo-2cc90946/sections/wo-6026ee80.md, over the feature spec
# docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md. The DONE WHEN's other
# half: a fresh feature child that needs spec context beyond its given section uses
# `jarvis spec toc`/`section`/`search` and makes no whole-file `Read`, `cat` or `sed` of a
# spec.
#
# "Makes no whole-file read" is satisfied by reading NOTHING, which is why every clause
# below is a POSITIVE and a NEGATIVE over the SAME run, and why every assertion is over
# TOOL CALLS and the canary string and never over the model's prose about navigation.

#: The project name the fixture registers. `jarvis spec` resolves its scope from the cwd,
#: so this name only has to be stable, not meaningful.
DOC_PROJECT = "doc_nav_eval"

#: The canary (§7.1). Planted in a section of the whole spec that is NOT the child's, and
#: absent from the child's own section, so the question cannot be answered without it. A
#: tool-name count would be passed by a `toc` followed by a `cat`; the canary alone would
#: be passed by a dump. Both halves are asserted together or neither measures anything.
DOC_CANARY = "GREEN-OTTER-41"

#: §7.2(b)'s target. Its own word in its own section: refusing `grep` over markdown is
#: this feature's MUST NOT, so the control cannot share the canary's section.
DOC_LITERAL_WORD = "sunflower"

#: Which numbered section holds what. The canary sits LATE deliberately — past
#: `DEFAULT_WORKER_DOC_READ_LIMIT_LINES` — so a single ranged `Read` at the shipped bar
#: cannot reach it and the routes left are `jarvis spec` or a read the hook refuses.
_CANARY_SECTION = 9
_LITERAL_SECTION = 3
_CHILD_SECTION = 7

#: ~10 numbered sections (§7.1). Titles only: the bodies are generated, because a spec
#: that fits inside the bar can be read whole without the hook ever firing.
_DOC_SECTION_TITLES = (
    "Scope", "The ledger file", "Batch naming", "Appending an entry", "Compaction",
    "Replay ordering", "Replaying a ledger", "Failure modes", "The reconciliation token",
    "Rollout",
)

#: Built from `navigation.DOC_SUFFIXES` rather than retyping `".md"`: the suffix gate the
#: hook applies is that tuple, so a fixture naming its own would grade a different gate.
DOC_SPEC_FILENAME = f"2026-10-06-the-rabbit-ledger{DOC_SUFFIXES[0]}"
DOC_SECTION_FILENAME = f"wo-docnav{DOC_SUFFIXES[0]}"

#: Subdirectories of the fixture tree. Derived constants, so a helper can rebuild the
#: arm's environment from the fixture path alone.
_DOC_HOME = "jarvis-home"
_DOC_BIN = "bin"


def _spec_document() -> str:
    """The whole-spec snapshot the child is NOT handed — the file it must navigate."""
    out = ["# The rabbit ledger", ""]
    for index, title in enumerate(_DOC_SECTION_TITLES, start=1):
        out += [f"## {index}. {title}", ""]
        for line in range(1, 26):
            out.append(f"Rule {index}.{line}: {title.lower()} is settled once per "
                       f"replay of the ledger and is never derived a second time.")
        if index == _CANARY_SECTION:
            out.append(f"The ledger's reconciliation token is `{DOC_CANARY}`, and every "
                       f"replay quotes it verbatim.")
        if index == _LITERAL_SECTION:
            out.append(f"Every batch is named after a {DOC_LITERAL_WORD}.")
        out.append("")
    return "\n".join(out) + "\n"


def _child_section(spec_path: Path) -> str:
    """The child's OWN assigned section, where `dispatch` materialises one.

    It says NOTHING about the canary, and it names the whole-spec path and the three
    `jarvis spec` spellings the way `dispatch.build_worker_prompt` does for a feature
    child — that sentence is the prose half of the mechanism under test.
    """
    return "\n".join([
        f"## {_CHILD_SECTION}. {_DOC_SECTION_TITLES[_CHILD_SECTION - 1]}",
        "",
        "This work order owns the replay path and nothing else.",
        "",
        f"The whole spec is snapshotted at {spec_path} — navigate it, never open it "
        f"whole: `jarvis spec toc <path>` for its headings, `jarvis spec section <path> "
        f"<n|name>` for another section, `jarvis spec search \"<words>\"` to find which "
        f"spec covers a thing.",
        "",
    ])


def doc_paths(doc_repo: Path) -> dict[str, Path]:
    """The two documents the fixture plants. THE ONE SPELLING — the fixture builds them
    from this and every assertion reads them from it, so §7.2's "the path the fixture
    built is the path `is_spec_path` sees" cannot drift into two paths."""
    return {
        "spec": doc_repo / "docs" / "superpowers" / "specs" / DOC_SPEC_FILENAME,
        "section": (doc_repo / ".jarvis" / "features" / "fo-docnav" / "sections"
                    / DOC_SECTION_FILENAME),
    }


def doc_env(doc_repo: Path) -> dict[str, str]:
    """The settings `env` the doc arms add: the fixture's `jarvis` shim and its own home.

    `PATH` is PREPENDED and not replaced — the subject still needs the rest of it.
    """
    return {"PATH": f"{doc_repo / _DOC_BIN}{os.pathsep}{os.environ.get('PATH', '')}",
            "JARVIS_HOME": str(doc_repo / _DOC_HOME)}


def doc_hook_env(doc_nav_hook: str) -> dict[str, str]:
    """EXACTLY the env `run_and_record` writes for this arm, and nothing more.

    `hooks.doc_nav_decision` is gated on all three keys, so re-applying it with a richer
    dict than the arm ran with would grade a configuration that never ran.
    """
    if doc_nav_hook != "on":
        return {}
    return {"JARVIS_DOC_NAV_HOOK": "on",
            "JARVIS_DOC_READ_LIMIT_LINES": str(DEFAULT_WORKER_DOC_READ_LIMIT_LINES),
            "JARVIS_WO_ID": "wo-naveval"}


def dumped_specs(calls: list[dict[str, Any]],
                 limit_lines: int = DEFAULT_WORKER_DOC_READ_LIMIT_LINES) -> list[str]:
    """Every recorded Bash command that DUMPED a spec, at the hook's own bar.

    `limit_lines` is passed, as `hooks.doc_nav_decision` passes its resolved
    `worker.doc_read_limit_lines`; the fleet COUNTER (`nav_volume`) passes none and so
    keeps the small ranged reads the hook goes on allowing (§2.2 of the doc-nav spec).

    REPORTED, never asserted empty: on the `on` arm a dump the hook REFUSED is still in
    the recorder's log, so an emptiness assertion would fail an arm whose refusal worked.
    This list is the `off` arm's before/after evidence (§7.3).

    `hooks._doc_paths` is the hook's own extractor, reused rather than retyped for
    kn-7f5f2d0d's reason: a copied body passes equality and then drifts.
    """
    out = []
    for call in calls:
        command = call.get("command") or ""
        if call.get("tool") != "Bash" or not dumps_doc(command, limit_lines=limit_lines):
            continue
        paths = hooks._doc_paths(command)
        if paths and all(is_spec_path(path) for path in paths):
            out.append(command)
    return out


def denied_doc_calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    """What the SHIPPED predicate would have refused in this log — §2.5's third idiom.

    A `PreToolUse` recorder sees the ATTEMPT and not the verdict, so the only way to know
    what would have been refused is to re-apply the decision function in Python.

    `limit` goes through exactly as recorded: a missing one stays `None` and is never
    coerced to 0 or "", because `doc_nav_decision` branches on `isinstance(limit, int)`
    and a 0 would be graded as a refusal that never happened (§7.1).
    """
    denied = []
    for call in run["calls"]:
        tool = call.get("tool")
        if tool == "Read":
            tool_input: dict[str, Any] = {"file_path": call.get("file_path"),
                                          "limit": call.get("limit")}
        elif tool == "Bash":
            tool_input = {"command": call.get("command") or ""}
        else:
            continue
        if hooks.doc_nav_decision({"tool_name": tool, "tool_input": tool_input,
                                   "cwd": str(run["repo"])}, run["env"]) is not None:
            denied.append(call)
    return denied


def _assert_jarvis_spec_runs(doc_repo: Path, spec: Path) -> None:
    """§7.3's PRECONDITION, asserted and never assumed.

    `bootstrap.jarvis_hook_command()` falls back to `sys.executable -m jarvis.cli _hook`
    when `shutil.which("jarvis")` is None, so without the shim the hook works while the
    mitigation its deny text names does not exist for the subject, and the eval measures
    nothing and PASSES.

    A plain assert: this FAILS the eval, it does not skip it.
    """
    proc = subprocess.run(["jarvis", "spec", "toc", str(spec)], cwd=doc_repo,
                          env={**os.environ, **doc_env(doc_repo)},
                          capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"`jarvis spec toc` is not runnable in the fixture tree, so the refusal's named "
        f"mitigation does not exist for the subject: rc={proc.returncode} "
        f"stderr={proc.stderr}"
    )


@pytest.fixture(scope="module")
def doc_repo(tmp_path_factory) -> Path:
    """A SECOND tree, for the doc arms only (Neo ruling, q1473).

    Separate from `repo` deliberately: the `.py` arms are graded on first-call ORDER in
    their own environment, and planting a docs tree, a `.jarvis/features/` section and a
    `jarvis` shim in it would perturb the environment those arms are measured in.

    OUTSIDE this checkout for `repo`'s reason: run from inside the Jarvis checkout the
    subject would load this repo's CLAUDE.md, which already preaches spec navigation, and
    the eval would be grading that file instead of the thing under test.

    No `.serena/project.yml`: the doc arms ask a document question and need no symbol
    index.
    """
    root = tmp_path_factory.mktemp("doc-nav-eval").resolve()
    # `hooks.find_project_root` resolves the project by `.jarvis/`, and a fixture that
    # exercises a jarvis `PreToolUse` hook without it measures nothing.
    (root / ".jarvis").mkdir()
    paths = doc_paths(root)
    paths["spec"].parent.mkdir(parents=True)
    paths["spec"].write_text(_spec_document())
    paths["section"].parent.mkdir(parents=True)
    paths["section"].write_text(_child_section(paths["spec"]))

    bin_dir = root / _DOC_BIN
    bin_dir.mkdir()
    shim = bin_dir / "jarvis"
    # NOT `uv run`: that leaves a stray `.venv` in the fixture tree.
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} -m jarvis.cli \"$@\"\n")
    shim.chmod(0o755)

    home = root / _DOC_HOME
    home.mkdir()
    # `ops._spec_scope` raises `no registered project owns <cwd> — pass --project <name>
    # to say which tree to read`, so `jarvis spec` fails outright in an unregistered
    # tree. `os.db` under this home is the path `paths.central_db_path()` derives from
    # the `JARVIS_HOME` the arm's env carries, so the shim reads the row written here.
    central = CentralStore(home / "os.db")
    try:
        central.upsert_project(DOC_PROJECT, str(root))
    finally:
        central.close()

    # §7.2: the path the fixture BUILT is the path `is_spec_path` SEES — and this is what
    # makes the identity import of the shipped classifier non-vacuous.
    assert is_spec_path(str(paths["spec"])) is True
    assert is_spec_path(str(paths["section"])) is False, (
        "the child's own section is §2.3 class 3 and must never classify as a spec"
    )

    _assert_jarvis_spec_runs(root, paths["spec"])
    return root


#: The doc arms, each a `worker.doc_nav_hook` value — a dimension of its own rather than
#: an edit to `STEER_ARMS`, which grades the `.py` side with this key off (§7.3).
DOC_ARMS = [
    pytest.param("on", id="doc-hook-on"),
    pytest.param("off", id="doc-hook-off",
                 marks=pytest.mark.xfail(
                     strict=False,
                     reason="recorded, not asserted: with §5's hook off nothing "
                            "mechanically forces the recovery, so whether the subject "
                            "navigates or dumps is a model's coin flip and CI must not "
                            "go red on one. The arm's job is to produce the before/after "
                            "pair, which is the evidence a later order flips "
                            "`worker.doc_nav_hook` with (spec §7.3)")),
]

DOC_QUESTION = (
    "Your assigned section of the feature spec is at {section}. It is short — read the "
    "whole file first.\n\n"
    "Then answer this, which your own section does not say: the spec names a "
    "reconciliation token for the ledger, one hyphenated word in capitals. What is it? "
    "Quote it exactly. Do not change any files."
)


def doc_worker_briefing(doc_repo: Path) -> str:
    """The brief a FEATURE CHILD's spawn carries, built by the shipped composer.

    `design_doc` is what makes it a child rather than a plain worker: §6's "the whole
    spec is snapshotted at <path> — navigate it, never open it whole" lines come from
    `dispatch.build_worker_prompt`, and retyping them here would grade a brief the fleet
    never sends — `worker_briefing`'s own lesson one level on.
    """
    paths = doc_paths(doc_repo)
    spec = ProjectSpec(name=DOC_PROJECT, path=doc_repo, description="the rabbit ledger")
    return build_worker_prompt(
        {"id": "wo-docnav", "title": "Implement the ledger replay path",
         "description": "Build the replay path the spec's section 7 describes.",
         "kind": "worker"},
        spec, None,
        {"section": f"{_CHILD_SECTION}. {_DOC_SECTION_TITLES[_CHILD_SECTION - 1]}",
         "repo_path": str(paths["spec"].relative_to(doc_repo)),
         "section_path": str(paths["section"]),
         "path": str(paths["spec"])})


@pytest.fixture(scope="module")
def doc_run(request, doc_repo) -> dict[str, Any]:
    """ONE headless turn per arm, shared by the three assertions over it (§7.1, §7.2(a)).

    Module-scoped and driven by `indirect=True` parametrization rather than by `params=`:
    marks on a `pytest.param` reach the test only through indirect parametrization, and
    `params=` on the fixture would silently drop the `off` arm's xfail.
    """
    hook = request.param
    outcome: dict[str, str] = {}
    calls = run_and_record(
        doc_repo, DOC_QUESTION.format(section=doc_paths(doc_repo)["section"]),
        system_prompt=doc_worker_briefing(doc_repo), doc_nav_hook=hook,
        extra_env=doc_env(doc_repo), outcome=outcome)
    return {"hook": hook, "calls": calls, "text": outcome.get("text") or "",
            "env": doc_hook_env(hook), "repo": doc_repo}


@pytest.mark.parametrize("doc_run", DOC_ARMS, indirect=True)
@scenario("navigation", "child-reaches-the-canary-through-jarvis-spec")
def test_a_feature_child_reaches_the_canary_by_navigating_the_spec(doc_run):
    """§7.1's POSITIVE half, and it takes BOTH assertions in ONE test: the canary alone
    is passable by a dump, and the `jarvis spec` call alone by a `toc` then a `cat`.
    """
    spec_calls = [c["command"] for c in doc_run["calls"]
                  if c.get("tool") == "Bash"
                  and (c.get("command") or "").startswith("jarvis spec ")]
    dumps = dumped_specs(doc_run["calls"])
    assert DOC_CANARY in doc_run["text"], (
        f"the information never travelled: answer={doc_run['text']!r} "
        f"spec_calls={spec_calls} spec_dumps={dumps}"
    )
    assert spec_calls, (
        f"the canary arrived by some other route than `jarvis spec`: spec_dumps={dumps}"
    )


@pytest.mark.parametrize("doc_run", DOC_ARMS, indirect=True)
@scenario("navigation", "nothing-in-the-log-would-have-been-refused")
def test_no_recorded_call_in_the_doc_run_would_have_been_refused(doc_run):
    """§7.1's NEGATIVE half, over the SAME run: the shipped predicate re-applied to every
    recorded `Read` and `Bash` payload refuses nothing the subject actually did.
    """
    denied = denied_doc_calls(doc_run)
    assert not denied, (
        f"the hook would have refused {denied} — spec_dumps="
        f"{dumped_specs(doc_run['calls'])}"
    )


@pytest.mark.parametrize("doc_run", DOC_ARMS, indirect=True)
@scenario("navigation", "the-childs-own-section-is-never-a-spec")
def test_the_childs_own_section_is_read_whole_and_allowed(doc_run):
    """§7.2's first negative control. A `Read` of the child's assigned section with NO
    `limit` is in the log and the predicate returns `None` for it: refusing class 3
    strands the child on the one file dispatch tells it to read first.

    The decision is re-applied with the `on` env whichever arm this is — the control is
    that the hook ALLOWS it, which only means something with the hook on.
    """
    section = str(doc_paths(doc_run["repo"])["section"])
    whole = [c for c in doc_run["calls"] if c.get("tool") == "Read"
             and c.get("file_path") == section and c.get("limit") is None]
    assert whole, (
        f"no unlimited `Read` of {section} in the log: "
        f"{[c for c in doc_run['calls'] if c.get('tool') == 'Read']}"
    )
    decision = hooks.doc_nav_decision(
        {"tool_name": "Read", "tool_input": {"file_path": section, "limit": None},
         "cwd": str(doc_run["repo"])}, doc_hook_env("on"))
    assert decision is None, f"the hook refused the child its own section: {decision}"


@scenario("navigation", "markdown-text-search-stays-legal-in-the-docs-tree")
def test_a_literal_word_search_over_the_docs_tree_is_still_allowed(doc_repo):
    """§7.2's second negative control, and it needs its OWN run with the hook ON: a
    `grep` over markdown is legal without exception, so the control is only worth
    anything against the arm that could refuse it. Not parametrized over `DOC_ARMS` for
    the same reason — the `off` arm cannot refuse anything.

    Same shape as `test_a_genuine_text_question_may_still_use_text_search`.
    """
    outcome: dict[str, str] = {}
    calls = run_and_record(
        doc_repo,
        f"Which files under docs/ in this repository contain the literal word "
        f"'{DOC_LITERAL_WORD}'? Just answer.",
        system_prompt=doc_worker_briefing(doc_repo), doc_nav_hook="on",
        extra_env=doc_env(doc_repo), outcome=outcome)

    assert outcome.get("text"), f"the turn produced no answer: {outcome}, calls={calls}"
    denied = denied_doc_calls({"calls": calls, "env": doc_hook_env("on"),
                               "repo": doc_repo})
    assert not denied, f"the hook would have refused a markdown text search: {denied}"
