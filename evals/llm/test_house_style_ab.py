"""Same-input A/B for the house style: does the injected block actually shorten output? (docs/superpowers/specs/2026-09-19-concision-enforced.md)"""

from __future__ import annotations

import os
import statistics
from pathlib import Path

import pytest

from jarvis import claude_cli, concision
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
    "id": "wo-eval77",
    "title": "A pull request red only because its base was red heals itself",
    "description": ("Teach the pull-request poll to tell a failure caused by this "
                    "branch apart from one inherited from a broken base, and re-run "
                    "the checks itself rather than spending a worker turn on it."),
}

#: The work the subject is asked to report
WORK_DONE = (
    "You have finished the work order. What you did: `Daemon.heal_inherited_failure` "
    "recognises a check that failed while the base branch was red and is green now, "
    "and re-runs those jobs instead of nudging the worker. It re-reads the pull "
    "request after the busy guard so the head it compares against is current, and "
    "`ops.carry_validated_head` now refuses to carry a panel verdict unless the new "
    "commit has exactly two parents with the judged commit first — because `gh pr "
    "update-branch` has no `--match-head-commit`, so no amount of re-reading closes "
    "the race. One attempt per (pull request, base sha) bounds it; a second red falls "
    "through to the existing worker nudge. While the base is still red no attempt is "
    "spent and the status line says what is being waited for. Five new tests in "
    "tests/test_inherited_failures.py, all passing, plus the full suite at 1,247 "
    "passed. You retracted kn-a1018a09, which stated a weaker version of the parent "
    "rule, and replaced it with kn-710a99d8. Pull request 393 is open."
)

SUMMARY_PROBE = (
    "\n\n# This turn\n"
    + WORK_DONE
    + "\n\nYou have no tool access right now. Reply with ONLY the single "
      "`jarvis wo finish` command you would run — one line, no prose, no code fences."
)

FINAL_MESSAGE_PROBE = (
    "\n\n# This turn\n"
    + WORK_DONE
    + "\n\nYou have already run `jarvis wo finish`. Reply with ONLY the final message "
      "of your turn — the one captured verbatim onto the work order record. No "
      "preamble about what you are about to write."
)

# (name, probe, extractor) — the extractor turns a reply into the text being measured
SCENARIOS = [
    ("summary", SUMMARY_PROBE, lambda r: concision.finish_summary(r) or ""),
    ("final_message", FINAL_MESSAGE_PROBE, lambda r: r),
]

#: What arm WITH is expected to come in under
SUMMARY_TARGET_WORDS = concision.DEFAULT_SUMMARY_MAX_WORDS
FINAL_MESSAGE_FLOOR_WORDS = 60


def _strip_house_style(text: str) -> str:
    """Arm WITHOUT: the same prompt with the injected block cut and nothing else"""
    start = text.find(concision.HOUSE_STYLE_BEGIN)
    end = text.find(concision.HOUSE_STYLE_END)
    if start == -1 or end == -1:
        return text
    return text[:start] + text[end + len(concision.HOUSE_STYLE_END):]


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Outside this repo, so the subject does not load CLAUDE.md on top of the contract being graded (same..."""
    return tmp_path_factory.mktemp("jarvis-house-style-ab")


@pytest.fixture(scope="module")
def spec(tmp_path_factory) -> ProjectSpec:
    return ProjectSpec(name="reports_app", path=tmp_path_factory.mktemp("reports_app"))


@pytest.fixture(scope="module")
def prompts(spec) -> dict[str, str]:
    """Both arms, composed once"""
    shipped = build_worker_prompt(WO, spec)
    with_style = shipped + "\n\n" + concision.house_style()
    return {"with": with_style, "without": _strip_house_style(with_style)}


def _run(system_prompt: str, cwd: Path, probe: str) -> str:
    return claude_cli.run_headless(
        probe, system_prompt=system_prompt, model=MODEL,
        cwd=cwd, tools="", timeout=180).strip()


def _terminal_line(config, line: str) -> None:
    """Write past pytest's capture: the arm counts are wanted on a PASSING run"""
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
def results(prompts, neutral_cwd, request) -> dict[str, dict[str, list[int]]]:
    out: dict[str, dict[str, list[int]]] = {}
    for name, probe, extract in SCENARIOS:
        out[name] = {
            arm: [concision.word_count(extract(_run(prompts[arm], neutral_cwd, probe)))
                  for _ in range(N_RUNS)]
            for arm in ("without", "with")
        }
    yield out
    # The MARGIN is the finding and a green scorecard hides it: "both arms at 90 words" and "with 90
    cfg = request.config
    _terminal_line(cfg, "")
    _terminal_line(cfg, f"house style A/B — model={MODEL}, n={N_RUNS}, words written")
    for name, _probe, _extract in SCENARIOS:
        _terminal_line(cfg, f"  {name}")
        for arm in ("with", "without"):
            counts = out[name][arm]
            _terminal_line(
                cfg, f"    arm {arm:<8} median {statistics.median(counts):6.0f}  "
                     f"| runs: {counts}")


@scenario("house-style-ab", "the injected style shortens the finish summary")
def test_the_styled_arm_writes_a_summary_under_the_cap(results):
    """The whole point"""
    counts = results["summary"]["with"]
    median = statistics.median(counts)

    assert median < SUMMARY_TARGET_WORDS, (
        f"styled arm median {median:.0f} words is not under the {SUMMARY_TARGET_WORDS} "
        f"cap — every finish would be denied once. runs: {counts}")


@scenario("house-style-ab", "the style is what did it, not the contract")
def test_the_style_beats_the_arm_without_it(results):
    """The margin, which is the only thing separating this from an A/A"""
    with_median = statistics.median(results["summary"]["with"])
    without_median = statistics.median(results["summary"]["without"])

    assert with_median < without_median, (
        f"the injected style changed nothing: with {with_median:.0f} words, without "
        f"{without_median:.0f}. Either the block is redundant against the contract "
        f"sentence — reclaim its per-turn cost — or it is not reaching the subject.")


@scenario("house-style-ab", "brevity was not bought by gutting the report")
def test_the_final_message_is_still_a_report(results):
    """The failure this exists to catch"""
    counts = results["final_message"]["with"]
    median = statistics.median(counts)

    assert median >= FINAL_MESSAGE_FLOOR_WORDS, (
        f"the final message collapsed to {median:.0f} words. The detail moved out of "
        f"the summary has to land here — if it lands nowhere, the record is worse than "
        f"it was verbose. runs: {counts}")
