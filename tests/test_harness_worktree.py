"""A worker whose session a harness opened lives in the HARNESS's worktree (docs/superpowers/specs/2026-10-22-session-dispatch.md)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import claude_cli, hooks
from jarvis.catalog import load_catalog
from jarvis.dispatch import _write_worker_settings

OPEN = ["claude-jarvis", "start", "@jarvis", "--prompt", "{prompt}", "{wo_name}"]


@pytest.fixture()
def layout(tmp_path, jarvis_home):
    from jarvis.project_store import ProjectStore

    import subprocess

    from jarvis.testing import make_git_project

    root = make_git_project(tmp_path, "proj")
    (root / ".jarvis").mkdir()
    (root / "CLAUDE.md").write_text("root rules")
    sibling = tmp_path / "proj.worktrees" / "wocrew01"
    subprocess.run(["git", "worktree", "add", "-q", "--detach", str(sibling)], cwd=root,
                   check=True)
    (sibling / "src").mkdir()
    (sibling / "CLAUDE.md").write_text("branch rules")
    store = ProjectStore(root)
    try:
        store.create_work_order("t", wo_id="wo-crew01")
        store.update_work_order("wo-crew01", worktree=str(sibling))
    finally:
        store.close()
    return root, sibling


def _env(root: Path, **over) -> dict:
    return {"JARVIS_WO_ID": "wo-crew01", "JARVIS_WO_KIND": "worker",
            "JARVIS_PROJECT_PATH": str(root), hooks.HARNESS_WORKTREE_ENV: "1",
            claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_BACKGROUND, **over}


def _write(cwd: Path, path: str) -> dict:
    return {"tool_name": "Write", "cwd": str(cwd),
            "tool_input": {"file_path": str(cwd / path), "content": "x = 1"}}


# -- one predicate for "this cwd is the worker's own worktree"


def test_jarvis_own_layout_is_a_worker_worktree(tmp_path):
    wt = tmp_path / "proj" / ".claude" / "worktrees" / "wo-1"
    assert hooks.in_worker_worktree(str(wt), {})


def test_a_harness_sibling_is_a_worker_worktree(layout):
    root, sibling = layout
    assert hooks.in_worker_worktree(str(sibling), _env(root))


def test_a_subdirectory_of_the_recorded_worktree_is_still_it(layout):
    root, sibling = layout
    assert hooks.in_worker_worktree(str(sibling / "src"), _env(root))


def test_any_other_directory_outside_the_root_is_not_a_worker_worktree(layout, tmp_path):
    root, _ = layout
    elsewhere = tmp_path / "home-ish"
    elsewhere.mkdir()
    assert not hooks.in_worker_worktree(str(elsewhere), _env(root))


def test_no_recorded_worktree_yet_fails_closed(layout):
    from jarvis.project_store import ProjectStore

    root, sibling = layout
    store = ProjectStore(root)
    try:
        store.update_work_order("wo-crew01", worktree="wo-crew01")
    finally:
        store.close()
    assert not hooks.in_worker_worktree(str(sibling), _env(root))


def test_the_project_root_is_never_a_harness_worktree(layout):
    root, _ = layout
    assert not hooks.in_worker_worktree(str(root / "src"), _env(root))


def test_a_sibling_without_the_harness_flag_is_not_a_worker_worktree(layout):
    root, sibling = layout
    env = _env(root)
    del env[hooks.HARNESS_WORKTREE_ENV]
    assert not hooks.in_worker_worktree(str(sibling), env)


def test_a_sibling_without_an_order_is_not_a_worker_worktree(layout):
    root, sibling = layout
    env = _env(root)
    del env["JARVIS_WO_ID"]
    assert not hooks.in_worker_worktree(str(sibling), env)


# -- the hooks that used to key on `/.claude/worktrees/`


def test_the_crew_rule_reaches_a_harness_worktree(layout):
    root, sibling = layout
    result = hooks.crew_edit_decision(_write(sibling, "src/app.py"),
                                      _env(root, JARVIS_REQUIRE_CREW="1"))
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_worktree_auto_allow_reaches_a_harness_worktree(layout):
    root, sibling = layout
    result = hooks.preflight_decision(_write(sibling, "src/app.py"), _env(root))
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_the_auto_allow_stops_at_the_harness_worktree(layout):
    root, sibling = layout
    payload = _write(sibling, "src/app.py")
    payload["tool_input"]["file_path"] = str(root / "src" / "app.py")
    assert hooks.preflight_decision(payload, _env(root)) is None


def test_py_nav_finds_the_project_from_the_env_in_a_harness_worktree(layout):
    root, sibling = layout
    (root / ".serena").mkdir()
    (root / ".serena" / "project.yml").write_text("name: proj\n")
    payload = {"tool_name": "Read", "cwd": str(sibling),
               "tool_input": {"file_path": str(sibling / "src" / "app.py")}}
    assert hooks.py_nav_decision(payload, _env(root, JARVIS_PY_NAV_HOOK="on")) is not None


# -- the worker settings, which used to name `.claude/worktrees/<wo-id>`


def _project(tmp_path, root: Path, launcher: dict | None):
    worker = {"transport": "background"}
    if launcher:
        worker["launcher"] = launcher
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"projects": [
        {"name": "proj", "path": str(root), "worker": worker}]}))
    return load_catalog(path).projects[0]


def _settings(project, wo) -> dict:
    return json.loads(_write_worker_settings(project, wo).read_text())


def test_a_harness_order_is_flagged_and_names_no_jarvis_worktree(tmp_path, layout):
    root, _ = layout
    project = _project(tmp_path, root, {"open": OPEN})
    settings = _settings(project, {"id": "wo-crew01", "kind": "worker", "worktree": "wo-crew01"})
    assert settings["env"][hooks.HARNESS_WORKTREE_ENV] == "1"
    assert not any(".claude/worktrees" in rule for rule in settings["permissions"]["allow"])
    assert not any(".claude/worktrees" in str(p) for p in settings.get("claudeMdExcludes", []))


def test_a_harness_order_with_its_worktree_known_names_that_worktree(tmp_path, layout):
    root, sibling = layout
    project = _project(tmp_path, root, {"open": OPEN})
    settings = _settings(project, {"id": "wo-crew01", "kind": "worker",
                                   "worktree": str(sibling)})
    rel = str(sibling).lstrip("/")
    assert f"Edit(//{rel}/**)" in settings["permissions"]["allow"]
    # A sibling is not under the project root, so the root's CLAUDE.md never loads twice
    assert str(root / "CLAUDE.md") not in settings.get("claudeMdExcludes", [])


def test_a_plain_order_keeps_jarvis_layout(tmp_path, layout):
    root, _ = layout
    project = _project(tmp_path, root, None)
    settings = _settings(project, {"id": "wo-crew01", "kind": "worker", "worktree": "wo-crew01"})
    assert hooks.HARNESS_WORKTREE_ENV not in settings["env"]
    rel = f"{str(root).lstrip('/')}/.claude/worktrees/wo-crew01"
    assert f"Edit(//{rel}/**)" in settings["permissions"]["allow"]


# -- dispatch records the worktree the harness chose, not the order id

from tests.test_session_dispatch import OPEN as FULL_OPEN, wrapper  # noqa: E402,F401


def test_dispatch_records_the_harness_worktree(jarvis_home, fake_claude, catalog_file,
                                               project, wrapper, monkeypatch):  # noqa: F811
    from jarvis import dispatch, ops, worker_session
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    data = json.loads(catalog_file.read_text())
    data["os"]["defaults"]["transport"] = "background"
    data["projects"][0].setdefault("worker", {})["launcher"] = {
        "open": [str(wrapper.path), *FULL_OPEN[1:]]}
    catalog_file.write_text(json.dumps(data))
    monkeypatch.setattr(worker_session, "BG_DEAD_GRACE", 0.0)
    ops.start_os(str(catalog_file), foreground=True)
    cat = load_catalog(catalog_file)
    ops.create_work_order("proj_a", "task")
    store, central = ProjectStore(project), CentralStore()
    try:
        wo = store.claim_next_pending()
        dispatch.dispatch_work_order(store, central, cat.projects[0], wo, os_config=cat.os)
        tree = Path(f"{project}.worktrees") / wo["id"].replace("-", "")
        [event] = [e for e in store.list_events(wo["id"]) if e["kind"] == "dispatched"]
        payload = event["payload"] if isinstance(event["payload"], dict) else json.loads(event["payload"])
        assert Path(payload["worktree"]).resolve() == tree.resolve()
        assert not (Path(project) / ".claude" / "worktrees" / wo["id"]).exists()
    finally:
        central.close()
        store.close()


# -- a recorded worktree is trusted only if it is a real, non-main worktree of the project


def _record(root: Path, worktree: str) -> None:
    from jarvis.project_store import ProjectStore

    store = ProjectStore(root)
    try:
        store.update_work_order("wo-crew01", worktree=worktree)
    finally:
        store.close()


def test_a_real_sibling_worktree_is_valid(layout):
    from jarvis import session_dispatch

    root, sibling = layout
    assert session_dispatch.harness_worktree_ok(root, sibling)


@pytest.mark.parametrize("pick", ["root", "ancestor", "plain", "missing", "relative"])
def test_anything_else_is_not_a_harness_worktree(layout, tmp_path, pick):
    from jarvis import session_dispatch

    root, _ = layout
    plain = tmp_path / "plain"
    plain.mkdir()
    path = {"root": root, "ancestor": tmp_path, "plain": plain,
            "missing": tmp_path / "gone", "relative": Path("proj.worktrees/wocrew01")}[pick]
    assert not session_dispatch.harness_worktree_ok(root, path)


def test_a_recorded_project_root_never_auto_allows(layout):
    root, _ = layout
    _record(root, str(root))
    assert not hooks.in_worker_worktree(str(root / "src"), _env(root))
    assert hooks.preflight_decision(_write(root, "src/app.py"), _env(root)) is None


def test_a_recorded_ancestor_never_auto_allows(layout, tmp_path):
    root, sibling = layout
    _record(root, str(tmp_path))
    assert not hooks.in_worker_worktree(str(sibling), _env(root))


def test_a_recorded_project_root_gets_no_edit_rules(tmp_path, layout):
    root, _ = layout
    project = _project(tmp_path, root, {"open": OPEN})
    settings = _settings(project, {"id": "wo-crew01", "kind": "worker", "worktree": str(root)})
    assert not any(r.startswith(("Edit(", "Write(", "NotebookEdit("))
                   for r in settings["permissions"]["allow"])


def test_an_open_that_reports_no_worktree_fails_the_dispatch(monkeypatch, tmp_path, layout):
    from jarvis import session_dispatch, worker_session
    from jarvis.project_store import ProjectStore

    root, _ = layout
    stopped = []
    monkeypatch.setattr(claude_cli, "spawn_bg_open", lambda argv, cwd, env=None:
                        claude_cli.BgEntry(id="abcd1234", session_id="abcd1234-x",
                                           pid=1, status="idle", state="", cwd=""))
    monkeypatch.setattr(claude_cli, "stop_session", lambda bg_id: stopped.append(bg_id) or True)
    lch = session_dispatch.parse_launcher({"open": OPEN}, "where")
    launch = session_dispatch.Launch(launcher=lch, project="proj", project_path=root,
                                     wo_id="wo-crew01", wo_kind="worker", phase="open",
                                     transport="background")
    store = ProjectStore(root)
    try:
        wo = store.get_work_order("wo-crew01")
        with pytest.raises(claude_cli.ClaudeCliError, match="worktree"):
            worker_session._launch_bg(store, wo, "go", root, False, None,
                                      tmp_path / "p.prompt", {}, launch)
        assert stopped == ["abcd1234"]
        assert store.get_work_order("wo-crew01")["worktree"] != str(root)
    finally:
        store.close()


# -- the settled-checkout sweep leaves a harness's worktree to the harness


def test_the_reclaim_sweep_never_removes_a_harness_worktree(layout):
    from jarvis import db, disk
    from jarvis.catalog import OsConfig, ProjectSpec
    from jarvis.project_store import ProjectStore

    root, sibling = layout
    (sibling / "CLAUDE.md").unlink()
    store = ProjectStore(root)
    try:
        store.set_status("wo-crew01", "completed")
        store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                           (db.now() - 48 * 3600, "wo-crew01"))
        store.conn.commit()
        cfg = OsConfig()
        cfg.worktree.reclaim_after_hours = 24
        reclaimed, kept = disk.sweep(store, ProjectSpec(name="proj", path=root,
                                                        worktree=cfg.worktree))
        assert sibling.exists()
        assert reclaimed == [] and kept == ["wo-crew01"]
    finally:
        store.close()
