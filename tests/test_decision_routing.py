"""How decisions leave a worker, and where the user's verdict on them lands.

Two paths exist — `jarvis wo ask` (Neo decides) and `jarvis wo assume` (the worker
decides and discloses). Across the fleet's first ~30 work orders the ask path was
used exactly zero times, because the contract told workers to prefer an assumption
"when the decision is reversible" and a capable worker always finds a reversible
reading. These guards lock in the replacement — an ownership test — and the second
learning source that stops Neo from being cold-started only by its own traffic.

The behavioural (LLM-graded) counterpart is evals/llm/test_worker_judgment.py.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.dispatch import build_worker_prompt
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


# -- the worker contract -------------------------------------------------------

@pytest.fixture()
def contract(catalog_file) -> str:
    catalog = load_catalog(catalog_file)
    spec = catalog.projects[0]
    return build_worker_prompt(
        {"id": "wo-test", "title": "t", "description": "d"}, spec, knowledge=[])


def test_contract_offers_both_decision_paths(contract: str) -> None:
    assert "jarvis wo ask wo-test" in contract
    assert "jarvis wo assume wo-test" in contract


def test_contract_makes_neo_the_first_responder(contract: str) -> None:
    """The regression this whole change exists to prevent.

    'Prefer an assumption when the decision is reversible' asks about risk of being
    wrong, which a capable worker can always argue its way past. Asking has to be the
    default response to a doubt, not a branch the worker can reason itself out of.
    """
    assert "Neo is your first responder. Any doubt goes to it." in contract
    assert "Prefer recording an assumption" not in contract, (
        "the reversibility hedge is back — it makes `jarvis wo ask` unreachable"
    )


def test_the_trigger_is_doubt_not_importance(contract: str) -> None:
    """Scoping the trigger to big/scope-changing calls re-derives the weaker
    contract: the worker still guesses on everything it judges small."""
    assert "The trigger is DOUBT, not importance" in contract
    assert "either would work" in contract


def test_contract_names_asking_as_normal_not_escalation(contract: str) -> None:
    """A worker that reads asking as 'bothering the user' will never do it."""
    assert "it is not an escalation" in contract
    assert "does not interrupt the user" in contract


def test_contract_says_ask_before_building(contract: str) -> None:
    assert "Ask BEFORE you build on it, not after" in contract


def test_contract_makes_assumptions_the_rare_case(contract: str) -> None:
    assert "should be RARE" in contract
    assert "a call you made with NO doubt" in contract


def test_contract_still_demands_every_assumption_be_recorded(contract: str) -> None:
    """Demoting assumptions must not shrink the audit trail as a side effect.

    Measured: an earlier draft defined an assumption as "a call you were entitled to
    make", and workers concluded that following a repo convention was not a decision
    at all — 5 of 8 went unrecorded (evals/llm/test_worker_judgment.py :: no-doubt
    calls are still disclosed). Whether routine calls deserve a review-queue slot is
    a separate question; it must not be answered by accident here.
    """
    assert "Record EVERY such call, including the small and obvious ones" in contract
    assert "only audit trail" in contract


def test_contract_preempts_the_rationalisations_for_guessing(contract: str) -> None:
    """Named verbatim, because these are the exact sentences a capable worker
    reaches for when it would rather not stop and ask."""
    for excuse in ("It's reversible", "it's only an "
                   "implementation detail", "I'll note it as an assumption"):
        assert excuse in contract, f"unaddressed rationalisation: {excuse!r}"


def test_operation_template_mirrors_the_contract() -> None:
    """dispatch.py briefs the worker; OPERATION.md is what a human (or an agent
    started outside the OS) reads. They must not drift apart."""
    from jarvis.bootstrap import ASSETS

    # the template is hard-wrapped prose, so compare on collapsed whitespace
    tmpl = " ".join((ASSETS / "OPERATION.md.tmpl").read_text().split())
    assert "Route any doubt to Neo — it is your first responder" in tmpl
    assert "jarvis wo ask" in tmpl
    assert "jarvis wo assume" in tmpl
    assert "it is not an escalation" in tmpl
    assert "The trigger is **doubt**, not importance" in tmpl
    assert "this should be rare" in tmpl.lower()
    assert "no doubt" in tmpl


# -- the user's verdict becomes a Neo learning ---------------------------------

@pytest.fixture()
def reviewable(started, project):
    """A finished work order sitting in needs_review with one pending assumption."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.assume(wo["id"], "assumed the export defaults to CSV")
    assert ops.finish(wo["id"], "shipped")["status"] == "needs_review"
    return daemon, wo


def _learnings():
    neo = NeoStore()
    try:
        return neo.all_learnings()
    finally:
        neo.close()


def test_review_without_feedback_teaches_nothing(reviewable) -> None:
    """Unchanged behaviour: a bare accept still just settles the work order."""
    _, wo = reviewable
    out = ops.review_work_order(wo["id"], accept=True)
    assert out["reviewed"] == 1
    assert "learning_id" not in out
    assert _learnings() == []


def test_accepting_with_feedback_teaches_neo(reviewable, project) -> None:
    _, wo = reviewable
    out = ops.review_work_order(
        wo["id"], accept=True, feedback="CSV is always the right default for exports")
    assert out["learning_id"]

    (learning,) = _learnings()
    assert learning["source"] == "review"
    assert learning["project"] == "proj_a"
    assert "assumed the export defaults to CSV" in learning["content"]
    assert "CSV is always the right default" in learning["content"]
    assert "accepted" in learning["content"]

    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["status"] == "completed"
    finally:
        store.close()


def test_a_taught_learning_reaches_neos_prompt(reviewable) -> None:
    """The point of recording it: it must show up in what Neo is told next time."""
    from jarvis import neo as neo_mod

    _, wo = reviewable
    ops.review_work_order(wo["id"], accept=True,
                          feedback="CSV is always the right default for exports")
    neo = NeoStore()
    try:
        assert "CSV is always the right default" in neo_mod.build_system_prompt(
            neo, "proj_a")
    finally:
        neo.close()


def test_rejecting_with_feedback_reaches_the_worker(reviewable, project) -> None:
    """A rejection whose reasoning never reaches the worker just strands it."""
    _, wo = reviewable
    out = ops.review_work_order(wo["id"], accept=False,
                                feedback="No — JSON, the consumer is an API")
    assert out["delivered"]["msg_id"]
    assert out["learning_id"]

    store = ProjectStore(project)
    try:
        queued = [m for m in store.list_messages(wo["id"])
                  if m["direction"] == "user_to_agent"]
        assert any("the consumer is an API" in m["content"] for m in queued)
        # guidance is on its way, so the work order is not also begging for attention
        assert not store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()

    (learning,) = _learnings()
    assert "rejected" in learning["content"]


def test_bare_rejection_still_flags_for_attention(reviewable, project) -> None:
    _, wo = reviewable
    ops.review_work_order(wo["id"], accept=False)
    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()
