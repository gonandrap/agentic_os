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
from pathlib import Path

import pytest

from jarvis.catalog import ProjectSpec
from jarvis.dispatch import build_worker_prompt, serena_allow_rules

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

ASSETS = Path(__file__).resolve().parents[2] / "src" / "jarvis" / "assets"

# What "grepped for code" looks like in a tool log. `Bash` is not here: a worker runs all
# sorts of legitimate shell, and only the built-in search tools are unambiguous evidence
# that it went looking for code by text.
TEXT_SEARCH_TOOLS = {"Grep", "Glob"}


def is_serena(tool: str) -> bool:
    return tool.startswith("mcp__serena__") or tool.startswith("mcp__plugin_serena_serena__")


def symbol_tools(tools: list[str]) -> list[str]:
    """Calls that answer a SYMBOL question rather than a text one.

    `search_for_pattern` is excluded on purpose — it is Serena's *text* search, so
    counting it would let a run that merely swapped one text search for another pass as a
    win, which is the easiest way for an eval like this to become vacuous.

    `LSP` IS counted, and that is a deliberate widening rather than a loosening. This
    suite's property is "navigate by symbol index, not by text", and a pyright
    go-to-definition answers the question with the same authority Serena does — the fleet
    simply has two symbol indexes available and either beats grep. Observed for real: a
    worker asked where a function was defined and who called it reached for `LSP`, used no
    text search at all, and answered correctly. Failing that run would have been the eval
    teaching "use this specific vendor" when the thing worth teaching is "do not
    reconstruct symbols out of string matches".
    """
    return [t for t in tools
            if t == "LSP" or (is_serena(t) and t.rsplit("__", 1)[-1] in {
                "find_symbol", "find_referencing_symbols", "get_symbols_overview",
                "find_declaration", "find_implementations", "read_memory",
                "list_memories"})]


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

RECORDER = '''\
import json, os, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
with open(os.environ["JARVIS_TOOL_LOG"], "a") as f:
    f.write(json.dumps({"tool": d.get("tool_name") or "",
                        "agent": d.get("agent_type") or ""}) + "\\n")
print("{}")
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
    (root / ".serena").mkdir()
    (root / ".serena" / "project.yml").write_text(
        'project_name: "nav-eval"\n'
        'language_servers:\n- python\n'
        'ignore_all_files_in_gitignore: false\n'
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    return root


def run_and_record(repo: Path, prompt: str, agents: dict[str, Path] | None = None,
                   system_prompt: str | None = None,
                   timeout: int = 420) -> list[dict[str, str]]:
    """Run one headless turn and return every tool call it made, with its agent_type."""
    log = repo / "tool-log.jsonl"
    log.write_text("")
    recorder = repo / "record_tool.py"
    recorder.write_text(RECORDER)
    settings = repo / "eval-settings.json"
    settings.write_text(json.dumps({
        "env": {"JARVIS_TOOL_LOG": str(log)},
        # The SAME allow rules dispatch writes for a real worker, imported rather than
        # retyped. Naming a Serena tool in `tools:` makes it visible; permission is a
        # separate gate, and a headless turn cannot answer a prompt — probed live, a seat
        # without these had `activate_project` BLOCKED and gave up. An eval that granted
        # permissions dispatch does not would be measuring a worker the fleet never runs.
        "permissions": {"allow": serena_allow_rules()},
        "hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [
            {"type": "command", "command": f"python3 {recorder}", "timeout": 15}]}]},
    }))
    for name, src in (agents or {}).items():
        dest = repo / ".claude" / "agents" / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text())

    argv = ["claude", "-p", prompt, "--output-format", "json", "--model", MODEL,
            "--settings", str(settings), "--permission-mode", "acceptEdits"]
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=timeout)

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

    used = seat_calls(calls, seat)
    grepped = [t for t in used if t in TEXT_SEARCH_TOOLS]
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


@scenario("navigation", "worker-uses-serena")
def test_a_worker_finds_code_with_serena(repo, worker_briefing):
    """No capability restriction is possible here — a worker needs Grep and Bash. The
    briefing's wording is the entire mechanism, so this is what would catch a rewording
    that quietly drops the ranking."""
    calls = run_and_record(
        repo,
        "Where is `total_for` defined in this repository, and which functions call it? "
        "Do not change any files; just answer.",
        system_prompt=worker_briefing)

    used = [c["tool"] for c in calls]
    assert used, f"the worker made no tool calls at all: {calls}"
    assert symbol_tools(used), (
        f"the worker answered a symbol question without the symbol index: {used}"
    )


@scenario("navigation", "worker-does-not-grep")
def test_a_worker_does_not_grep_for_code(repo, worker_briefing):
    calls = run_and_record(
        repo,
        "Where is `apply_discount` defined in this repository, and which functions call "
        "it? Do not change any files; just answer.",
        system_prompt=worker_briefing)

    grepped = [c["tool"] for c in calls if c["tool"] in TEXT_SEARCH_TOOLS]
    assert not grepped, f"the worker used {grepped} to find code that Serena had indexed"


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
