"""Every owner ruling tells the user in the inbox (docs/superpowers/specs/2026-11-07-neo-owns-os-origin-assumption-review.md §6)"""

from __future__ import annotations

import pytest

from jarvis import autoreview, timeline
from jarvis.central_store import CentralStore

from tests.test_autoreview import ROUTINE, ask, drain, inbox, started  # noqa: F401
from tests.test_owner_delivery import deliver, events, park, row

pytestmark = pytest.mark.usefixtures("neo_projects")

BATCHED = "The OS decided"


def neo_rows(wo_id: str) -> list[dict]:
    # `flag_assumptions_pending` adds its own row on an escalation: count only Neo's
    return [r for r in inbox(wo_id) if r["title"].startswith("Neo ")]


def one_notice(store, wo_id: str, outcome: str) -> tuple[dict, dict]:
    (r,) = neo_rows(wo_id)
    assert r["level"] == "info" and r["push"] == 0
    (ev,) = events(store, wo_id, "autoreview_owner_ruled")
    assert ev["outcome"] == outcome and ev["n"] == 1
    assert not [x for x in inbox(wo_id) if x["title"].startswith(BATCHED)]
    return r, ev


def test_an_owner_accept_is_one_neo_row_not_the_batched_notice(started):
    store, wo = park(started, f"FORCE_ACCEPT — {ROUTINE}")
    ask(started, store)
    drain(started)

    r, ev = one_notice(store, wo["id"], "accepted")
    assert r["title"] == f"Neo accepted assumption #1 on {wo['id']}"
    assert "add feature X" in r["body"] and "Asked by: schedule" in r["body"]
    assert f"jarvis wo show {wo['id']}" in r["body"]
    assert f'jarvis wo overrule {wo["id"]} 1 --feedback "…"' in r["body"]
    assert ev["neo_question_id"] == row(store, wo["id"])["neo_question_id"]
    label, _ = timeline._describe("autoreview_owner_ruled", ev)
    assert "Neo accepted assumption #1" in label


def test_an_owner_rework_is_one_neo_row(started):
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}")
    ask(started, store)
    drain(started)

    r, _ = one_notice(store, wo["id"], "reworked")
    assert r["title"] == f"Neo sent back for rework assumption #1 on {wo['id']}"
    assert f"jarvis wo overrule {wo['id']} 1" in r["body"]


def test_the_rework_cap_hands_it_to_the_user_with_why(started):
    store, wo = park(started, f"FORCE_DENY — {ROUTINE}")
    for attempt in range(autoreview.MAX_REWORKS + 1):
        if attempt:
            deliver(store, wo["id"], f"delivery {attempt + 1}")
        ask(started, store)
        drain(started)

    handed = [r for r in neo_rows(wo["id"]) if "handed you" in r["title"]]
    (r,) = handed
    assert r["title"] == f"Neo handed you assumption #1 on {wo['id']}"
    assert "Why it is yours" in r["body"] and "overrule" not in r["body"]
    assert "rework-cap" not in r["body"]
    caps = [e for e in events(store, wo["id"], "autoreview_owner_ruled")
            if e["outcome"] == "escalated"]
    assert [e["cause"] for e in caps] == ["rework-cap"]
    assert len(neo_rows(wo["id"])) == autoreview.MAX_REWORKS + 1


def test_conflicting_authority_hands_it_to_the_user(started):
    store, wo = park(started, f"FORCE_CONFLICT — {ROUTINE}")
    ask(started, store)
    drain(started)

    r, ev = one_notice(store, wo["id"], "escalated")
    assert r["title"] == f"Neo handed you assumption #1 on {wo['id']}"
    assert ev["cause"] == "conflicting-authority"
    assert "Why it is yours" in r["body"] and "overrule" not in r["body"]


def test_a_failed_call_tells_nothing(started):
    """kn-40db1828: a failure is no ruling, so no notice"""
    store, wo = park(started, f"FORCE_FAIL — {ROUTINE}")
    ask(started, store)
    drain(started)

    assert neo_rows(wo["id"]) == []
    assert events(store, wo["id"], "autoreview_owner_ruled") == []


def test_a_failed_notice_never_undoes_the_ruling(started, monkeypatch):
    def boom(self, **_kw):
        raise RuntimeError("inbox down")

    store, wo = park(started, f"FORCE_ACCEPT — {ROUTINE}")
    ask(started, store)
    monkeypatch.setattr(CentralStore, "add_inbox", boom)
    drain(started)

    assert row(store, wo["id"])["status"] == "accepted"
    assert len(events(store, wo["id"], "autoreview_owner_ruled")) == 1


def test_a_user_order_keeps_the_batched_notice_and_no_neo_row(started):
    """The rule above every other: a person's order is told as before"""
    store, wo = park(started, f"FORCE_ACCEPT — {ROUTINE}", origin="jarvis")
    ask(started, store)
    drain(started)

    assert row(store, wo["id"])["status"] == "accepted"
    (r,) = inbox(wo["id"])
    assert r["title"] == f"The OS decided 1 assumption(s) for you on {wo['id']}"
    assert events(store, wo["id"], "autoreview_owner_ruled") == []
