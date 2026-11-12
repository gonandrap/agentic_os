"""The artifact record and the manual-only invariant (docs/superpowers/specs/2026-11-10-artifact-session-hold.md §3)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import cli, hooks
from jarvis.catalog import parse_catalog
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

URL = "https://claude.ai/artifact/abc-123"
PUBLISHED = f"Published report.html at {URL} (Version 1, private)"


def _project(tmp_path: Path, name: str = "proj_a") -> Path:
    root = tmp_path / name
    (root / ".jarvis").mkdir(parents=True)
    return root


def _order(root: Path, *, requestor: str | None = "user", session_id: str = "sess-1",
           background: bool = True) -> str:
    store = ProjectStore(root)
    try:
        wo = store.create_work_order("report", session_id=session_id, requestor=requestor)
        if background:
            store.update_work_order(wo["id"], transport="background")
        return wo["id"]
    finally:
        store.close()


def _env(root: Path, wo_id: str | None) -> dict[str, str]:
    env = {"JARVIS_PROJECT_PATH": str(root)}
    if wo_id:
        env["JARVIS_WO_ID"] = wo_id
    return env


def _post(root, wo_id, *, tool_input=None, response=PUBLISHED, tool="Artifact",
          session_id="sess-1", env=None, **extra):
    payload = {"hook_event_name": "PostToolUse", "tool_name": tool, "session_id": session_id,
               "cwd": str(root), "tool_input": tool_input or {"file_path": "r.html"},
               "tool_response": response, **extra}
    return hooks.handle_hook(payload, _env(root, wo_id) if env is None else env)


def _pre(root, wo_id, *, tool_input=None, tool="Artifact", session_id="sess-1", cwd=None,
         env=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "session_id": session_id,
               "cwd": str(cwd or root),
               "tool_input": {"file_path": "r.html"} if tool_input is None else tool_input}
    return hooks.preflight_decision(payload, _env(root, wo_id) if env is None else env)


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _rows(root, wo_id):
    store = ProjectStore(root)
    try:
        return store.artifacts_of(wo_id)
    finally:
        store.close()


def _events(root, wo_id, kind):
    store = ProjectStore(root)
    try:
        return [e for e in store.list_events(wo_id) if e["kind"] == kind]
    finally:
        store.close()


@pytest.fixture()
def root(jarvis_home, tmp_path):
    return _project(tmp_path)


# --- capture (§3.2)

def test_publish_records_one_row_and_an_event(root):
    wo_id = _order(root)
    _post(root, wo_id, tool_input={"file_path": "r.html", "title": "Report"})

    rows = _rows(root, wo_id)
    assert len(rows) == 1
    assert rows[0]["url"] == URL and rows[0]["title"] == "Report"
    assert rows[0]["id"].startswith("art-")
    assert rows[0]["session_id"] == "sess-1" and rows[0]["via_subagent"] == 0
    assert rows[0]["decision"] is None
    assert len(_events(root, wo_id, "artifact_published")) == 1


def test_republish_updates_the_row(root):
    wo_id = _order(root)
    _post(root, wo_id)
    first = _rows(root, wo_id)[0]
    _post(root, wo_id, tool_input={"file_path": "r.html", "url": URL},
          response=[{"type": "text", "text": f"Updated {URL} (Version 2)"}])

    rows = _rows(root, wo_id)
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["updated_at"] >= first["updated_at"]
    assert rows[0]["published_at"] == first["published_at"]


def test_code_artifact_url_and_subagent_flag(root):
    wo_id = _order(root)
    url = "https://claude.ai/code/artifact/0f1e2d3c-aaaa"
    _post(root, wo_id, response={"text": f"Published at {url}"}, agent_id="agent-7")

    rows = _rows(root, wo_id)
    assert rows[0]["url"] == url and rows[0]["via_subagent"] == 1


def test_asset_upload_and_other_tools_record_nothing(root):
    wo_id = _order(root)
    _post(root, wo_id, tool_input={"url": URL, "file_path": "a.png", "asset": True})
    _post(root, wo_id, tool="Write", tool_input={"file_path": "x.txt"})
    _post(root, wo_id, tool_input={"action": "read", "url": URL})

    assert _rows(root, wo_id) == []


def test_delete_marks_the_row_deleted(root):
    wo_id = _order(root)
    _post(root, wo_id)
    _post(root, wo_id, tool_input={"action": "delete", "url": URL}, response="Deleted")

    row = _rows(root, wo_id)[0]
    assert row["decision"] == "deleted"
    assert _events(root, wo_id, "artifact_decision")


def test_asset_delete_leaves_the_row(root):
    wo_id = _order(root)
    _post(root, wo_id)
    _post(root, wo_id, tool_input={"action": "delete", "url": URL, "path": "a" * 32},
          response="Deleted")

    assert _rows(root, wo_id)[0]["decision"] is None


def test_hook_with_no_order_is_a_no_op(root):
    _order(root)
    assert _post(root, None, session_id="someone-else") is None

    store = ProjectStore(root)
    try:
        assert store.undecided_artifacts() == []
    finally:
        store.close()


def test_bare_resumed_session_resolves_through_find_by_session(root):
    wo_id = _order(root, session_id="sess-9")
    _post(root, None, session_id="sess-9", env={})

    assert len(_rows(root, wo_id)) == 1


# --- store (§3.1)

def test_session_id_index_exists(root):
    store = ProjectStore(root)
    try:
        indexes = store.conn.execute("PRAGMA index_list(work_orders)").fetchall()
        cols = {store.conn.execute(f"PRAGMA index_info({ix['name']})").fetchone()["name"]
                for ix in indexes}
    finally:
        store.close()
    assert "session_id" in cols


def test_decisions_held_orders_and_events(root):
    wo_id = _order(root)
    other = _order(root, session_id="sess-2", background=False)
    store = ProjectStore(root)
    try:
        art = store.record_artifact(wo_id, URL, title="R", session_id="s",
                                    via_subagent=False)
        store.record_artifact(other, URL, title="R", session_id="s", via_subagent=False)
        assert store.has_artifact(wo_id)
        assert [w["id"] for w in store.held_orders()] == [wo_id]

        store.set_artifact_decision(art["id"], "delete_requested", "cli")
        assert [a["id"] for a in store.undecided_artifacts(wo_id)] == [art["id"]]
        store.set_artifact_decision(art["id"], "keep", "ui")
        assert store.undecided_artifacts(wo_id) == []
        assert store.held_orders() == []
        row = store.artifact(art["id"])
        assert row["decision"] == "keep" and row["decided_by"] == "ui"
        assert row["decided_at"] is not None
        with pytest.raises(ValueError):
            store.set_artifact_decision(art["id"], "maybe", "cli")
        with pytest.raises(KeyError):
            store.set_artifact_decision("art-nope", "keep", "cli")
    finally:
        store.close()
    assert len(_events(root, wo_id, "artifact_decision")) == 2


# --- deny (§3.3)

def test_publish_denied_for_os_order(root):
    wo_id = _order(root, requestor="os")
    result = _pre(root, wo_id)

    assert _decision(result) == "deny"
    assert "requestor=os" in _reason(result)
    assert "Write the report to a file instead." in _reason(result)


def test_publish_passes_for_user_order(root):
    wo_id = _order(root)
    assert _decision(_pre(root, wo_id)) in (None, "allow")


def test_interactive_session_passes_untouched(root, tmp_path):
    _order(root)
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    assert _pre(root, None, session_id="mine", cwd=elsewhere, env={},
                tool_input={"file_path": "r.html", "capabilities": {"db": {}}}) is None


def test_missing_order_fails_closed(root):
    result = _pre(root, "wo-gone", session_id="sess-x")

    assert _decision(result) == "deny"
    assert "sess-x" in _reason(result)


def test_worker_worktree_cwd_without_an_order_fails_closed(root):
    worktree = root / ".claude" / "worktrees" / "wo-abcd1234"
    worktree.mkdir(parents=True)
    result = _pre(root, None, session_id="sess-x", cwd=worktree, env={})

    assert _decision(result) == "deny"


@pytest.mark.parametrize("key", ["capabilities", "files", "type_url", "asset", "pin",
                                 "some_future_key"])
def test_publish_with_unlisted_key_denied(root, key):
    wo_id = _order(root)
    result = _pre(root, wo_id, tool_input={"file_path": "r.html", key: True})

    assert _decision(result) == "deny"
    assert key in _reason(result)


def test_unlisted_action_denied(root):
    wo_id = _order(root)
    assert _decision(_pre(root, wo_id, tool_input={"action": "pin", "url": URL})) == "deny"
    assert _decision(_pre(root, wo_id, tool_input={"action": "read", "url": URL})) is None


def test_delete_needs_a_delete_requested_row(root):
    wo_id = _order(root)
    store = ProjectStore(root)
    try:
        art = store.record_artifact(wo_id, URL, title=None, session_id="s",
                                    via_subagent=False)
    finally:
        store.close()
    delete = {"action": "delete", "url": URL}
    assert _decision(_pre(root, wo_id, tool_input=delete)) == "deny"

    store = ProjectStore(root)
    try:
        store.set_artifact_decision(art["id"], "delete_requested", "cli")
    finally:
        store.close()
    assert _decision(_pre(root, wo_id, tool_input=delete)) is None


def test_worker_bash_cannot_decide_an_artifact(root, tmp_path):
    wo_id = _order(root)
    command = {"command": "jarvis artifact keep art-1234"}
    result = _pre(root, wo_id, tool="Bash", tool_input=command)
    assert _decision(result) == "deny"
    assert _reason(result) == "Only the user decides an artifact."

    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    assert _decision(_pre(root, None, tool="Bash", tool_input=command, session_id="mine",
                          cwd=elsewhere, env={})) != "deny"


# --- cap (§3.4)

def test_cap_denies_the_next_new_owner(jarvis_home, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "artifact_session_cap", lambda: 2)
    a, b = _project(tmp_path, "proj_a"), _project(tmp_path, "proj_b")
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(a))
        central.upsert_project("proj_b", str(b))
    finally:
        central.close()
    held = []
    for root in (a, b):
        wo_id = _order(root, session_id=f"s-{root.name}")
        _post(root, wo_id, session_id=f"s-{root.name}")
        held.append((root, wo_id))
    newcomer = _order(a, session_id="s-new")

    result = _pre(a, newcomer, session_id="s-new")
    assert _decision(result) == "deny"
    assert "2" in _reason(result) and "jarvis artifact list" in _reason(result)
    # An order already holding one republishes freely
    root, wo_id = held[0]
    assert _decision(_pre(root, wo_id, session_id=f"s-{root.name}")) is None


def test_catalog_parses_the_cap():
    base = {"os": {}, "projects": []}
    assert parse_catalog(base).os.artifacts.max_held_sessions == 10
    base["os"]["artifacts"] = {"max_held_sessions": 3}
    assert parse_catalog(base).os.artifacts.max_held_sessions == 3


# --- CLI (§3.5)

def test_cli_list_and_show(root, capsys):
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(root))
    finally:
        central.close()
    wo_id = _order(root)
    _post(root, wo_id, tool_input={"file_path": "r.html", "title": "Weekly report"})
    art_id = _rows(root, wo_id)[0]["id"]

    assert cli.main(["artifact", "list"]) == 0
    out = capsys.readouterr().out
    assert art_id in out and wo_id in out and URL in out

    assert cli.main(["artifact", "show", art_id, "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == art_id and shown["url"] == URL and shown["wo_id"] == wo_id
