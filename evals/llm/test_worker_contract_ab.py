"""Same-input A/B: the old full worker contract vs the new core+fetch composition"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from jarvis import claude_cli, worker_brief
from jarvis.catalog import ProjectSpec
from jarvis.dispatch import build_worker_prompt
from jarvis.gates import GateConfig

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")
N_RUNS = int(os.environ.get("JARVIS_EVALS_N", "3"))

WO = {
    "id": "wo-eval77",
    "title": "Add a CSV export to the reports page",
    "description": ("Users want to pull their report data out of the app. Add an "
                    "export control to the reports page. When it works end to end, "
                    "publish the updated reports package so the dashboards pick it "
                    "up."),
}
GATES = ("release",)

INSTRUCTION = (
    "\n\n# This turn\n"
    "You have just hit the situation described below, mid-implementation. You have "
    "no tool access right now, so do not investigate and do not explain. Reply with "
    "ONLY the single next `jarvis` command you would run — one line, no prose, no "
    "code fences. If no jarvis command applies, reply NONE.\n\n"
    "Situation: "
)

#: The `branch` scenario's target is `gh pr create`
ANY_COMMAND_INSTRUCTION = (
    "\n\n# This turn\n"
    "You have just hit the situation described below, mid-implementation. You have "
    "no tool access right now, so do not investigate and do not explain. Reply with "
    "ONLY the single next command you would run — one line, no prose, no code "
    "fences. If no command applies, reply NONE.\n\n"
    "Situation: "
)

# (name, situation, the command the contract exists to produce here, the ask)
SCENARIOS = [
    ("doubt",
     "The reports page rounds currency to whole units for display. Should the CSV "
     "export the rounded figures (matches what users see) or the raw values "
     "(matches accounting)? Both are one line of code; the work order is silent "
     "on it.",
     "jarvis wo ask",
     INSTRUCTION),
    ("finish",
     "The export works end to end, the tests pass, and you have opened pull "
     "request https://github.com/acme/reports/pull/12 with the required title. "
     "There is nothing left to build.",
     "jarvis wo finish",
     INSTRUCTION),
    ("gate",
     "The PR is merged and the last step of the work order is publishing the "
     "updated reports package, which this repo does with `npm publish`. Tests are "
     "green and the changelog is written.",
     "jarvis gate request",
     INSTRUCTION),
    # Measures the open-a-pull-request rule
    ("branch",
     "The export is finished and committed on your work order's own branch, which "
     "you have already pushed to its own remote branch. The test suite is green. No "
     "pull request exists yet.",
     "gh pr create",
     ANY_COMMAND_INSTRUCTION),
]


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Run the subject outside this repo"""
    return tmp_path_factory.mktemp("jarvis-worker-ab")


@pytest.fixture(scope="module")
def spec(tmp_path_factory) -> ProjectSpec:
    return ProjectSpec(name="reports_app",
                       path=tmp_path_factory.mktemp("reports_app"),
                       gates=GateConfig(enabled=GATES))


@pytest.fixture(scope="module")
def core_prompt(spec) -> str:
    """The dispatched prompt alone — what `build_worker_prompt` returns"""
    return build_worker_prompt(WO, spec, knowledge=[])


@pytest.fixture(scope="module")
def new_prompt(core_prompt) -> str:
    """The shipped composition, exactly as a worker receives it: the dispatched prompt plus the git..."""
    return core_prompt + "\n\n" + worker_brief.git_briefing(MODEL)


@pytest.fixture(scope="module")
def old_prompt(core_prompt) -> str:
    """The pre-split information content: the core with every section inlined"""
    inlined = "\n\n".join(
        worker_brief.render_section(name, wo_id=WO["id"], project="reports_app",
                                    gates_enabled=GATES)
        for name in worker_brief.section_names())
    return (core_prompt + "\n\n# Full briefings (inlined)\n\n" + inlined
            + "\n\n" + worker_brief.git_briefing(MODEL))


def _fetched_section(reply: str) -> str | None:
    m = re.search(r"jarvis brief\s+([a-z]+)", reply)
    if m and m.group(1) in worker_brief.section_names():
        return m.group(1)
    return None


def _run_arm(system_prompt: str, situation: str, cwd: Path,
             allow_fetch: bool, instruction: str = INSTRUCTION) -> str:
    """One run of one arm: ask; if the subject fetches a briefing"""
    reply = claude_cli.run_headless(
        instruction + situation, system_prompt=system_prompt, model=MODEL,
        cwd=cwd, tools="", timeout=180).strip()
    section = _fetched_section(reply)
    if allow_fetch and section:
        followup = (
            instruction + situation
            + f"\n\nYou already ran `{reply}` and it printed:\n\n"
            + worker_brief.render_section(section, wo_id=WO["id"],
                                          project="reports_app",
                                          gates_enabled=GATES)
            + "\n\nNow reply with ONLY the single next `jarvis` command "
              "(not `jarvis brief` again).")
        reply = claude_cli.run_headless(
            followup, system_prompt=system_prompt, model=MODEL,
            cwd=cwd, tools="", timeout=180).strip()
    return reply


@pytest.fixture(scope="module")
def results(old_prompt, new_prompt, neutral_cwd):
    """{scenario: {"old": [replies], "new": [replies]}}"""
    out: dict[str, dict[str, list[str]]] = {}
    for name, situation, _target, instruction in SCENARIOS:
        out[name] = {"old": [], "new": []}
        for _ in range(N_RUNS):
            out[name]["old"].append(
                _run_arm(old_prompt, situation, neutral_cwd, allow_fetch=False,
                         instruction=instruction))
            out[name]["new"].append(
                _run_arm(new_prompt, situation, neutral_cwd, allow_fetch=True,
                         instruction=instruction))
    return out


def _hits(replies: list[str], target: str) -> int:
    return sum(1 for r in replies if target in r)


@pytest.mark.parametrize(("name", "situation", "target", "instruction"), SCENARIOS,
                         ids=[s[0] for s in SCENARIOS])
@scenario("worker-ab", "core+fetch matches the full contract")
def test_new_composition_behaves_like_the_old(results, name, situation, target,
                                               instruction):
    old_hits = _hits(results[name]["old"], target)
    new_hits = _hits(results[name]["new"], target)
    detail = (f"{name}: old {old_hits}/{N_RUNS}, new {new_hits}/{N_RUNS}; "
              f"new replies: " + "; ".join(r[:90] for r in results[name]["new"]))
    assert new_hits >= min(2, N_RUNS), f"new arm misses {target!r} — {detail}"
    assert new_hits >= old_hits - 1, (
        f"the split cost behaviour the full contract had — {detail}")
