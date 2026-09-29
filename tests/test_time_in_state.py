"""Time in each state: the spans the two chokepoints write, and the one reader over them.

Built against a real `ProjectStore` in the style of `tests/test_active_time.py`'s
`Record` — the store stamps `db.now()` and there is no back-dating API, so a fixture that
needs a transition to have happened thirteen hours ago says so afterwards with SQL.
`now` is passed explicitly everywhere; nothing sleeps.

Spec: docs/superpowers/specs/2026-09-27-time-in-each-state.md.
"""

from __future__ import annotations

import json as _json
import os

import pytest

from jarvis import cli, landing, ops, timeline
from jarvis.project_store import (
    FO_STATUSES,
    WO_STATUSES,
    ProjectStore,
    feature_status_label,
)

MINUTE = 60.0
HOUR = 3600.0
#: An arbitrary epoch the fixtures hang off. Big enough that nothing reads as "unset".
T0 = 1_700_000_000.0
NOW = T0 + 24 * HOUR


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path / "proj")
    yield s
    s.close()


def backdate(store: ProjectStore, order_id: str, *stamps: float) -> None:
    """Put an order's spans, and its creation, on the clock — oldest first."""
    ids = [r["id"] for r in store.conn.execute(
        "SELECT id FROM wo_state_spans WHERE order_id=? ORDER BY id", (order_id,))]
    assert len(ids) == len(stamps), (ids, stamps)
    for span_id, ts in zip(ids, stamps):
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?", (ts, span_id))
    store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                       (stamps[0], order_id))
    store.conn.execute("UPDATE feature_orders SET created_at=? WHERE id=?",
                       (stamps[0], order_id))
    store.conn.commit()


def event_at(store: ProjectStore, wo_id: str, kind: str, ts: float) -> None:
    store.add_event(wo_id, kind)
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events WHERE wo_id=?)",
        (ts, wo_id))
    store.conn.commit()


def turn_at(store: ProjectStore, wo_id: str, started: float, ended: float) -> None:
    row = store.create_turn(wo_id, kind="message", prompt="go")
    store.finish_turn(row["id"], state="done")
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (started, ended, row["id"]))
    store.conn.commit()


def totals_by_status(payload: dict) -> dict[str, dict]:
    return {t["status"]: t for t in payload["totals"]}


# 1 -------------------------------------------------------------------------


def test_spans_sum_to_lifetime(store):
    wo = store.create_work_order("t")
    for status in ("running", "needs_review", "waiting_pr_merge"):
        store.set_status(wo["id"], status)
    backdate(store, wo["id"], T0, T0 + HOUR, T0 + 3 * HOUR, T0 + 4 * HOUR)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert round(sum(s["seconds"] for s in payload["spans"]), 2) == \
        payload["lifetime_seconds"]
    assert payload["lifetime_seconds"] == NOW - T0
    assert [s["status"] for s in payload["spans"]] == [
        "pending", "running", "needs_review", "waiting_pr_merge"]
    assert payload["spans"][-1]["open"] is True


def test_terminal_lifetime_stops_at_the_ending(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    store.set_status(wo["id"], "completed")
    backdate(store, wo["id"], T0, T0 + HOUR, T0 + 5 * HOUR)

    reading = ops.state_durations(store, wo_id=wo["id"], now=NOW)
    payload = reading.as_dict(NOW)
    assert payload["lifetime_seconds"] == 5 * HOUR
    assert round(sum(s["seconds"] for s in payload["spans"]), 2) == 5 * HOUR
    later = reading.as_dict(NOW + 10 * HOUR)
    assert later["lifetime_seconds"] == 5 * HOUR


# 2 -------------------------------------------------------------------------


def test_re_entry_accumulates(store):
    wo = store.create_work_order("t")
    for status in ("needs_review", "running", "needs_review"):
        store.set_status(wo["id"], status)
    backdate(store, wo["id"], T0, T0 + HOUR, T0 + 2 * HOUR, T0 + 3 * HOUR)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    entry = totals_by_status(payload)["needs_review"]
    assert entry["entries"] == 2
    assert entry["seconds"] == HOUR + (NOW - (T0 + 3 * HOUR))


# 3 -------------------------------------------------------------------------


def test_current_status_age_and_observer_events(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    store.set_status(wo["id"], "waiting_pr_merge")
    backdate(store, wo["id"], NOW - 20 * HOUR, NOW - 14 * HOUR, NOW - 13 * HOUR)
    turn_at(store, wo["id"], NOW - 14 * HOUR, NOW - 13 * HOUR)
    event_at(store, wo["id"], "turn_ended", NOW - 13 * HOUR)
    event_at(store, wo["id"], "health_finding", NOW - MINUTE)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["current_status"] == "waiting_pr_merge"
    assert payload["current_status_age"] == 13 * HOUR
    assert payload["last_activity_age"] == 13 * HOUR

    event_at(store, wo["id"], "landing_seen", NOW - 10 * MINUTE)
    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 10 * MINUTE
    assert payload["last_activity_kind"] == "landing_seen"


# 4 -------------------------------------------------------------------------


def test_settled_order_has_no_current_span(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "completed")
    backdate(store, wo["id"], T0, T0 + HOUR)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["current_status"] == ""
    assert payload["current_status_since"] is None
    assert payload["current_status_age"] is None
    assert payload["current_status_age_human"] is None
    assert not [s for s in payload["spans"] if s["open"]]


# 5 -------------------------------------------------------------------------


def test_brand_new_order(store):
    wo = store.create_work_order("t")
    backdate(store, wo["id"], NOW - MINUTE)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert [s["status"] for s in payload["spans"]] == ["pending"]
    assert totals_by_status(payload)["pending"]["entries"] == 1
    assert payload["last_activity_ts"] is None
    assert payload["last_activity_age"] is None
    assert ops.NO_ACTIVITY_NOTE in payload["notes"]


# 6 -------------------------------------------------------------------------


def test_no_change_guard(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "needs_review")
    store.set_status(wo["id"], "needs_review", pr_url="https://x/1")

    spans = store.state_spans(wo["id"])
    assert [s["to_status"] for s in spans] == ["pending", "needs_review"]
    assert len(store.events_of_kind(wo["id"], "status")) == 1
    assert store.get_work_order(wo["id"])["pr_url"] == "https://x/1"
    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert totals_by_status(payload)["needs_review"]["entries"] == 1


def test_park_unlanded_twice_writes_one_span(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    work = landing.Authored(branch="wo/t", base="main", commits=1)
    ops.park_unlanded(store, store.get_work_order(wo["id"]), work)
    ops.park_unlanded(store, store.get_work_order(wo["id"]), work)

    assert [s["to_status"] for s in store.state_spans(wo["id"])] == [
        "pending", "running", "needs_review"]
    assert len(store.events_of_kind(wo["id"], "status")) == 2
    assert len(store.events_of_kind(wo["id"], "work_unlanded")) == 1


# 7 -------------------------------------------------------------------------


def test_feature_order_spans_are_exact(store):
    fo = store.create_feature_order("f")
    for status in ("planning", "executing", "completed"):
        store.set_feature_status(fo["id"], status)
    backdate(store, fo["id"], T0, T0 + HOUR, T0 + 2 * HOUR, T0 + 3 * HOUR)

    reading = ops.state_durations(store, fo_id=fo["id"], now=NOW)
    payload = reading.as_dict(NOW)
    assert reading.order_kind == "fo"
    assert reading.approximate is False
    assert payload["lifetime_seconds"] == 3 * HOUR
    assert round(sum(s["seconds"] for s in payload["spans"]), 2) == 3 * HOUR
    assert payload["current_status"] == ""


# 8 -------------------------------------------------------------------------


def test_feature_activity_is_the_familys(store):
    fo = store.create_feature_order("f")
    manager = store.create_work_order("m", parent_id=fo["id"], kind="manager")
    child = store.create_work_order("c", parent_id=fo["id"])
    store.set_feature_status(fo["id"], "executing")
    turn_at(store, child["id"], NOW - 20 * MINUTE, NOW - 5 * MINUTE)
    assert manager["id"]  # idle: no turn, no message

    payload = ops.state_durations(store, fo_id=fo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 5 * MINUTE


# 9 -------------------------------------------------------------------------


def test_every_status_has_a_label():
    for status in WO_STATUSES:
        label = timeline.STATUS_LABEL.get(status, "")
        assert label and label != status, status
    for kind in ("feature", "improvement"):
        for status in FO_STATUSES:
            assert feature_status_label(kind, status), (kind, status)


# the reader's own contract ------------------------------------------------


def test_one_subject_only(store):
    wo = store.create_work_order("t")
    with pytest.raises(ops.OpsError):
        ops.state_durations(store, now=NOW)
    with pytest.raises(ops.OpsError):
        ops.state_durations(store, wo_id=wo["id"], fo_id="fo-1", now=NOW)


# the CLI surfaces (spec §6) -----------------------------------------------


def _out(capsys, argv: list[str]) -> str:
    assert cli.main(argv) == 0, argv
    return capsys.readouterr().out


#: Every key `as_dict` promises. Spelled out because the `--json` shape is a contract
#: other tooling reads (spec §4).
PAYLOAD_KEYS = (
    "order_id", "order_kind", "now", "approximate", "lifetime_seconds",
    "lifetime_human", "spans", "totals", "current_status", "current_status_since",
    "current_status_age", "current_status_age_human", "last_activity_ts",
    "last_activity_kind", "last_activity_age", "last_activity_age_human", "notes",
)


def test_wo_show_json_carries_time_in_state(jarvis_home, catalog_file, project, capsys):
    ops.start_os(str(catalog_file), foreground=True)
    s = ProjectStore(project)
    try:
        wo = s.create_work_order("ship the exporter")
        s.set_status(wo["id"], "needs_review")
    finally:
        s.close()

    doc = _json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))["time_in_state"]

    for key in PAYLOAD_KEYS:
        assert key in doc, key
    assert doc["current_status"] == "needs_review"
    assert [t["status"] for t in doc["totals"]] == ["pending", "needs_review"]


def test_wo_show_human_prints_the_status_a_duration_and_an_entry_count(
        jarvis_home, catalog_file, project, capsys):
    ops.start_os(str(catalog_file), foreground=True)
    s = ProjectStore(project)
    try:
        wo = s.create_work_order("ship the exporter")
        s.set_status(wo["id"], "needs_review")
    finally:
        s.close()

    out = _out(capsys, ["wo", "show", wo["id"]])

    assert "time in state" in out
    assert "needs_review" in out
    assert "1 entry" in out
    assert "← now" in out


def test_fo_show_carries_the_key_and_labels_a_coarse_span(
        jarvis_home, catalog_file, project, capsys):
    """The point of the flag: an unlabelled coarse span is the defect Neo's ruling names.

    A feature whose spans were never observed is every feature that predates the table,
    reproduced here by dropping the rows and letting the next open backfill them.
    """
    ops.start_os(str(catalog_file), foreground=True)
    s = ProjectStore(project)
    try:
        fo = s.create_feature_order("CSV export", description="the whole ask")
        s.conn.execute("DELETE FROM wo_state_spans WHERE order_id=?", (fo["id"],))
        s.conn.commit()
    finally:
        s.close()

    doc = _json.loads(_out(capsys, ["--json", "fo", "show", fo["id"]]))["time_in_state"]
    for key in PAYLOAD_KEYS:
        assert key in doc, key
    assert doc["approximate"] is True

    out = _out(capsys, ["fo", "show", fo["id"]])
    assert "time in state" in out
    assert "a feature order keeps no event trail" in out


# 10 ------------------------------------------------------------------------
#
# Spec: docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md, fix 4 — the
# sixth activity source. A lead whose own transcript is quiet while its subagent's grows
# is the incident; `stat()` mtime, and an absent transcript contributes NOTHING.

SECOND = 1.0


def _transcript(config_dir, cwd, session_id: str, *, subagent: str = "",
                mtime: float = 0.0):
    munged = "".join(c if c.isalnum() else "-" for c in str(cwd))
    base = config_dir / "projects" / munged
    path = base / f"{session_id}.jsonl" if not subagent else (
        base / session_id / "subagents" / subagent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "assistant"}\n')
    import os as _os
    _os.utime(path, (mtime, mtime))
    return path


def _quiet_wo(store, *, session_id: str = "sess-1"):
    """An order whose every table went quiet 45 minutes ago."""
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    backdate(store, wo["id"], NOW - 20 * HOUR, NOW - 14 * HOUR)
    turn_at(store, wo["id"], NOW - 14 * HOUR, NOW - 45 * MINUTE)
    event_at(store, wo["id"], "turn_ended", NOW - 45 * MINUTE)
    store.update_work_order(wo["id"], session_id=session_id)
    return wo


def test_a_live_subagent_transcript_counts_as_activity(store, tmp_path, monkeypatch):
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    wo = _quiet_wo(store)
    _transcript(config, store.project_path, "sess-1", mtime=NOW - 14 * HOUR)
    _transcript(config, store.project_path, "sess-1", subagent="a.jsonl",
                mtime=NOW - 30 * SECOND)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 30 * SECOND
    assert payload["last_activity_kind"] == "transcript"


def test_a_transcript_in_the_worktree_counts(store, tmp_path, monkeypatch):
    """The cwd is the worktree when the order has one — `worktree_path`'s computation."""
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    wo = _quiet_wo(store, session_id="sess-2")
    store.update_work_order(wo["id"], worktree="wo-42639749")
    worktree = store.project_path / ".claude" / "worktrees" / "wo-42639749"
    _transcript(config, worktree, "sess-2", mtime=NOW - 2 * MINUTE)

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 2 * MINUTE
    assert payload["last_activity_kind"] == "transcript"


def test_a_missing_transcript_contributes_nothing(store, tmp_path, monkeypatch):
    """Absent, not 1970: no tuple at all, so the note stands and the timestamp is None."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    wo = store.create_work_order("t")
    backdate(store, wo["id"], NOW - MINUTE)
    store.update_work_order(wo["id"], session_id="sess-missing")

    payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_ts"] is None
    assert payload["last_activity_age"] is None
    assert payload["last_activity_kind"] == ""
    assert ops.NO_ACTIVITY_NOTE in payload["notes"]


def test_an_unreadable_subagent_directory_is_absent_not_zero(
        store, tmp_path, monkeypatch):
    """The glob yields nothing: no tuple, so the note stands. Not the raising path."""
    import os as _os
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    wo = store.create_work_order("t")
    backdate(store, wo["id"], NOW - MINUTE)
    store.update_work_order(wo["id"], session_id="sess-3")
    path = _transcript(config, store.project_path, "sess-3", subagent="a.jsonl",
                       mtime=NOW - 30 * SECOND)
    _os.chmod(path.parent, 0o000)
    try:
        payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    finally:
        _os.chmod(path.parent, 0o755)

    assert payload["last_activity_ts"] is None
    assert payload["last_activity_age"] is None
    assert ops.NO_ACTIVITY_NOTE in payload["notes"]


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root reads through a 0o000 directory, so nothing raises")
def test_an_unreadable_transcript_parent_is_absent_not_zero(
        store, tmp_path, monkeypatch):
    """The RAISING path: `stat()` on a file under a 0o000 parent is a PermissionError.

    Swallowed, contributing no tuple — `Path.exists()` would have turned it into a
    silent False, which is why the read stats directly.
    """
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    wo = store.create_work_order("t")
    backdate(store, wo["id"], NOW - MINUTE)
    store.update_work_order(wo["id"], session_id="sess-4")
    path = _transcript(config, store.project_path, "sess-4", mtime=NOW - 30 * SECOND)
    os.chmod(path.parent, 0o000)
    try:
        with pytest.raises(PermissionError):    # the read is genuinely denied here
            path.stat()
        payload = ops.state_durations(store, wo_id=wo["id"], now=NOW).as_dict(NOW)
    finally:
        os.chmod(path.parent, 0o755)

    assert payload["last_activity_ts"] is None
    assert payload["last_activity_age"] is None
    assert ops.NO_ACTIVITY_NOTE in payload["notes"]


def test_a_feature_inherits_a_childs_transcript(store, tmp_path, monkeypatch):
    config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    fo = store.create_feature_order("f")
    child = store.create_work_order("c", parent_id=fo["id"])
    store.set_feature_status(fo["id"], "executing")
    store.update_work_order(child["id"], session_id="sess-child")
    _transcript(config, store.project_path, "sess-child", subagent="a.jsonl",
                mtime=NOW - 3 * MINUTE)

    payload = ops.state_durations(store, fo_id=fo["id"], now=NOW).as_dict(NOW)
    assert payload["last_activity_age"] == 3 * MINUTE
    assert payload["last_activity_kind"] == "transcript"
# 11 -- no status write without a span, and a reader that refuses to disagree ----------
# Spec: docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md §2d2


def test_the_atomic_claim_records_its_span(store):
    """`claim_next_pending` is one conditional UPDATE and stays one — the span is written
    beside it on a claim that won."""
    wo = store.create_work_order("t")

    claimed = store.claim_next_pending()

    assert claimed["id"] == wo["id"] and claimed["status"] == "dispatching"
    spans = store.state_spans(wo["id"])
    assert [s["to_status"] for s in spans] == ["pending", "dispatching"]
    assert spans[-1]["from_status"] == "pending"
    assert spans[-1]["trigger"] == "claim"


def test_a_claim_that_found_nothing_writes_no_span(store):
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")

    assert store.claim_next_pending() is None
    assert [s["to_status"] for s in store.state_spans(wo["id"])] == ["pending", "running"]


def test_a_dispatch_retry_records_the_way_back_to_pending(store):
    """The three raw-SQL bypasses left neither a span nor a `status` event, so an order
    that went round the retry loop read as still `dispatching` for ever."""
    wo = store.create_work_order("t")
    store.claim_next_pending()

    assert store.release_dispatch_claim(wo["id"], "claude is not on PATH") == "pending"

    assert [s["to_status"] for s in store.state_spans(wo["id"])] == [
        "pending", "dispatching", "pending"]
    assert store.get_work_order(wo["id"])["dispatch_attempts"] == 1
    assert store.get_work_order(wo["id"])["retry_after"]


def test_a_dispatch_that_spent_its_launches_records_the_failed_span(store):
    wo = store.create_work_order("t")
    store.claim_next_pending()

    assert store.release_dispatch_claim(wo["id"], "boom", max_attempts=1) == "failed"

    assert [s["to_status"] for s in store.state_spans(wo["id"])] == [
        "pending", "dispatching", "failed"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "failed" and fresh["retry_after"] is None


def test_the_backfill_fills_a_gap_instead_of_skipping_the_order(tmp_path):
    """wo-3615faf7's durable half: `_backfill_wo_spans` was all-or-nothing per order, so
    an order with a partial history could never be repaired by anything."""
    path = tmp_path / "gapped"
    store = ProjectStore(path)
    try:
        wo = store.create_work_order("t")
        store.set_status(wo["id"], "running")
        store.set_status(wo["id"], "needs_review")
        # The pre-upgrade daemon's shape: the `status` event with no span beside it.
        store.conn.execute(
            "DELETE FROM wo_state_spans WHERE order_id=? AND to_status='needs_review'",
            (wo["id"],))
        store.conn.commit()
    finally:
        store.close()

    reopened = ProjectStore(path)
    try:
        assert [s["to_status"] for s in reopened.state_spans(wo["id"])] == [
            "pending", "running", "needs_review"]
        assert ops.state_durations(reopened, wo_id=wo["id"],
                                   now=NOW).current_status == "needs_review"
    finally:
        reopened.close()


def test_a_store_whose_spans_are_current_pays_two_scalar_reads(tmp_path):
    """The gap-fill runs in `__init__`, so on every CLI invocation and every reconcile of
    every project. Without the high-water guard the per-order GROUP BY ran EVERY time —
    260ms on a 500-order project — because `set_status` stamped its event after its own
    span. The two rows share a moment now, and this is the assertion that keeps it."""
    path = tmp_path / "current"
    store = ProjectStore(path)
    try:
        for i in range(3):
            wo = store.create_work_order(f"t{i}")
            store.set_status(wo["id"], "running")
            store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    reopened = ProjectStore(path)
    sql: list[str] = []
    reopened.conn.set_trace_callback(sql.append)
    try:
        reopened._backfill_wo_spans()
    finally:
        reopened.conn.set_trace_callback(None)
        reopened.close()

    assert not [s for s in sql if "GROUP BY" in s.upper()]
    assert len(sql) == 4      # two counts, two MAXes, and nothing per order


def test_the_reader_reports_the_column_when_the_spans_disagree(store):
    """The belt. A reader that silently believed the gap is why nobody noticed for 20.7h
    — and the note carries no elapsed time, because it becomes an attention reason."""
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "waiting_input")
    store.conn.execute("UPDATE work_orders SET status='needs_review' WHERE id=?",
                       (wo["id"],))
    store.conn.commit()

    reading = ops.state_durations(store, wo_id=wo["id"], now=NOW)
    payload = reading.as_dict(NOW)

    assert reading.current_status == "needs_review"
    assert reading.current_status_since is None
    assert ops.SPANS_BEHIND_NOTE in reading.notes
    assert payload["current_status_age"] is None
    assert payload["current_status_age_human"] is None


def test_an_agreeing_open_span_is_still_the_current_status(store):
    """The other half: the belt only fires on a disagreement, and the ordinary reading
    is unchanged."""
    wo = store.create_work_order("t")
    store.set_status(wo["id"], "running")
    backdate(store, wo["id"], T0, T0 + HOUR)

    reading = ops.state_durations(store, wo_id=wo["id"], now=NOW)

    assert reading.current_status == "running"
    assert reading.current_status_since == T0 + HOUR
    assert ops.SPANS_BEHIND_NOTE not in reading.notes
