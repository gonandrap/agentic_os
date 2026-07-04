"""Jarvis behavioral scorecard (deterministic).

What Jarvis PROMISES: `jarvis status` tells the exact truth about what needs the
user (no false alarms, no misses), work routes to the right project with the right
metadata, feedback reaches the right worker, and the safety rails (dependencies,
reviews, drift, dead sessions) actually rail.
"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore

scenario = pytest.mark.scenario


@pytest.fixture()
def fleet(jarvis_home, fake_claude, tmp_path, claude_json, make_two_projects):
    """Two-project fleet, started, with a tickable daemon."""
    return make_two_projects


@pytest.fixture()
def make_two_projects(jarvis_home, fake_claude, tmp_path, claude_json):
    from jarvis.testing import make_git_project
    pa = make_git_project(tmp_path, "proj_a")
    pb = make_git_project(tmp_path, "proj_b")
    claude_json(pa), claude_json(pb)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"}},
        "projects": [
            {"name": "proj_a", "path": str(pa)},
            {"name": "proj_b", "path": str(pb), "max_concurrent": 1,
             "worker": {"model": "haiku", "permission_mode": "bypassPermissions"}},
        ],
    }))
    ops.start_os(str(catalog), foreground=True)
    daemon = Daemon(load_catalog(catalog))
    return {"a": pa, "b": pb, "daemon": daemon, "catalog": catalog}


def bind(project_path, wo_id):
    sess = [s for s in claude_cli.list_background_sessions()
            if s.name.startswith(f"[WO {wo_id}]")]
    handle_hook(
        {"hook_event_name": "SessionStart", "session_id": sess[0].session_id,
         "cwd": str(project_path)},
        {"JARVIS_WO_ID": wo_id, "JARVIS_PROJECT_PATH": str(project_path)},
    )
    return sess[0]


# -- 1. status truthfulness: flag exactly what needs the user -----------------------

@scenario("jarvis/status-truth", "quiet fleet reports healthy with zero attention")
def test_quiet_is_quiet(fleet):
    st = ops.os_status()
    assert st["attention"] == []
    assert st["daemon"]["running"] is False  # foreground start — no daemon process
    assert {p["name"] for p in st["projects"]} == {"proj_a", "proj_b"}


@scenario("jarvis/status-truth", "a blocked worker is an attention item")
def test_blocked_worker_flagged(fleet):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    sess = bind(fleet["a"], wo["id"])
    handle_hook({"hook_event_name": "Notification", "session_id": sess.session_id,
                 "cwd": str(fleet["a"]), "message": "needs permission"},
                {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT_PATH": str(fleet["a"])})
    st = ops.os_status()
    assert any(a["wo_id"] == wo["id"] for a in st["attention"])
    assert not st["healthy"]


@scenario("jarvis/status-truth", "pending assumptions flag needs_review after finish")
def test_assumptions_flag_review(fleet):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    ops.assume(wo["id"], "picked postgres over sqlite")
    ops.finish(wo["id"], "done, one assumption")
    st = ops.os_status()
    item = [a for a in st["attention"] if a["wo_id"] == wo["id"]]
    assert item and item[0]["status"] == "needs_review"


@scenario("jarvis/status-truth", "accepting the review clears the attention")
def test_review_clears(fleet):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    ops.assume(wo["id"], "assumed X")
    ops.finish(wo["id"], "done")
    ops.review_work_order(wo["id"], accept=True)
    st = ops.os_status()
    assert not any(a["wo_id"] == wo["id"] for a in st["attention"])
    store = ProjectStore(fleet["a"])
    try:
        assert store.get_work_order(wo["id"])["status"] == "completed"
    finally:
        store.close()


@scenario("jarvis/status-truth", "manually edited injected settings surface as drift")
def test_settings_drift_flagged(fleet):
    sp = fleet["a"] / ".claude" / "settings.json"
    data = json.loads(sp.read_text())
    data["permissions"]["allow"].append("Bash(rm -rf *)")  # tampering
    sp.write_text(json.dumps(data))
    st = ops.os_status()
    assert any(a["title"] == "settings drift" and a["project"] == "proj_a"
               for a in st["attention"])


@scenario("jarvis/status-truth", "a vanished worker session becomes failed + attention")
def test_dead_session_flagged(fleet, fake_claude, monkeypatch):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    sess = bind(fleet["a"], wo["id"])
    claude_cli.stop_session(sess.id)  # session disappears from the roster
    store = ProjectStore(fleet["a"])
    try:  # age the wo past the 120s grace period
        store.conn.execute("UPDATE work_orders SET updated_at = updated_at - 999 WHERE id=?",
                           (wo["id"],))
    finally:
        store.close()
    d.tick_count = 0
    d.tick()  # reconcile tick
    st = ops.os_status()
    assert any(a["wo_id"] == wo["id"] and "disappeared" in a["reason"]
               for a in st["attention"])


# -- 2. routing: right project, right metadata, right limits -----------------------

@scenario("jarvis/routing", "work order dispatches in ITS project with per-project defaults")
def test_project_isolation_and_defaults(fleet, fake_claude):
    d = fleet["daemon"]
    ops.create_work_order("proj_b", "b-task")
    d.tick()
    bg = [c for c in fake_claude.calls if "--bg" in c["argv"]]
    assert len(bg) == 1
    argv = bg[0]["argv"]
    assert bg[0]["cwd"] == str(fleet["b"])
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    store = ProjectStore(fleet["a"])
    try:
        assert store.list_work_orders() == []  # nothing leaked into proj_a
    finally:
        store.close()


@scenario("jarvis/routing", "per-WO metadata overrides project defaults")
def test_wo_overrides(fleet, fake_claude):
    d = fleet["daemon"]
    ops.create_work_order("proj_a", "special", model="opus", permission_mode="plan")
    d.tick()
    argv = [c for c in fake_claude.calls if "--bg" in c["argv"]][0]["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--permission-mode") + 1] == "plan"


@scenario("jarvis/routing", "concurrency caps hold per project and release on completion")
def test_concurrency_respected(fleet, fake_claude):
    d = fleet["daemon"]
    for i in range(3):
        ops.create_work_order("proj_b", f"t{i}")  # max_concurrent=1
    d.tick()
    assert len([c for c in fake_claude.calls if "--bg" in c["argv"]]) == 1
    store = ProjectStore(fleet["b"])
    try:
        running = store.list_work_orders(statuses=("running",))
        ops.finish(running[0]["id"], "done")
    finally:
        store.close()
    d.tick()
    assert len([c for c in fake_claude.calls if "--bg" in c["argv"]]) == 2


@scenario("jarvis/routing", "the worker prompt carries the full contract + knowledge")
def test_prompt_contract(fleet, fake_claude):
    central = CentralStore()
    try:
        central.add_knowledge("never bump major versions", project="proj_a")
    finally:
        central.close()
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "upgrade deps", description="careful please")
    d.tick()
    prompt = [c for c in fake_claude.calls if "--bg" in c["argv"]][0]["argv"][-1]
    for must in ("upgrade deps", "careful please", f"jarvis wo assume {wo['id']}",
                 f"jarvis wo ask {wo['id']}", f"jarvis wo finish {wo['id']}",
                 "never bump major versions", "Never push to main"):
        assert must in prompt, f"contract element missing: {must}"


# -- 3. feedback routing --------------------------------------------------------------

@scenario("jarvis/feedback", "feedback waits while the worker runs, delivers when idle")
def test_feedback_waits_then_delivers(fleet, fake_claude):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    sess = bind(fleet["a"], wo["id"])
    ops.send_message(wo["id"], "use the staging bucket")
    d.tick_count = 0
    d.tick()
    store = ProjectStore(fleet["a"])
    try:
        assert store.queued_messages(wo["id"]), "must NOT deliver mid-turn"
    finally:
        store.close()
    fake_claude.set_session_state(sess.session_id, "done")
    d.tick_count = 0
    d.tick()
    d.delivery_pool.shutdown(wait=True)
    store = ProjectStore(fleet["a"])
    try:
        assert not store.queued_messages(wo["id"])
        resumes = [c for c in fake_claude.calls
                   if "--bg" in c["argv"] and "--resume" in c["argv"]]
        assert resumes and resumes[0]["argv"][
            resumes[0]["argv"].index("--resume") + 1] == sess.session_id
        # the worker's reply came back into the record
        msgs = store.list_messages(wo["id"])
        assert any(m["direction"] == "agent_to_user" for m in msgs)
    finally:
        store.close()


@scenario("jarvis/feedback", "feedback to an unknown work order fails loudly")
def test_feedback_unknown_wo(fleet):
    with pytest.raises(ops.OpsError):
        ops.send_message("wo-nonexistent", "hello?")


# -- 4. safety rails -------------------------------------------------------------------

@scenario("jarvis/safety-rails", "backlog promotion blocks on unfinished dependencies")
def test_backlog_dependency_rail(fleet):
    central = CentralStore()
    try:
        dep = central.add_backlog("proj_a", "foundation")
        item = central.add_backlog("proj_a", "tower", depends_on=[dep["id"]])
    finally:
        central.close()
    with pytest.raises(ops.OpsError, match="unfinished dependencies"):
        ops.promote_backlog(item["id"])
    forced = ops.promote_backlog(item["id"], force=True)
    assert forced["forced_over_blockers"] == [dep["id"]]


@scenario("jarvis/safety-rails", "completing a promoted work order closes its backlog item")
def test_backlog_closure(fleet):
    d = fleet["daemon"]
    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "todo thing")
    finally:
        central.close()
    wo_id = ops.promote_backlog(item["id"])["wo_id"]
    d.tick()
    ops.finish(wo_id, "shipped")
    central = CentralStore()
    try:
        assert central.get_backlog(item["id"])["status"] == "done"
    finally:
        central.close()


@scenario("jarvis/safety-rails", "unmanaged bg sessions are adopted and badged ad-hoc")
def test_adhoc_visibility(fleet, fake_claude):
    d = fleet["daemon"]
    claude_cli.spawn_background(prompt="rogue", cwd=fleet["a"], name="side quest")
    d.tick_count = 0
    d.tick()
    store = ProjectStore(fleet["a"])
    try:
        adhoc = [w for w in store.list_work_orders() if w["origin"] == "adhoc"]
        assert len(adhoc) == 1 and adhoc[0]["title"] == "side quest"
    finally:
        store.close()


@scenario("jarvis/safety-rails", "rejected review keeps the work order on the user's plate")
def test_reject_keeps_attention(fleet):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    ops.assume(wo["id"], "assumed the wrong thing")
    ops.finish(wo["id"], "done")
    ops.review_work_order(wo["id"], accept=False)
    st = ops.os_status()
    assert any(a["wo_id"] == wo["id"] for a in st["attention"])


@scenario("jarvis/safety-rails", "worker settings scope edits to the worktree only")
def test_worker_permissions_scoped(fleet, fake_claude):
    d = fleet["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    d.tick()
    argv = [c for c in fake_claude.calls if "--bg" in c["argv"]][0]["argv"]
    settings = json.loads(open(argv[argv.index("--settings") + 1]).read())
    allow = settings["permissions"]["allow"]
    wt = f"{str(fleet['a']).lstrip('/')}/.claude/worktrees/{wo['id']}"
    assert f"Edit(//{wt}/**)" in allow and f"Write(//{wt}/**)" in allow
    assert not any(r.startswith("Edit(") and wt not in r for r in allow), \
        "no edit rule may reach outside the worktree"
