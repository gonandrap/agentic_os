"""LLM-graded navigation evals: when an agent is asked about particular code"""

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

# The BUILT-IN search tools (docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md)
TEXT_SEARCH_TOOLS = {"Grep", "Glob"}


def is_serena(tool: str) -> bool:
    return tool.startswith("mcp__serena__") or tool.startswith("mcp__plugin_serena_serena__")


def text_search_for_code(calls: list[dict[str, str]]) -> list[str]:
    """Every call in this log that went looking for CODE by text"""
    found = []
    for c in calls:
        if c["tool"] in TEXT_SEARCH_TOOLS:
            found.append(c["tool"])
        elif c["tool"] == "Bash" and navigates_source(c.get("command") or "",
                                                     SOURCE_SUFFIXES, NAV_COMMANDS):
            found.append(f"Bash({c['command']})")
    return found


def symbol_tools(tools: list[str]) -> list[str]:
    """Calls that answer a SYMBOL question rather than a text one (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)"""
    return [t for t in tools if is_symbol_call(t)]


def first_navigation_call(calls: list[dict[str, str]]) -> dict[str, str] | None:
    """The first call in the ORDERED log that navigated source, either way"""
    for c in calls:
        if is_symbol_call(c.get("tool") or ""):
            return c
        if c.get("tool") == "Bash" and navigates_source(c.get("command") or "",
                                                       SOURCE_SUFFIXES, NAV_COMMANDS):
            return c
    return None


# -- the scratch repo the subject is asked about

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

# The negative control's target lives in a NON-SOURCE file: §6's hook must not refuse a literal-word...
NOTES = '''\
# Pricing notes

A coupon is applied before the total is computed.
'''

# The Bash COMMAND is recorded, not just the tool name: without it a `Bash(grep -rn …)` is...
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

# §5's activation line, which a real spawn gets from `settings.base.json`'s SessionStart /...
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
    """A tiny Serena-indexed python repo, outside this one"""
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
    # §6 (specs/2026-10-02-serena-the-cheap-path.md): py_nav_decision resolves project root by...
    (root / ".jarvis").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    return root


#: The arms, each a `(bash_first, tool_search, py_nav_hook, doc_nav_hook)` tuple
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
                   # The SHIPPED defaults, imported rather than retyped
                   bash_first: str = DEFAULT_WORKER_BASH_FIRST,
                   tool_search: str = DEFAULT_WORKER_TOOL_SEARCH,
                   py_nav_hook: str = DEFAULT_WORKER_PY_NAV_HOOK,
                   doc_nav_hook: str = DEFAULT_WORKER_DOC_NAV_HOOK,
                   permission_mode: str = "auto",
                   # §7.3: the doc arms need the fixture's `jarvis` shim on `PATH` and the fixture's own `JARVIS_HOME`
                   extra_env: dict[str, str] | None = None,
                   outcome: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Run one headless turn and return every tool call it made, with its agent_type"""
    log = repo / "tool-log.jsonl"
    log.write_text("")
    recorder = repo / "record_tool.py"
    recorder.write_text(RECORDER)
    activation = repo / "activation_context.py"
    activation.write_text(ACTIVATION)
    # `sys.executable`, not bare `python3`, so `jarvis` is importable in the hook process
    activation_matchers = [{"hooks": [
        {"type": "command", "command": f"{sys.executable} {activation}", "timeout": 15}]}]
    settings = repo / "eval-settings.json"
    # §6's hook is gated on `JARVIS_WO_ID`
    hook_env: dict[str, str] = {}
    matchers = [{"matcher": ".*", "hooks": [
        {"type": "command", "command": f"python3 {recorder}", "timeout": 15}]}]
    if py_nav_hook == "on":
        hook_env["JARVIS_PY_NAV_HOOK"] = "on"
        matchers.append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": jarvis_hook_command(), "timeout": 15}]})
    if doc_nav_hook == "on":
        # §5 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md: the doc hook has a `Read` arm...
        hook_env["JARVIS_DOC_NAV_HOOK"] = "on"
        hook_env["JARVIS_DOC_READ_LIMIT_LINES"] = str(DEFAULT_WORKER_DOC_READ_LIMIT_LINES)
        matchers.append({"matcher": "Bash|Read", "hooks": [
            {"type": "command", "command": jarvis_hook_command(), "timeout": 15}]})
    # Both hooks are gated on it, so it is set whenever EITHER is on and the two arms compose instead of...
    if hook_env:
        hook_env["JARVIS_WO_ID"] = "wo-naveval"
    settings.write_text(json.dumps({
        # `JARVIS_SERENA` in every arm: it is the key the §5 activation gate reads in production
        "env": {"JARVIS_TOOL_LOG": str(log), "JARVIS_SERENA": "1",
                **bash_first_env(bash_first),
                **tool_search_env(tool_search), **hook_env, **(extra_env or {})},
        # The SAME allow rules dispatch writes for a real worker, imported rather than retyped
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
        # `keep_default_context`: this eval's SUBJECT is the default environment
        text = claude_cli.run_headless(prompt, system_prompt=system_prompt,
                                       settings=settings,
                                       permission_mode=permission_mode, model=MODEL,
                                       cwd=repo, timeout=timeout, tools=None,
                                       keep_default_context=True)
        # The ANSWER as well as the log: a hook-twin test asserts the turn produced one
        if outcome is not None:
            outcome["text"] = text or ""
    except claude_cli.ClaudeCliError as exc:
        if outcome is not None:
            outcome["error"] = str(exc)
        pass  # the tool log is the measurement; a failed turn still made its calls

    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def seat_calls(calls: list[dict[str, str]], seat: str) -> list[str]:
    """Only what the SEAT invoked"""
    return [c["tool"] for c in calls if c.get("agent") == seat]


# -- the seats

SEAT_QUESTION = (
    "Use the Task tool to invoke the subagent type '{seat}' with this exact question, "
    "and report its answer verbatim:\n\n"
    "In this repository, where is the function `total_for` defined, and which functions "
    "call it? Answer with the file and the callers."
)


@pytest.mark.parametrize("seat", ["jarvis-architect", "jarvis-test-lead"])
@scenario("navigation", "seat-uses-serena")
def test_a_seat_finds_code_with_serena(repo, seat):
    """The seat has both Serena and Grep"""
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
    """The assertion the user asked for outright: no `Grep`"""
    calls = run_and_record(
        repo, SEAT_QUESTION.format(seat=seat),
        agents={seat: ASSETS / "agents" / f"{seat}.md"})

    seat_log = [c for c in calls if c.get("agent") == seat]
    grepped = text_search_for_code(seat_log)
    assert not grepped, f"{seat} used {grepped} to find code that Serena had indexed"


# -- an ordinary worker, held by prose alone


def worker_briefing(repo: Path, tool_search: str = DEFAULT_WORKER_TOOL_SEARCH) -> str:
    """The brief THIS ARM's spawn would carry, not a default one"""
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
    """No capability restriction is possible here — a worker needs Grep and Bash"""
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
    """`Bash(grep -rn …)` counts here, not just `Grep` — see `navigates_source`"""
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
    """The feature's done-when, as an ORDER assertion rather than set membership (§7): a worker that greps..."""
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
    """The negative control, and it is the one that keeps this suite honest"""
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
    """The negative control's hook twin: §6's hook is suffix-gated"""
    outcome: dict[str, str] = {}
    calls = run_and_record(
        repo,
        "Which lines of notes.md in this repository contain the literal word 'coupon'? "
        "Just answer.",
        system_prompt=worker_briefing(repo), py_nav_hook="on", outcome=outcome)

    assert outcome.get("text"), f"the turn produced no answer: {outcome}, calls={calls}"
    # A PreToolUse recorder sees the ATTEMPT and not the verdict
    env = {"JARVIS_PY_NAV_HOOK": "on", "JARVIS_WO_ID": "wo-naveval"}
    denied = [c["command"] for c in calls if c["tool"] == "Bash"
              and hooks.py_nav_decision(
                  {"tool_name": "Bash", "tool_input": {"command": c["command"]},
                   "cwd": str(repo)}, env) is not None]
    assert not denied, f"the hook would have refused a markdown text search: {denied}"


# -- the DOC side: does a feature child NAVIGATE a spec?

#: The project name the fixture registers
DOC_PROJECT = "doc_nav_eval"

#: The canary (§7.1)
DOC_CANARY = "GREEN-OTTER-41"

#: §7.2(b)'s target
DOC_LITERAL_WORD = "sunflower"

#: Which numbered section holds what
_CANARY_SECTION = 9
_LITERAL_SECTION = 3
_CHILD_SECTION = 7

#: ~10 numbered sections (§7.1)
_DOC_SECTION_TITLES = (
    "Scope", "The ledger file", "Batch naming", "Appending an entry", "Compaction",
    "Replay ordering", "Replaying a ledger", "Failure modes", "The reconciliation token",
    "Rollout",
)

#: Built from `navigation.DOC_SUFFIXES` rather than retyping `".md"`: the suffix gate the hook applies...
DOC_SPEC_FILENAME = f"2026-10-06-the-rabbit-ledger{DOC_SUFFIXES[0]}"
DOC_SECTION_FILENAME = f"wo-docnav{DOC_SUFFIXES[0]}"

#: Subdirectories of the fixture tree
_DOC_HOME = "jarvis-home"
_DOC_BIN = "bin"


def _spec_document() -> str:
    """The whole-spec snapshot the child is NOT handed — the file it must navigate"""
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
    """The child's OWN assigned section, where `dispatch` materialises one"""
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
    """The two documents the fixture plants"""
    return {
        "spec": doc_repo / "docs" / "superpowers" / "specs" / DOC_SPEC_FILENAME,
        "section": (doc_repo / ".jarvis" / "features" / "fo-docnav" / "sections"
                    / DOC_SECTION_FILENAME),
    }


def doc_env(doc_repo: Path) -> dict[str, str]:
    """The settings `env` the doc arms add: the fixture's `jarvis` shim and its own home"""
    return {"PATH": f"{doc_repo / _DOC_BIN}{os.pathsep}{os.environ.get('PATH', '')}",
            "JARVIS_HOME": str(doc_repo / _DOC_HOME)}


def doc_hook_env(doc_nav_hook: str) -> dict[str, str]:
    """EXACTLY the env `run_and_record` writes for this arm, and nothing more"""
    if doc_nav_hook != "on":
        return {}
    return {"JARVIS_DOC_NAV_HOOK": "on",
            "JARVIS_DOC_READ_LIMIT_LINES": str(DEFAULT_WORKER_DOC_READ_LIMIT_LINES),
            "JARVIS_WO_ID": "wo-naveval"}


def dumped_specs(calls: list[dict[str, Any]],
                 limit_lines: int = DEFAULT_WORKER_DOC_READ_LIMIT_LINES) -> list[str]:
    """Every recorded Bash command that DUMPED a spec, at the hook's own bar"""
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
    """What the SHIPPED predicate would have refused in this log — §2.5's third idiom"""
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
    """§7.3's PRECONDITION, asserted and never assumed"""
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
    """A SECOND tree, for the doc arms only (Neo ruling, q1473)"""
    root = tmp_path_factory.mktemp("doc-nav-eval").resolve()
    # `hooks.find_project_root` resolves the project by `.jarvis/`
    (root / ".jarvis").mkdir()
    paths = doc_paths(root)
    paths["spec"].parent.mkdir(parents=True)
    paths["spec"].write_text(_spec_document())
    paths["section"].parent.mkdir(parents=True)
    paths["section"].write_text(_child_section(paths["spec"]))

    bin_dir = root / _DOC_BIN
    bin_dir.mkdir()
    shim = bin_dir / "jarvis"
    # NOT `uv run`: that leaves a stray `.venv` in the fixture tree
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} -m jarvis.cli \"$@\"\n")
    shim.chmod(0o755)

    home = root / _DOC_HOME
    home.mkdir()
    # `ops._spec_scope` raises `no registered project owns <cwd>
    central = CentralStore(home / "os.db")
    try:
        central.upsert_project(DOC_PROJECT, str(root))
    finally:
        central.close()

    # §7.2: the path the fixture BUILT is the path `is_spec_path` SEES
    assert is_spec_path(str(paths["spec"])) is True
    assert is_spec_path(str(paths["section"])) is False, (
        "the child's own section is §2.3 class 3 and must never classify as a spec"
    )

    _assert_jarvis_spec_runs(root, paths["spec"])
    return root


#: The doc arms, each a `worker.doc_nav_hook` value
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
    """The brief a FEATURE CHILD's spawn carries, built by the shipped composer"""
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
    """ONE headless turn per arm, shared by the three assertions over it (§7.1, §7.2(a))"""
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
    """§7.1's POSITIVE half, and it takes BOTH assertions in ONE test: the canary alone is passable by a..."""
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
    """§7.1's NEGATIVE half, over the SAME run: the shipped predicate re-applied to every recorded `Read`..."""
    denied = denied_doc_calls(doc_run)
    assert not denied, (
        f"the hook would have refused {denied} — spec_dumps="
        f"{dumped_specs(doc_run['calls'])}"
    )


@pytest.mark.parametrize("doc_run", DOC_ARMS, indirect=True)
@scenario("navigation", "the-childs-own-section-is-never-a-spec")
def test_the_childs_own_section_is_read_whole_and_allowed(doc_run):
    """§7.2's first negative control"""
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
    """§7.2's second negative control"""
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
