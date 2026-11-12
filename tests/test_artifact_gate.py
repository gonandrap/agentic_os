"""The completion gate and the user's decision (docs/superpowers/specs/2026-11-10-artifact-session-hold.md §5)"""

from __future__ import annotations

import pytest

from jarvis import cli, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

URL = "https://claude.ai/artifact/abc-123"
BLOCKER = invariants.artifact_decision_blocker(1)


@pytest.fixture(autouse=True)
def _user_shell(monkeypatch):
    # The suite may itself run inside a worker session
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def stops(monkeypatch):
    calls: list[str] = []

    def fake_stop(wo, store):
        calls.append(wo["id"])
        return {"stopped": False, "reason": "test"}

    monkeypatch.setattr(ops, "stop_worker_session", fake_stop)
    return calls


def _owning(project, title="report", *, session_id="sess-w1"):
    wo = ops.create_work_order("proj_a", title)
    store = ProjectStore(project)
    store.update_work_order(wo["id"], session_id=session_id, transport="background")
    art = store.record_artifact(wo["id"], URL, title="r", session_id=session_id,
                                via_subagent=False)
    store.close()
    return wo["id"], art["id"]


def _row(project, wo_id):
    store = ProjectStore(project)
    try:
        return store.get_work_order(wo_id)
    finally:
        store.close()


def _assert_parked(project, wo_id):
    store = ProjectStore(project)
    try:
        wo = store.get_work_order(wo_id)
        assert wo["status"] == "needs_review"
        assert invariants.true_blockers(store, wo)[0] == BLOCKER
        assert wo["attention_reason"] == BLOCKER
        assert store.events_of_kind(wo_id, ops.ARTIFACT_DECISION_REQUIRED_EVENT)
    finally:
        store.close()


def test_mark_done_parks_an_order_with_an_undecided_artifact(started, project, stops):
    wo_id, _ = _owning(project)

    out = ops.mark_done(wo_id)

    assert out["status"] == "needs_review"
    assert stops == []
    _assert_parked(project, wo_id)


def test_complete_merged_parks_and_reports_it(started, project, stops):
    wo_id, _ = _owning(project)
    store = ProjectStore(project)
    store.update_work_order(wo_id, pr_url="https://github.com/o/r/pull/1")

    out = ops.complete_merged(store, store.get_work_order(wo_id))
    store.close()

    assert out["status"] == "needs_review"
    assert stops == []
    _assert_parked(project, wo_id)


def test_a_submit_in_session_parks(started, project, stops):
    wo_id, _ = _owning(project)
    store = ProjectStore(project)

    status = ops.settle_in_session(store, store.get_work_order(wo_id), "verdict_submitted")
    store.close()

    assert status == "needs_review"
    assert stops == []
    _assert_parked(project, wo_id)


def test_keep_then_one_tick_completes_it(started, project, stops):
    wo_id, art_id = _owning(project)
    ops.mark_done(wo_id)

    out = ops.decide_artifact("proj_a", wo_id, art_id, "keep", by="cli")
    assert out["decision"] == "keep" and out["owed"] == 0
    started.tick()

    assert _row(project, wo_id)["status"] == "completed"
    store = ProjectStore(project)
    assert store.events_of_kind(wo_id, ops.ARTIFACT_DECIDED_EVENT)
    store.close()


def test_delete_without_yes_refuses(started, project):
    wo_id, art_id = _owning(project)

    with pytest.raises(ops.OpsError, match="--yes"):
        ops.decide_artifact("proj_a", wo_id, art_id, "delete", by="cli")

    store = ProjectStore(project)
    assert store.artifact(art_id)["decision"] is None
    store.close()


def test_delete_tells_the_user_once_and_never_the_session(started, project, stops):
    wo_id, art_id = _owning(project)
    ops.mark_done(wo_id)
    store = ProjectStore(project)

    def notices():
        return store.conn.execute("SELECT * FROM notifications WHERE wo_id=? AND "
                                  "source='artifact' ORDER BY id", (wo_id,)).fetchall()

    ops.decide_artifact("proj_a", wo_id, art_id, "delete", by="cli", yes=True)
    ops.decide_artifact("proj_a", wo_id, art_id, "delete", by="cli", yes=True)

    told = notices()
    assert len(told) == 1
    assert URL in told[0]["body"] and f"jarvis artifact delete {art_id} --yes --done" \
        in told[0]["body"]
    assert store.queued_messages(wo_id) == []
    # Requested is not carried out: still owed, still held, still parked
    assert store.artifact(art_id)["decision"] == "delete_requested"
    assert ops.artifact_owed(store, wo_id) == 1
    assert [w["id"] for w in store.held_orders()] == [wo_id]
    store.close()
    started.tick()
    assert _row(project, wo_id)["status"] == "needs_review"

    ops.decide_artifact("proj_a", wo_id, art_id, "delete", by="cli", yes=True, done=True)
    started.tick()

    assert _row(project, wo_id)["status"] == "completed"
    with pytest.raises(ops.OpsError, match="deleted"):
        ops.decide_artifact("proj_a", wo_id, art_id, "keep", by="cli")


def test_cancelled_order_keeps_the_blocker(started, project, stops):
    wo_id, _ = _owning(project)

    ops.cancel(wo_id)

    store = ProjectStore(project)
    wo = store.get_work_order(wo_id)
    assert wo["status"] == "cancelled"
    assert BLOCKER in invariants.true_blockers(store, wo)
    store.close()


def test_failed_order_carries_the_blocker(started, project):
    wo_id, _ = _owning(project)
    store = ProjectStore(project)
    store.set_status(wo_id, "failed")

    assert BLOCKER in invariants.true_blockers(store, store.get_work_order(wo_id))
    store.close()


def test_backstop_parks_a_completed_order(started, project):
    wo_id, _ = _owning(project)
    store = ProjectStore(project)
    store.set_status(wo_id, "completed")

    found = list(invariants.check_undecided_artifact_not_completed(store))

    assert [v.invariant for v in found] == [invariants.INV_ARTIFACT_UNDECIDED_COMPLETED]
    store.close()
    _assert_parked(project, wo_id)


def test_an_order_with_no_artifact_completes_as_today(started, project, stops):
    wo = ops.create_work_order("proj_a", "plain")

    assert ops.mark_done(wo["id"])["status"] == "completed"
    assert stops == [wo["id"]]


def test_ack_refuses_an_owed_order(started, project, stops):
    wo_id, _ = _owning(project)
    ops.mark_done(wo_id)

    with pytest.raises(ops.OpsError, match="jarvis artifact"):
        ops.ack_attention(wo_id)


@pytest.mark.parametrize("argv", [["keep"], ["delete", "--yes"]])
def test_an_os_session_cannot_decide(started, project, monkeypatch, capsys, argv):
    wo_id, art_id = _owning(project, session_id="sess-w1")
    store = ProjectStore(project)
    events = len(store.list_events(wo_id))
    store.close()

    for name, value in (("JARVIS_WO_ID", wo_id), ("CLAUDE_CODE_SESSION_ID", "sess-w1")):
        monkeypatch.setenv(name, value)
        rc = cli.main(["artifact", argv[0], art_id, *argv[1:]])
        monkeypatch.delenv(name)

        assert rc != 0
        assert ops.ARTIFACT_DECIDER_REFUSAL in capsys.readouterr().err
        store = ProjectStore(project)
        assert store.artifact(art_id)["decision"] is None
        assert len(store.list_events(wo_id)) == events
        store.close()

    assert cli.main(["artifact", argv[0], art_id, *argv[1:]]) == 0
    store = ProjectStore(project)
    assert store.artifact(art_id)["decision"] in ("keep", "delete_requested")
    store.close()
