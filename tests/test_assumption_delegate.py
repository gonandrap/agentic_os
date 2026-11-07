"""'Let Neo decide': the user hands an escalated assumption back to Neo (io-c223fe10)"""

from __future__ import annotations

import json

import pytest

from jarvis import neo, ops, timeline
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

PR = "https://github.com/acme/proj/pull/7"
JUDGED = "a1b2c3d4e5f6000000000000000000000000aaaa"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def park(daemon, *texts: str):
    """Parked on its assumptions with auto-review OFF: the user is the only decider"""
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = False
    wo = ops.create_work_order("proj_a", "add feature X", description="d")
    for text in texts:
        ops.assume(wo["id"], text)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(spec.path)
    row = store.latest_validation_round(wo_id=wo["id"]) or store.open_validation_round(
        wo_id=wo["id"], fingerprint="fp1")
    store.set_validation_head(row["id"], JUDGED)
    store.close_validation_round(row["id"], "passed", "")
    wo = store.get_work_order(wo["id"])
    assert wo["status"] == "needs_review"
    return store, wo


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


def delegated_questions() -> list[dict]:
    s = NeoStore()
    try:
        return [q for q in s.list_questions() if neo.is_delegated(q)]
    finally:
        s.close()


def inbox_titles(wo_id: str) -> list[str]:
    from jarvis.central_store import CentralStore

    c = CentralStore()
    try:
        return [r["title"] for r in c.unacked_inbox() if r["wo_id"] == wo_id]
    finally:
        c.close()


def test_delegated_accept_is_neos_and_says_it_was_delegated(started):
    store, wo = park(started, "FORCE_ACCEPT named the helper `_render_row`")

    out = ops.delegate_assumptions(wo["id"], feedback="no view on naming")
    (q,) = delegated_questions()
    assert out["delegated"][0]["question_id"] == q["id"]
    assert "no view on naming" in q["question"]
    assert "Escalating is NOT an answer" in q["question"]

    started._neo_drain()

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted" and row["decided_by"] == "neo"
    (accepted,) = events(store, wo["id"], "autoreview_accepted")
    assert accepted["delegated"] is True
    assert any("Neo decided (delegated by you)" in t for t in inbox_titles(wo["id"]))
    label, _ = timeline._describe("autoreview_accepted", accepted)
    assert "Neo decided (delegated by you)" in label
    assert NeoStore().delegations("proj_a") == 1


def test_delegated_deny_rejects_and_sends_the_reason_to_the_worker(started):
    store, wo = park(started, "FORCE_DENY put the helper at the bottom of the module")

    ops.delegate_assumptions(wo["id"])
    started._neo_drain()

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "rejected" and row["decided_by"] == "neo"
    (msg,) = [m for m in store.list_messages(wo["id"])
              if "delegated assumption #1" in m["content"]]
    assert "the worker chose wrongly" in msg["content"]
    assert events(store, wo["id"], "autoreview_rejected")[0]["delegated"] is True


def test_a_high_stakes_row_is_refused_and_the_refusal_is_recorded(started):
    store, wo = park(started, "FORCE_ACCEPT reused the production api key")

    with pytest.raises(ops.OpsError, match="api key"):
        ops.delegate_assumptions(wo["id"])

    assert delegated_questions() == []
    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending"
    (rec,) = events(store, wo["id"], ops.DELEGATED_EVENT)
    assert rec["asked"] == [] and rec["refused"][0]["n"] == 1
    assert ops.review_state(store, wo)["delegate"] == ""


def test_only_the_unheld_rows_go_to_neo(started):
    store, wo = park(started, "FORCE_ACCEPT named the helper `_render_row`",
                     "FORCE_ACCEPT dropped the users table")

    out = ops.delegate_assumptions(wo["id"])

    assert [d["n"] for d in out["delegated"]] == [1]
    assert [r["n"] for r in out["refused"]] == [2]


@pytest.mark.parametrize("marker", ["FORCE_FAIL", "FORCE_ESCALATE"])
def test_a_failed_or_non_answer_reply_leaves_the_row_pending(started, marker):
    """kn-40db1828: no verdict synthesised; escalate is not an answer to a delegation"""
    store, wo = park(started, f"{marker} named the helper `_render_row`")

    ops.delegate_assumptions(wo["id"])
    started._neo_drain()

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending"
    (q,) = delegated_questions()
    assert q["status"] == "queued"
    assert events(store, wo["id"], "autoreview_accepted") == []
    assert events(store, wo["id"], "autoreview_rejected") == []


def test_once_the_retries_are_spent_the_row_comes_back_saying_why(started):
    """Silence is the failure (kn-53a20d3a): the user hears Neo did not decide it"""
    from jarvis.neo_store import MAX_ANSWER_ATTEMPTS

    store, wo = park(started, "FORCE_ESCALATE named the helper `_render_row`")
    ops.delegate_assumptions(wo["id"])
    (q,) = delegated_questions()
    s = NeoStore()
    s.conn.execute("UPDATE questions SET attempts=? WHERE id=?",
                   (MAX_ANSWER_ATTEMPTS, q["id"]))
    s.close()

    started._neo_drain()

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending"
    (back,) = [e for e in events(store, wo["id"], "autoreview_escalated")
               if e.get("delegated")]
    assert "Neo did not decide it" in back["reason"]
    assert any("Neo did not decide" in t for t in inbox_titles(wo["id"]))
    label, _ = timeline._describe("autoreview_escalated", back)
    assert "yours again" in label
