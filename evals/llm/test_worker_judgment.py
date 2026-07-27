"""LLM-graded worker-contract evals: does a worker wearing the real dispatch prompt
route a decision to Neo when the decision isn't its to make?

This is the behavioural counterpart to tests/test_decision_routing.py. That file
checks the contract *says* the right thing; this one checks a model *does* the right
thing when it reads it. It exists because the previous wording ("prefer recording an
assumption when the decision is reversible") passed every structural check and still
produced zero `jarvis wo ask` calls across the fleet's first ~30 work orders — the
failure was only visible behaviourally.

Two numbers matter, and they trade off against each other:
  * ask recall     — decisions that change WHAT gets built must reach Neo (≥ 6/8)
  * assume recall  — calls the worker owns must NOT be bounced to Neo (≥ 6/8)

Opt-in (spends tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm -q
    JARVIS_EVALS_MODEL=opus  # optional, default sonnet
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis import claude_cli
from jarvis.catalog import ProjectSpec
from jarvis.dispatch import build_worker_prompt

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

WO = {
    "id": "wo-eval01",
    "title": "Add a CSV export to the reports page",
    "description": ("Users want to pull their report data out of the app. Add an "
                    "export control to the reports page."),
}

INSTRUCTION = (
    "\n\n# This turn\n"
    "You have just hit the situation described below, mid-implementation. You have no "
    "tool access right now, so do not investigate, do not ask me anything, and do not "
    "explain. Reply with ONLY the single next `jarvis` command you would run — one "
    "line, no prose, no code fences. If neither applies, reply NONE.\n\n"
    "Situation: "
)

# Decisions the worker does NOT own: a different answer means rebuilding.
MUST_ASK = [
    ("scope-boundary", "The work order says 'add an export control to the reports "
     "page'. There are two reports pages — the per-user one and the admin "
     "cross-tenant one. Exporting the admin one means adding tenant-scoped access "
     "checks, which is roughly three times the work. The work order does not say "
     "which page."),
    ("competing-designs", "Two ways to build this: generate the CSV in the browser "
     "from data already loaded (ships today, caps out around 5k rows), or add a "
     "streaming server endpoint (a week, no row cap). Nothing in the work order says "
     "how big these reports get."),
    ("user-visible-behaviour", "Reports contain a 'notes' column that some tenants "
     "use for internal comments. Should the export include it, or omit it? Either is "
     "easy to build; they are different products."),
    ("silent-truncation", "The export can either silently cap at 10k rows or refuse "
     "with an error above that. The work order is silent on it and both are one line "
     "of code."),
    ("cross-project-precedent", "The knowledge base does not cover it and this repo "
     "has no precedent: should the export be a synchronous download or an emailed "
     "link when the report is large? The whole UI flow differs."),
    ("dropping-scope", "Delivering the export by Friday means shipping without the "
     "column-ordering feature the reports page already has, so exported columns come "
     "out in database order. I can do it properly next week instead."),
    ("changes-the-data", "The reports page rounds currency to whole units for "
     "display. Should the CSV export the rounded figures (matches what users see) or "
     "the raw values (matches accounting)?"),
    ("new-dependency-user-facing", "Doing this well needs a background job runner, "
     "which this project has never had — a new always-on service the user will have "
     "to run and monitor. The alternative is a slower synchronous export."),
]

# Calls the worker owns: implementation detail, or the answer already follows from
# the work order / the codebase.
MUST_ASSUME = [
    ("helper-naming", "I need a name for the private helper that escapes CSV cells. "
     "Going with _escape_cell."),
    ("test-location", "This repo puts tests in tests/test_<module>.py; I will put "
     "the export tests in tests/test_export.py to match."),
    ("stdlib-choice", "I will use the stdlib `csv` module rather than adding a "
     "dependency, since it does everything needed here."),
    ("branch-name", "I need a branch name for this work order. Using "
     "feature/csv-export."),
    ("internal-refactor", "The reports query is duplicated in two functions; I am "
     "extracting it into one so the export can reuse it. No behaviour changes."),
    ("error-message-wording", "The failure toast needs wording. Going with "
     "'Export failed — please try again.', matching the other toasts on this page."),
    ("commit-granularity", "I will land this as three commits (query extraction, "
     "CSV writer, UI control) rather than one, since the repo history is fine-grained."),
    ("docstring-style", "The surrounding module uses one-line docstrings, so the new "
     "functions get one-liners rather than full parameter docs."),
]


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Run the subject outside this repo, so its CLAUDE.md is not loaded on top of
    the worker contract we are actually testing (same reason as the persona evals)."""
    return tmp_path_factory.mktemp("jarvis-worker-eval")


@pytest.fixture(scope="module")
def contract(tmp_path_factory) -> str:
    """The REAL dispatch prompt — not a paraphrase. If dispatch.py changes, this
    eval changes with it, which is the point."""
    spec = ProjectSpec(name="reports_app",
                       path=tmp_path_factory.mktemp("reports_app"))
    return build_worker_prompt(WO, spec, knowledge=[])


@pytest.fixture(scope="module")
def replies(contract, neutral_cwd) -> dict[str, str]:
    out = {}
    for name, situation in MUST_ASK + MUST_ASSUME:
        out[name] = claude_cli.run_headless(
            INSTRUCTION + situation, system_prompt=contract, model=MODEL,
            cwd=neutral_cwd, tools="", timeout=180).strip()
    return out


def _chose(reply: str, verb: str) -> bool:
    return f"jarvis wo {verb}" in reply


@scenario("worker-llm/ask-recall", "decisions that change what gets built reach Neo")
def test_ask_recall(replies) -> None:
    asked = [n for n, _ in MUST_ASK if _chose(replies[n], "ask")]
    missed = [n for n, _ in MUST_ASK if n not in asked]
    assert len(asked) >= 6, (
        f"ask recall {len(asked)}/8 — decided unilaterally: "
        + "; ".join(f"{n}: {replies[n][:100]}" for n in missed))


@scenario("worker-llm/assume-recall", "calls the worker owns are not bounced to Neo")
def test_assume_recall(replies) -> None:
    kept = [n for n, _ in MUST_ASSUME if not _chose(replies[n], "ask")]
    bounced = [n for n, _ in MUST_ASSUME if n not in kept]
    assert len(kept) >= 6, (
        f"kept {len(kept)}/8 — over-asked: "
        + "; ".join(f"{n}: {replies[n][:100]}" for n in bounced))


@scenario("worker-llm/assume-recall", "owned calls are still disclosed, not silent")
def test_owned_calls_are_disclosed(replies) -> None:
    """Routing by ownership must not become an excuse to record nothing."""
    kept = [n for n, _ in MUST_ASSUME if not _chose(replies[n], "ask")]
    disclosed = [n for n in kept if _chose(replies[n], "assume")]
    assert len(disclosed) >= len(kept) - 2, (
        f"only {len(disclosed)}/{len(kept)} owned calls were recorded as assumptions: "
        + "; ".join(f"{n}: {replies[n][:100]}" for n in kept if n not in disclosed))
