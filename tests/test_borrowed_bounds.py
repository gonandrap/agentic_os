"""Every builder that puts borrowed text in front of Neo BOUNDS it.

User decision #11, Neo ruling on question 1945 (option a): a test pins the bounds
rather than prose. The seven `neo.ask` builders and the call site each one renders into:

* src/jarvis/gates.py:1024 `queue_for_review` (neo.ask at :1047)
* src/jarvis/gates.py:830 `build_request_question` (rendered into that same ask)
* src/jarvis/automerge.py:587 `_request_question`
* src/jarvis/remedies.py:1494 `_request_question`
* src/jarvis/remedies.py:1542 `propose` (neo.ask at :1595)
* src/jarvis/remedies.py:1619 `propose_fix` (neo.ask at :1666)
* src/jarvis/issues.py:1144 `ask_triage` (neo.ask at :1159)
* src/jarvis/supervisor.py:1091 `_question_for` and :1074 `escalation_context`
  (neo.ask at src/jarvis/supervisor.py:1224)

Each case feeds 50000 characters into ONE borrowed field and asserts the rendered
payload carries no more of it than that field's named constant in
src/jarvis/provenance.py, and that the run arrives inside a borrowed block.
"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis import gates, provenance
from jarvis.provenance import (CONTEXT_LIMIT, DESCRIPTION_LIMIT, EVIDENCE_LIMIT,
                               TITLE_LIMIT)

HUGE = "X" * 50000

ACTION = gates.GatedAction(kind="release", summary="ship a release",
                           command="./scripts/release.sh", matched="release.sh")


def _longest_run(text: str) -> int:
    best = run = 0
    for char in text:
        run = run + 1 if char == "X" else 0
        best = max(best, run)
    return best


class _Neo:
    """Captures what reached `neo.ask` — the question and the context, nothing else."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def ask(self, project: str, wo_id: str, question: str, context: str = "",
            kind: str = "", **_: Any) -> dict[str, Any]:
        self.asked.append((question, context))
        return {"id": 1}

    def close(self) -> None:
        pass

    def payload(self) -> str:
        return "\n".join(q + "\n" + c for q, c in self.asked)


class _Store:
    """The reads `gates.queue_for_review` makes, and the writes it performs."""

    def __init__(self, approval: dict[str, Any]) -> None:
        self.approval = approval

    def list_approvals(self, wo_id, limit=0):
        return []

    def user_messages(self, wo_id, limit=0):
        return []

    def start_review(self, approval_id, question_id):
        pass

    def get_approval(self, approval_id):
        return self.approval

    def get_work_order(self, wo_id):
        return {"id": wo_id, "status": "running"}

    def set_status(self, wo_id, status):
        pass


class _PStore:
    """The `remedies` writes, stubbed: this test grades prompt text, not rows."""

    def add_approval(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {"id": 7}

    def link_neo_question(self, *a: Any, **k: Any) -> None:
        pass

    def update_alarm(self, *a: Any, **k: Any) -> None:
        pass

    def add_event(self, *a: Any, **k: Any) -> None:
        pass

    def pending_approvals(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return []


def _wo(**over: Any) -> dict[str, Any]:
    return {"id": "wo-1", "title": "t", "description": "d", **over}


# -- one case per borrowed field -------------------------------------------------------


def _gates_request(field: str) -> str:
    wo = _wo(**({field: HUGE} if field in ("title", "description") else {}))
    return gates.build_request_question(
        ACTION, wo,
        justification=HUGE if field == "justification" else "j",
        evidence=HUGE if field == "evidence" else "e")


def _gates_queue(field: str) -> str:
    wo = _wo(**({field: HUGE} if field in ("title", "description") else {}))
    approval = {"id": 3, "contested": 0, "agent_type": "",
                "justification": HUGE if field == "justification" else "j",
                "evidence": HUGE if field == "evidence" else "e"}
    neo = _Neo()
    gates.queue_for_review(_Store(approval), neo, "proj_a", wo, ACTION, approval)
    return neo.payload()


def _automerge_request(field: str) -> str:
    from jarvis import automerge

    decision = automerge.Decision(armed=True, code="armed", reason="r", round_n=1,
                                  judged_sha="abc1234")
    return automerge._request_question("proj_a", _wo(**{field: HUGE}), decision,
                                       "https://example.invalid/pr/1")


def _remedies_request(field: str) -> str:
    from jarvis import remedies

    return remedies._request_question(
        "proj_a", "wo-1", remedies.REMEDIES["nudge"],
        HUGE if field == "argument" else "a",
        HUGE if field == "evidence" else "e",
        HUGE if field == "reason" else "r")


def _remedies_propose(field: str, monkeypatch) -> str:
    from jarvis import remedies

    monkeypatch.setattr(remedies, "_refusal", lambda *a, **k: None)
    monkeypatch.setattr(remedies, "_carrier_id", lambda *a, **k: "wo-1")
    neo = _Neo()
    remedies.propose(
        _PStore(), neo, "proj_a",
        {"id": "wo-1", "title": HUGE if field == "title" else "t"},
        {"id": 5, "wo_id": "wo-1", "reason": HUGE if field == "alarm_reason" else "ar"},
        "nudge", HUGE if field == "argument" else "a", object(),
        evidence=HUGE if field == "evidence" else "e",
        reason=HUGE if field == "reason" else "r")
    return neo.payload()


def _remedies_propose_fix(field: str, monkeypatch) -> str:
    from jarvis import remedies

    monkeypatch.setattr(remedies, "_config_refusal", lambda *a, **k: None)
    monkeypatch.setattr(remedies, "_carrier_for_subject", lambda *a, **k: "wo-1")
    neo = _Neo()
    remedies.propose_fix(
        _PStore(), neo, "proj_a",
        {"id": "wo-1", "title": HUGE if field == "title" else "t"},
        "nudge", HUGE if field == "argument" else "a", object(),
        reason=HUGE if field == "reason" else "r")
    return neo.payload()


def _issues_triage(field: str, monkeypatch) -> str:
    from jarvis import issues, neo_store

    neo = _Neo()
    monkeypatch.setattr(neo_store, "NeoStore", lambda *a, **k: neo)
    issues.ask_triage("proj_a", "https://example.invalid/i/1",
                      HUGE if field == "title" else "t",
                      HUGE if field == "body" else "b", "critical")
    return neo.payload()


def _supervisor_question(field: str) -> str:
    from jarvis import supervisor

    verdict = {"question": HUGE if field == "question" else "",
               "reason": HUGE if field == "verdict_reason" else "vr"}
    alarm = {"reason": HUGE if field == "alarm_reason" else "ar"}
    return (supervisor._question_for(verdict, alarm) + "\n"
            + supervisor.escalation_context(HUGE if field == "evidence" else "ev",
                                            verdict))


#: (builder, borrowed field, the constant that bounds it).
CASES = [
    ("gates.build_request_question", "title", TITLE_LIMIT),
    ("gates.build_request_question", "description", DESCRIPTION_LIMIT),
    ("gates.build_request_question", "justification", DESCRIPTION_LIMIT),
    ("gates.build_request_question", "evidence", EVIDENCE_LIMIT),
    ("gates.queue_for_review", "title", TITLE_LIMIT),
    ("gates.queue_for_review", "description", DESCRIPTION_LIMIT),
    ("gates.queue_for_review", "justification", DESCRIPTION_LIMIT),
    ("gates.queue_for_review", "evidence", EVIDENCE_LIMIT),
    ("automerge._request_question", "title", TITLE_LIMIT),
    ("automerge._request_question", "description", DESCRIPTION_LIMIT),
    ("remedies._request_question", "argument", DESCRIPTION_LIMIT),
    ("remedies._request_question", "reason", DESCRIPTION_LIMIT),
    ("remedies._request_question", "evidence", EVIDENCE_LIMIT),
    ("remedies.propose", "title", TITLE_LIMIT),
    ("remedies.propose", "alarm_reason", CONTEXT_LIMIT),
    ("remedies.propose", "argument", DESCRIPTION_LIMIT),
    ("remedies.propose", "reason", DESCRIPTION_LIMIT),
    ("remedies.propose", "evidence", EVIDENCE_LIMIT),
    ("remedies.propose_fix", "title", TITLE_LIMIT),
    ("remedies.propose_fix", "argument", DESCRIPTION_LIMIT),
    ("remedies.propose_fix", "reason", DESCRIPTION_LIMIT),
    ("issues.ask_triage", "title", TITLE_LIMIT),
    ("issues.ask_triage", "body", 4000),
    ("supervisor._question_for", "question", DESCRIPTION_LIMIT),
    ("supervisor._question_for", "alarm_reason", DESCRIPTION_LIMIT),
    ("supervisor.escalation_context", "evidence", EVIDENCE_LIMIT),
    ("supervisor.escalation_context", "verdict_reason", DESCRIPTION_LIMIT),
]


def _render(builder: str, field: str, monkeypatch) -> str:
    if builder == "gates.build_request_question":
        return _gates_request(field)
    if builder == "gates.queue_for_review":
        return _gates_queue(field)
    if builder == "automerge._request_question":
        return _automerge_request(field)
    if builder == "remedies._request_question":
        return _remedies_request(field)
    if builder == "remedies.propose":
        return _remedies_propose(field, monkeypatch)
    if builder == "remedies.propose_fix":
        return _remedies_propose_fix(field, monkeypatch)
    if builder == "issues.ask_triage":
        return _issues_triage(field, monkeypatch)
    return _supervisor_question(field)


@pytest.mark.parametrize("builder, field, limit", CASES,
                         ids=[f"{b}:{f}" for b, f, _ in CASES])
def test_every_borrowed_field_is_bounded_by_its_named_constant(builder, field, limit,
                                                               monkeypatch):
    """An unbounded field is an unbounded prompt: 50000 characters in, at most `limit`
    out, and the quotation arrives inside a marker so the reviewer can see whose it is."""
    rendered = _render(builder, field, monkeypatch)

    assert HUGE not in rendered, f"{builder} renders {field} unbounded"
    assert _longest_run(rendered) <= limit, \
        f"{builder} lets {field} through at {_longest_run(rendered)} > {limit}"
    # The triage prompt's JSON `context=` is a machine record the daemon reads back, so
    # its two fields are bounded but not wrapped; the prose half quotes the title tagged.
    if builder == "issues.ask_triage" and field == "title":
        assert provenance.OPEN_MARKER in rendered
    if builder != "issues.ask_triage":
        before = rendered.split("X" * 32)[0]
        assert before.count(provenance.OPEN_MARKER) == \
            before.count(provenance.CLOSE_MARKER) + 1, \
            f"{builder} renders {field} outside a borrowed block"
