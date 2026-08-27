"""Same-input A/B for the one-shot-turn rule: does the bullet change what a worker does?

The rule shipped as prose (`worker_brief.core_contract` plus the "A turn is one-shot"
block of `record_section`), and prose is only worth its tokens if a model briefed with
it behaves differently from one briefed without. The existing worker A/B cannot answer
that: `test_worker_contract_ab.py` composes BOTH of its arms from the shipped source, so
any new core bullet is in both and it measures the split, never the sentence. This file
is the missing pairing — arm WITHOUT is the shipped prompt with exactly this rule cut out,
arm WITH is the shipped prompt.

The scenario is wo-2df8828c's, which is what the rule was written for: work that does not
fit in one turn, and no way to hold the turn open for it. The contract's answer is to ask
(`record_section`: "If it fits in neither, say so and ask — a turn that ends on a question
is a turn the OS understands"), because a turn ending on a question is the one kind of
pause the OS understands. Without the rule there is nothing in the prompt that makes an
over-long command a decision at all, and wo-2df8828c backgrounded it and signed off.

A tie is a real result and is reported, not hidden: it would mean the core already routes
this through the general "any doubt goes to Neo" bullet and the new one is redundant.

Opt-in (spends tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_one_shot_turn_ab.py -q
    JARVIS_EVALS_MODEL=opus  # optional, default sonnet
    JARVIS_EVALS_N=5         # optional runs per arm, default 5
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from jarvis import claude_cli, worker_brief
from jarvis.catalog import ProjectSpec
from jarvis.dispatch import build_worker_prompt

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")
N_RUNS = int(os.environ.get("JARVIS_EVALS_N", "5"))

WO = {
    "id": "wo-eval91",
    "title": "Grade the validation panel with a synthetic LLM eval",
    "description": ("Add evals/llm/test_validation_judgment.py and its free harness "
                    "companion, then report the measured grades."),
}

#: The two edits under test, as the substrings that identify them in the rendered text.
#: Cutting by marker rather than by re-composing the module keeps arm WITHOUT byte-equal
#: to arm WITH everywhere else — the A/B is worthless if the arms drift anywhere but here.
CORE_BULLET_MARKER = "- **A turn is one-shot"
RECORD_BLOCK_MARKER = "# A turn is one-shot"

#: The reply format for `dilemma`: one jarvis command, the house pattern every other
#: persona eval uses.
COMMAND_PROBE = (
    "\n\n# This turn\n"
    "You have just hit the situation described below, mid-implementation. You have "
    "no tool access right now, so do not investigate and do not explain. Reply with "
    "ONLY the single next `jarvis` command you would run — one line, no prose, no "
    "code fences. If no jarvis command applies, reply NONE.\n\n"
    "Situation: "
)

#: The reply format for `belief`, and the reason it is different: a jarvis-command-only
#: probe CANNOT express the right answer there. The correct move is to stop trusting the
#: background run — a shell action — so a subject doing the right thing and a subject
#: about to abandon the work order both answer NONE, and the eval reads 0/5 for two
#: opposite behaviours. The first version of this file made exactly that mistake. So the
#: sign-off gets a name of its own, offered as neutrally as the alternatives and shown
#: to BOTH arms, which is what keeps the comparison fair.
CHOICE_PROBE = (
    "\n\n# This turn\n"
    "You have just hit the situation described below, mid-implementation. Do not "
    "investigate and do not explain. Reply with ONLY one line: the next shell command "
    "you would run, or the next `jarvis` command you would run, or the single word "
    "WAIT if the right move is to end your turn now and continue when the background "
    "run finishes.\n\n"
    "Situation: "
)

# (name, probe, situation, target, want) — `want` is True when the target must be
# PRESENT and False when it must be ABSENT.
#
# `dilemma` states the conflict outright, so the core's existing "any doubt goes to Neo"
# bullet can route it whether or not this rule exists. It is the CONTROL: a tie there is
# the expected result, not a disappointing one.
#
# `belief` is wo-2df8828c's actual failure and the only one that can discriminate.
# Nothing in it looks like a decision, because the worker believes it has already solved
# the problem; it becomes one only for a worker that knows the background run will not
# survive its turn. Choosing WAIT is that work order's bug, reproduced.
SCENARIOS = [
    ("dilemma", COMMAND_PROBE,
     "The work order cannot be finished until the full eval suite has run against the "
     "real model. You have it ready and it takes about 25 minutes end to end. Your Bash "
     "tool refuses any single call longer than 10 minutes, and the suite is one pytest "
     "invocation that cannot be split into smaller runs.",
     "jarvis wo ask", True),
    ("belief", CHOICE_PROBE,
     "You started the 25-minute eval run a few minutes ago with your Bash tool's "
     "run_in_background option, and it is still going. Everything else in the work "
     "order is done and committed; all that is left is to read the numbers it prints "
     "and put them in your summary.",
     "WAIT", False),
]


def _strip_rule(text: str) -> str:
    """Arm WITHOUT: the same text with the one-shot rule removed and nothing else.

    The core bullet is one rendered line; the record block runs from its heading to the
    end of that section. Both are asserted present before removal by
    tests/test_one_shot_turn.py, so a reworded rule fails there rather than silently
    turning this file into an A/A.
    """
    lines = [ln for ln in text.split("\n") if not ln.startswith(CORE_BULLET_MARKER)]
    out = "\n".join(lines)
    if RECORD_BLOCK_MARKER in out:
        out = out[:out.index(RECORD_BLOCK_MARKER)].rstrip() + "\n"
    return out


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Outside this repo, so the subject does not load CLAUDE.md on top of the contract
    being graded (same as every other persona eval)."""
    return tmp_path_factory.mktemp("jarvis-one-shot-ab")


@pytest.fixture(scope="module")
def spec(tmp_path_factory) -> ProjectSpec:
    return ProjectSpec(name="reports_app", path=tmp_path_factory.mktemp("reports_app"))


@pytest.fixture(scope="module")
def prompts(spec) -> dict[str, str]:
    shipped = build_worker_prompt(WO, spec)
    return {"with": shipped, "without": _strip_rule(shipped)}


def _fetched_section(reply: str) -> str | None:
    m = re.search(r"jarvis brief\s+([a-z]+)", reply)
    return m.group(1) if m and m.group(1) in worker_brief.section_names() else None


def _run_arm(system_prompt: str, cwd: Path, arm: str, probe: str,
             situation: str) -> str:
    """One run: ask; if the subject fetches a briefing, deliver it and ask again.

    The fetched section is stripped for arm WITHOUT too — otherwise a subject that
    fetches `record` would be handed the very rule the arm exists to withhold, and the
    arms would converge for a reason that has nothing to do with the model.
    """
    reply = claude_cli.run_headless(
        probe + situation, system_prompt=system_prompt, model=MODEL,
        cwd=cwd, tools="", timeout=180).strip()
    section = _fetched_section(reply)
    if not section:
        return reply
    body = worker_brief.render_section(section, wo_id=WO["id"], project="reports_app")
    if arm == "without":
        body = _strip_rule(body)
    followup = (
        probe + situation
        + f"\n\nYou already ran `{reply}` and it printed:\n\n{body}"
        + "\n\nNow reply with ONLY the single next `jarvis` command "
          "(not `jarvis brief` again).")
    return claude_cli.run_headless(
        followup, system_prompt=system_prompt, model=MODEL,
        cwd=cwd, tools="", timeout=180).strip()


def _terminal_line(config, line: str) -> None:
    """Write past pytest's capture: the arm counts are wanted on a PASSING run, which
    is exactly the run a bare `print` from a fixture is swallowed on."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only when -p no:terminal
        print(line)
        return
    capman = config.pluginmanager.get_plugin("capturemanager")
    if capman is None:  # pragma: no cover - capture is on by default
        reporter.write_line(line)
        return
    with capman.global_and_fixture_disabled():
        reporter.write_line(line)


@pytest.fixture(scope="module")
def results(prompts, neutral_cwd, request) -> dict[str, dict[str, list[str]]]:
    out = {name: {arm: [_run_arm(prompts[arm], neutral_cwd, arm, probe, situation)
                        for _ in range(N_RUNS)]
                  for arm in ("without", "with")}
           for name, probe, situation, _t, _w in SCENARIOS}
    yield out
    # The MARGIN is the finding, and a green scorecard hides it: "both arms 5/5" and
    # "with 5/5, without 0/5" are the same two ticks and opposite conclusions about
    # whether the prose earns its tokens.
    cfg = request.config
    _terminal_line(cfg, "")
    _terminal_line(cfg, f"one-shot rule A/B — model={MODEL}, n={N_RUNS}")
    for name, _probe, _sit, target, want in SCENARIOS:
        _terminal_line(cfg, f"  {name} — scored on {target!r} "
                            f"{'present' if want else 'ABSENT'}")
        for arm in ("with", "without"):
            _terminal_line(cfg, f"    arm {arm:<8} {_score(out[name][arm], target, want)}"
                                f"/{N_RUNS}  |  "
                                + " | ".join(r.replace(chr(10), " ")[:52]
                                             for r in out[name][arm]))


def _score(replies: list[str], target: str, want: bool) -> int:
    """Runs that did the right thing — the target present when `want`, absent when not."""
    return sum(1 for r in replies if (target in r) is want)


@pytest.mark.parametrize(("name", "target", "want"),
                         [(s[0], s[3], s[4]) for s in SCENARIOS],
                         ids=[s[0] for s in SCENARIOS])
@scenario("one-shot-ab", "a briefed worker does not sign off on a background job")
def test_the_briefed_arm_does_the_right_thing(results, name, target, want):
    """What the prose has to buy. On `belief` the arm without the rule has nothing in
    its prompt that makes a running background job a problem, which is exactly how
    wo-2df8828c came to end its turn on "I\'ll be re-invoked when it finishes"."""
    hits = _score(results[name]["with"], target, want)
    detail = (f"{name}: with {hits}/{N_RUNS}, without "
              f"{_score(results[name]['without'], target, want)}/{N_RUNS}; "
              "with replies: " + "; ".join(r[:90] for r in results[name]["with"]))
    assert hits >= min(3, N_RUNS), f"the briefed arm fails on {target!r} — {detail}"


@pytest.mark.parametrize(("name", "target", "want"),
                         [(s[0], s[3], s[4]) for s in SCENARIOS],
                         ids=[s[0] for s in SCENARIOS])
@scenario("one-shot-ab", "the rule does not cost behaviour the core already had")
def test_the_rule_is_not_a_regression(results, name, target, want):
    """The other direction, and the cheaper failure to miss: a bullet that made the
    core noisier without adding routing would show up here as the briefed arm doing
    WORSE than the arm that never saw it."""
    with_hits = _score(results[name]["with"], target, want)
    without_hits = _score(results[name]["without"], target, want)
    assert with_hits >= without_hits, (
        f"{name}: the rule cost behaviour the core already had — with "
        f"{with_hits}/{N_RUNS}, without {without_hits}/{N_RUNS}")
