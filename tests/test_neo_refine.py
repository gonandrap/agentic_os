"""Neo's third outcome: return a compound or oversized question to its asker (wo-ea3c6f0c)"""

from __future__ import annotations

import pytest

from jarvis import autoreview
from jarvis import neo as neo_mod
from jarvis.neo_store import NeoStore

COMPOUND = ("6.2 item 5 gets no guard here: section 7 step 2 closes it. Also: "
            "_supervisor_config already falls back on CatalogError.")
ATOMIC = "_supervisor_config already falls back to SupervisorConfig on CatalogError."


def test_the_precheck_catches_a_bundle_and_an_oversized_text_only():
    assert "bundles independent claims" in neo_mod.refine_note(COMPOUND, 1000)
    assert "over 50" in neo_mod.refine_note(ATOMIC, 50)
    assert neo_mod.refine_note(ATOMIC, 1000) == ""
    # `also` mid-sentence is one claim, not two
    assert neo_mod.refine_note("It also falls back on CatalogError.", 1000) == ""
    assert neo_mod.refine_note("Q1: which file?\nQ2: which name?", 1000) != ""


def _never(*_a, **_k):
    raise AssertionError("a pre-checked question must not reach the model")


def _model_refines(*_a, **_k):
    return neo_mod.parse_verdict(
        '{"escalate": false, "refine": true, "answer": "", "reason": "split it"}')


@pytest.fixture
def neo(jarvis_home):
    store = NeoStore()
    yield store
    store.close()


def test_a_compound_worker_question_is_refined_without_a_model_call(neo):
    q = neo.ask("proj_a", "wo-1", COMPOUND)
    delivered = []
    results = neo_mod.drain_queue(neo, "m", answer=_never,
                                  deliver=lambda q_, v: delivered.append(v))
    assert results[0]["outcome"] == "refined"
    assert neo.get(q["id"])["status"] == "refined"
    assert delivered[0]["refine"] and not delivered[0]["escalate"]


def test_a_model_refine_is_its_own_outcome_not_an_escalation(neo):
    q = neo.ask("proj_a", "wo-1", ATOMIC)
    neo_mod.drain_queue(neo, "m", answer=_model_refines)
    row = neo.get(q["id"])
    assert (row["status"], row["answer_reason"]) == ("refined", "split it")
    assert neo.escalation_cause_counts() == []


def test_the_refine_cap_turns_the_next_bounce_into_an_escalation(neo):
    for _ in range(neo_mod.MAX_REFINES):
        neo.ask("proj_a", "wo-1", COMPOUND)
    neo_mod.drain_queue(neo, "m", answer=_never)
    assert neo.refines("wo-1", "question") == neo_mod.MAX_REFINES
    # Past the cap the pre-check stops bouncing: the question reaches the model
    capped = neo.ask("proj_a", "wo-1", COMPOUND)
    neo_mod.drain_queue(neo, "m", answer=_model_refines)
    row = neo.get(capped["id"])
    assert (row["status"], row["escalation_cause"]) == ("escalated", "scope-too-large")


def test_an_os_built_kind_is_never_refined(neo):
    q = neo.ask("proj_a", "wo-1", COMPOUND, kind="approval")
    neo_mod.drain_queue(neo, "m", answer=lambda *a, **k: neo_mod.parse_verdict(
        '{"escalate": false, "verdict": "approve", "answer": "", "reason": "ok"}'))
    assert neo.get(q["id"])["status"] == "answered"


class _Store:
    """The four writes `propose` makes on the refine path"""

    def __init__(self):
        self.reviewed, self.messages, self.events = [], [], []

    def link_assumption_question(self, *_a):
        pass

    def review_assumption(self, aid, status, **kw):
        self.reviewed.append((aid, status, kw["decided_by"]))

    def queue_message(self, wo_id, text, source=""):
        self.messages.append(text)

    def add_event(self, wo_id, kind, payload):
        self.events.append(kind)


def test_a_compound_assumption_goes_back_to_its_worker_not_to_the_user(neo):
    store = _Store()
    wo = {"id": "wo-1", "title": "t", "description": "d"}
    a = {"id": 7, "n": 2, "status": "pending", "content": COMPOUND}
    q = autoreview.propose(store, neo, "proj_a", wo, a, [a], early=True)
    assert neo.get(q["id"])["status"] == "refined"
    assert store.reviewed == [(7, "refined", autoreview.DECIDER)]
    assert store.messages[0].startswith(neo_mod.REFINE_PREFIX)
    assert store.events == ["autoreview_refined"]
