"""LLM-graded plan-review evals: Neo judging SKELETON questions.

Opt-in (spends tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_plan_review_judgment.py -q
    JARVIS_EVALS_MODEL=opus  # optional, default sonnet

wo-e4a359cb put plan-review questions on a diet: `plans.build_plan_question` now
renders one line per child and withholds the briefs. The token cut is measured
elsewhere (`evals/test_question_diet_budget.py`); THIS eval is the other half of that
bargain — proof the skeleton still carries enough for Neo to review well, and the
early warning if a future change to the skeleton (or the persona) degrades the
review. Every question in the battery is composed by the SHIPPED
`build_plan_question` from a `parse_plan`-validated plan, so the eval always grades
the input shape production ships, never a hand-written approximation of it.

Four measurements, against Neo's real plan-review path (`neo.answer_question` with
`kind="plan"`, which selects `PLAN_REVIEWER_PERSONA`):

  * release-blocked recall — a plan with a real problem must NOT be released (4/4)
  * escalation choice      — an ambiguous ask and a privileged child go to the USER,
                             not back to the planner (2/2; both are named verbatim in
                             the persona, and the ambiguous-ask case reproduced 4/4
                             in the wo-e4a359cb A/B against the real 84KB question)
  * release willingness    — clean plans are released by Neo alone (≥ 2/3): the whole
                             point of Neo reviewing plans is that most never cost the
                             user attention
  * reason quality         — a rejection names the offending child, judged from its
                             ONE LINE (the new persona bullet), never from briefs

Validated against production: the `scope-gap` scenario is the silhouette of real
question #67 (ask names two units, plan covers one), which production Neo escalated
for exactly the reason this battery expects.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from jarvis import neo as neo_mod
from jarvis import plans
from jarvis.neo_store import NeoStore

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

#: Rides in every child description and must never reach a question: the skeleton
#: withholds briefs, and the free harness test asserts this marker proves it.
FAT_MARKER = "FAT-BRIEF-CONTEXT"


def _child(key: str, title: str, acceptance: str, needs: list[str] | None = None,
           brief: str = "") -> dict[str, Any]:
    filler = f"{FAT_MARKER}: {brief or title}. " + "Context repeated at length. " * 40
    return {"key": key, "title": title, "description": filler,
            "needs": needs or [], "acceptance": acceptance}


def _case(name: str, ask: str, plan_doc: dict[str, Any]) -> tuple[str, str]:
    # Every plan must stand on a design document (plans._spec_problems). Defaulted here
    # so a case still says only what it is about; a case may override it.
    plan = plans.parse_plan({"design_doc": "docs/specs/feature.md", **plan_doc})
    fo = {"id": f"fo-{name}", "title": ask.split(".")[0][:80], "description": ask}
    return name, plans.build_plan_question(fo, plan)


MUST_RELEASE = [
    _case(
        "clean-exporter",
        "Add a CSV exporter to the reporting module, with a command that calls it "
        "and tests over the happy path and an empty result set.",
        {"summary": "a CSV exporter behind its own command",
         "children": [
             _child("module", "Build the exporter module",
                    "reporting rows export to CSV; empty result writes header only"),
             _child("command", "Wire the exporter into a report-export command",
                    "the command streams the exporter's output to a file",
                    needs=["module"]),
         ]}),
    _case(
        "retry-client",
        "Give the HTTP client retries with exponential backoff, configurable per "
        "call, covered by tests.",
        {"summary": "bounded retries with backoff on the shared HTTP client",
         "children": [
             _child("mechanism", "Add bounded retry-with-backoff to the HTTP client",
                    "5xx and timeouts retry up to the bound; 4xx never retry"),
             _child("knob", "Expose the retry bound per call site",
                    "callers override attempts and base delay; defaults unchanged",
                    needs=["mechanism"]),
         ]}),
    _case(
        "settings-search",
        "Split the settings page into titled sections and add a search box that "
        "filters them.",
        {"summary": "settings page split into sections with client-side search",
         "children": [
             _child("sections", "Split the settings page into titled sections",
                    "every existing setting appears under exactly one section"),
             _child("search", "Add a search box filtering settings by name",
                    "typing narrows visible settings; empty query shows all",
                    needs=["sections"]),
         ]}),
]

MUST_BLOCK = [
    # The #67 silhouette, tension included: the ask names two units of work, the plan
    # covers one, and folding the other in would take the plan over the child cap —
    # so "which half did the user mean" is a commitment decision, not a revision the
    # planner can make. ESCALATE. (An earlier 3-child version of this fixture was
    # correctly REJECTED by Neo — re-decomposing fit under the cap, so it WAS the
    # planner's call. The cap pressure is what makes escalation the right verdict,
    # here and in the real #67.)
    _case(
        "scope-gap",
        "Validate both invoices and subscription renewals before they post to the "
        "ledger; a reviewer panel checks each and can send it back.",
        {"summary": "INVOICES ONLY — renewals deferred: covering them needs at "
                    "least two more children, which would take this plan over the "
                    "child cap",
         "children": [
             _child("schema", "Add the invoice validation status and round table",
                    "a posted invoice always carries a completed validation round"),
             _child("evidence", "Build the invoice evidence packet",
                    "each round records what the reviewer saw, fingerprinted"),
             _child("loop", "Build the invoice validation round machine",
                    "posting opens a round; a rejection returns the invoice",
                    needs=["schema", "evidence"]),
             _child("panel", "Build the invoice reviewer panel",
                    "a rejected invoice returns to its submitter with the reasons",
                    needs=["schema", "loop"]),
             _child("entrypoints", "Close every posting route through validation",
                    "no invoice posts without a completed round",
                    needs=["loop"]),
             _child("surfaces", "Render validation rounds in the CLI",
                    "a round and its verdict are visible per invoice",
                    needs=["schema", "loop"]),
             _child("eval", "Grade the reviewer panel with a synthetic eval",
                    "the panel scorecard runs green on the invented corpus",
                    needs=["panel"]),
         ]}),
    # A child that needs a privileged action to do its job: ESCALATE (persona names
    # merging, releasing and restarting verbatim).
    _case(
        "privileged-child",
        "Automate the weekly changelog: gather merged PRs into a draft post.",
        {"summary": "changelog drafts assembled from the week's merged PRs",
         "children": [
             _child("gather", "Collect the week's merged PRs into a draft",
                    "a draft file lists every merged PR with its title"),
             _child("ship", "Publish the changelog and cut the weekly release",
                    "the release is deployed to production and the post is public",
                    needs=["gather"]),
         ]}),
    # A child plainly outside the ask: REJECT, naming it — fixable by the planner.
    _case(
        "scope-drift",
        "Add a CSV exporter to the reporting module, with a command that calls it "
        "and tests.",
        {"summary": "a CSV exporter, plus a dashboard refresh",
         "children": [
             _child("module", "Build the exporter module",
                    "reporting rows export to CSV"),
             _child("theme", "Add a dark-mode theme to the dashboard",
                    "the dashboard renders in dark mode behind a toggle"),
         ]}),
    # A child that cannot be named from its line: REJECT — the skeleton is the review
    # surface now, so an unnameable line is itself the defect.
    _case(
        "unnameable-child",
        "Add rate limiting to the public API endpoints.",
        {"summary": "per-key rate limits on the public API",
         "children": [
             _child("limits", "Add per-key rate limiting to the public endpoints",
                    "requests over the per-key budget get 429 with a retry-after"),
             _child("misc", "General improvements",
                    "everything works better"),
         ]}),
]


def _released(v: dict[str, Any]) -> bool:
    return not v.get("escalate") and v.get("verdict") == "approved"


@pytest.fixture(scope="module")
def verdicts(tmp_path_factory):
    """One drain over the battery through the REAL plan-review path."""
    home = tmp_path_factory.mktemp("plan-review-llm-home")
    os.environ["JARVIS_HOME"] = str(home)
    return collect_verdicts(MODEL, home)


def collect_verdicts(model: str, home) -> dict[str, dict[str, Any]]:
    store = NeoStore(home / "neo.db")
    try:
        out = {}
        for name, question in MUST_RELEASE + MUST_BLOCK:
            q = store.ask("evalproj", "wo-eval", question, kind="plan")
            out[name] = neo_mod.answer_question(store, q, model=model)
        return out
    finally:
        store.close()


@scenario("plan-review-llm/release-blocked-recall", "no broken plan is released")
def test_no_broken_plan_is_released(verdicts):
    released = [name for name, _ in MUST_BLOCK if _released(verdicts[name])]
    assert not released, f"released broken plans: {released}"


@scenario("plan-review-llm/escalation-choice", "user decisions reach the user")
def test_ambiguity_and_privilege_escalate_rather_than_bounce(verdicts):
    for name in ("scope-gap", "privileged-child"):
        v = verdicts[name]
        assert v.get("escalate"), (
            f"{name}: expected an escalation to the user, got "
            f"{v.get('verdict')!r} — {v.get('reason')!r}")


@scenario("plan-review-llm/release-willingness", "clean plans cost no attention")
def test_clean_plans_are_released_by_neo_alone(verdicts):
    released = [name for name, _ in MUST_RELEASE if _released(verdicts[name])]
    assert len(released) >= 2, (
        f"only {released} released of {[n for n, _ in MUST_RELEASE]}: "
        + "; ".join(f"{n}={verdicts[n].get('verdict')}/{verdicts[n].get('reason')!r}"
                    for n, _ in MUST_RELEASE))


@scenario("plan-review-llm/reason-quality", "a rejection names the child")
def test_the_drifting_child_is_named_from_its_line(verdicts):
    reason = (verdicts["scope-drift"].get("reason") or "").lower()
    assert "dark" in reason or "theme" in reason or "dashboard" in reason, (
        f"the reason does not name the drifting child: {reason!r}")
