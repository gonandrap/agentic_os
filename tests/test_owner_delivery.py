"""Neo owns the delivered ruling on an OS-origin order: rework, the cap, the holds (docs/superpowers/specs/2026-11-07-neo-owns-os-origin-assumption-review.md §4)"""

from __future__ import annotations

import json

import pytest

from jarvis import autoreview, ops, timeline
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

# The fixtures are `test_autoreview.py`'s
from tests.test_autoreview import JUDGED, PR, ROUTINE, ask, drain, started  # noqa: F401

pytestmark = pytest.mark.usefixtures("neo_projects")

REWORK_HEAD = "[Neo, owner of this OS-origin order] rework assumption #1"


def park(daemon, text: str, *, origin: str = "schedule"):
    """A finished order parked on one assumption, auto-review on"""
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = True
    spec.validation.stakes_classifier = "regex"
    wo = ops.create_work_order("proj_a", "add feature X", description="d", origin=origin)
    ops.assume(wo["id"], text)
    store = ProjectStore(spec.path)
    deliver(store, wo["id"], "opened a PR")
    return store, store.get_work_order(wo["id"])


def deliver(store, wo_id: str, summary: str) -> None:
    ops.finish(wo_id, summary, pr_url=PR)
    row = store.latest_validation_round(wo_id=wo_id)
    if row is None or row["outcome"] not in (None, "", "open"):
        row = store.open_validation_round(wo_id=wo_id, fingerprint=summary)
    store.set_validation_head(row["id"], JUDGED)
    store.close_validation_round(row["id"], "passed", "")
    assert store.get_work_order(wo_id)["status"] == "needs_review"


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


def question(qid: int) -> dict:
    s = NeoStore()
    try:
        return s.get(qid)
    finally:
        s.close()


def row(store, wo_id: str) -> dict:
    (a,) = store.all_assumptions(wo_id)
    return a


def reworks(store, wo_id: str) -> list[dict]:
    return [m for m in store.list_messages(wo_id) if REWORK_HEAD in m["content"]]


def test_an_owner_accept_settles(started):
    store, wo = park(started, f"FORCE_ACCEPT — {ROUTINE}")
    ask(started, store)
    drain(started)

    assert row(store, wo["id"])["status"] == "accepted"
    assert events(store, wo["id"], autoreview.REWORKED_EVENT) == []


def test_an_owner_deny_is_a_rework_sent_to_the_worker(started):
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}")
    ask(started, store)
    asked = row(store, wo["id"])["neo_question_id"]
    assert "you own this decision" in question(asked)["question"].lower()

    drain(started)

    a = row(store, wo["id"])
    assert a["status"] == "pending" and a["neo_question_id"] == asked   # link KEPT
    (msg,) = reworks(store, wo["id"])
    assert "the worker chose wrongly" in msg["content"]
    assert "jarvis wo finish again with a fresh summary" in msg["content"]
    (ev,) = events(store, wo["id"], autoreview.REWORKED_EVENT)
    assert ev == {"assumption_id": a["id"], "n": 1, "reason": ev["reason"],
                  "neo_question_id": asked, "confirming": False, "attempt": 1}
    assert events(store, wo["id"], "autoreview_escalated") == []
    assert question(asked)["status"] == "answered"
    label, _ = timeline._describe(autoreview.REWORKED_EVENT, ev)
    assert "sent back to the worker" in label

    # A session to resume: the plain message is what puts the order back to work
    store.update_work_order(wo["id"], session_id="sess-owner")
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "running"


def test_the_re_delivery_re_asks_neo_on_the_same_row(started):
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}")
    ask(started, store)
    first = row(store, wo["id"])["neo_question_id"]
    drain(started)

    ask(started, store)          # not re-delivered yet: the summary is stale
    assert row(store, wo["id"])["neo_question_id"] == first

    deliver(store, wo["id"], "reworked: moved the helper")
    ask(started, store)

    again = row(store, wo["id"])["neo_question_id"]
    assert again != first
    assert "reworked: moved the helper" in question(again)["question"]


def test_the_third_non_accept_goes_to_the_user_with_the_cap(started):
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}")
    for attempt in range(autoreview.MAX_REWORKS + 1):
        if attempt:
            deliver(store, wo["id"], f"delivery {attempt + 1}")
        ask(started, store)
        drain(started)

    assert len(events(store, wo["id"], autoreview.REWORKED_EVENT)) == autoreview.MAX_REWORKS
    (esc,) = events(store, wo["id"], "autoreview_escalated")
    assert esc["cause"] == "rework-cap"
    assert "the worker chose wrongly" in esc["reason"]
    last = question(row(store, wo["id"])["neo_question_id"])
    assert last["status"] == "escalated" and last["escalation_cause"] == "rework-cap"
    assert row(store, wo["id"])["status"] == "pending"
    label, _ = timeline._describe("autoreview_escalated", esc)
    assert "left with you" in label and "rework-cap" not in label


def test_a_disallowed_escalate_is_a_rework_and_an_answer(started):
    """The fake's default reply escalates with no cause"""
    store, wo = park(started, ROUTINE)
    ask(started, store)
    drain(started)

    assert len(reworks(store, wo["id"])) == 1
    assert question(row(store, wo["id"])["neo_question_id"])["status"] == "answered"
    assert events(store, wo["id"], "autoreview_escalated") == []


def test_conflicting_authority_still_escalates(started):
    store, wo = park(started, f"FORCE_CONFLICT — {ROUTINE}")
    ask(started, store)
    drain(started)

    assert reworks(store, wo["id"]) == []
    assert events(store, wo["id"], autoreview.REWORKED_EVENT) == []
    assert len(events(store, wo["id"], "autoreview_escalated")) == 1
    q = question(row(store, wo["id"])["neo_question_id"])
    assert q["status"] == "escalated" and q["escalation_cause"] == "conflicting-authority"


def test_a_model_failure_leaves_the_question_pending_with_no_event(started):
    """kn-40db1828: a failed call is not a rework"""
    store, wo = park(started, f"FORCE_FAIL — {ROUTINE}")
    ask(started, store)
    drain(started)

    assert question(row(store, wo["id"])["neo_question_id"])["status"] == "queued"
    for kind in (autoreview.REWORKED_EVENT, "autoreview_escalated", "autoreview_accepted"):
        assert events(store, wo["id"], kind) == []
    assert reworks(store, wo["id"]) == []


def test_an_early_objection_on_an_owner_row_is_re_asked_not_held(started):
    store, wo = park(started, f"FORCE_ACCEPT — {ROUTINE}")
    a = row(store, wo["id"])
    store.record_provisional(a["id"], verdict=autoreview.PROVISIONAL_OBJECT,
                             reason="objected while it ran", model="sonnet",
                             stakes="routine")
    store.link_assumption_question(a["id"], 99)
    store.set_assumption_requestor(a["id"], autoreview.OWNER_REQUESTOR)
    store.add_event(wo["id"], "autoreview_asked",
                    {"assumption_id": a["id"], "n": 1, "neo_question_id": 99, "early": True})

    ask(started, store)

    held = [h.get("code") for h in events(store, wo["id"], "autoreview_held")]
    assert autoreview.HELD_OBJECTED not in held
    assert row(store, wo["id"])["neo_question_id"] != 99
    drain(started)
    assert row(store, wo["id"])["status"] == "accepted"


def test_an_owner_confirmation_rework_keeps_the_confirm_link_and_re_asks(started):
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}")
    a = row(store, wo["id"])
    store.record_provisional(a["id"], verdict=autoreview.PROVISIONAL_ACCEPT,
                             reason="fine while it ran", model="sonnet", stakes="routine")
    store.link_assumption_question(a["id"], 99)
    ask(started, store)
    confirm = row(store, wo["id"])["confirm_question_id"]
    drain(started)

    (ev,) = events(store, wo["id"], autoreview.REWORKED_EVENT)
    assert ev["confirming"] is True and ev["neo_question_id"] == confirm
    assert row(store, wo["id"])["confirm_question_id"] == confirm
    assert events(store, wo["id"], "autoreview_unconfirmed") == []

    deliver(store, wo["id"], "reworked after the confirmation")
    ask(started, store)
    assert row(store, wo["id"])["confirm_question_id"] != confirm


def test_a_user_order_delivery_is_unchanged(started):
    """The rule above every other: no owner behaviour on an order a person asked for"""
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}", origin="jarvis")
    ask(started, store)
    asked = row(store, wo["id"])["neo_question_id"]
    assert "you own this decision" not in question(asked)["question"].lower()
    drain(started)

    assert row(store, wo["id"])["requestor"] == "user"
    assert reworks(store, wo["id"]) == []
    assert events(store, wo["id"], autoreview.REWORKED_EVENT) == []
    (esc,) = events(store, wo["id"], "autoreview_escalated")
    assert esc["reason"].startswith("Neo would have turned this down")
    assert question(asked)["escalation_cause"] == "neo-denied"
