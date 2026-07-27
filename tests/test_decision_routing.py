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


def test_contract_routes_decisions_by_ownership_not_reversibility(contract: str) -> None:
    """The regression this whole change exists to prevent.

    'Prefer an assumption when the decision is reversible' asks about risk of being
    wrong, which a capable worker can always argue its way past. The test has to be
    about who owns the decision.
    """
    assert "OWNERSHIP" in contract
    assert "would a different answer change WHAT gets built" in contract
    assert "Prefer recording an assumption" not in contract, (
        "the reversibility hedge is back — it makes `jarvis wo ask` unreachable"
    )


def test_contract_names_asking_as_normal_not_escalation(contract: str) -> None:
    """A worker that reads asking as 'bothering the user' will never do it."""
    assert "it is not an escalation" in contract
    assert "does not cost the user attention" in contract


def test_contract_says_ask_before_building(contract: str) -> None:
    assert "Ask BEFORE you build, not after" in contract


def test_contract_still_demands_every_assumption_be_recorded(contract: str) -> None:
    """Routing by ownership must not shrink the audit trail as a side effect.

    Measured: an earlier draft defined an assumption as "a call you were entitled to
    make", and workers concluded that following a repo convention was not a decision
    at all — 5 of 8 owned calls went unrecorded (evals/llm/test_worker_judgment.py
    :: owned calls are still disclosed). Whether routine calls deserve a review-queue
    slot is a separate question; it must not be answered by accident here.
    """
    assert "Record EVERY such call, including the small and obvious ones" in contract
    assert "only audit trail" in contract


def test_contract_preempts_the_reversibility_rationalisation(contract: str) -> None:
    assert "Do not talk yourself into \"it's reversible\"" in contract


def test_operation_template_mirrors_the_contract() -> None:
    """dispatch.py briefs the worker; OPERATION.md is what a human (or an agent
    started outside the OS) reads. They must not drift apart."""
    from jarvis.bootstrap import ASSETS

    tmpl = (ASSETS / "OPERATION.md.tmpl").read_text()
    assert "Route decisions by ownership, not by risk" in tmpl
    assert "jarvis wo ask" in tmpl
    assert "jarvis wo assume" in tmpl
    assert "not an escalation" in tmpl


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
