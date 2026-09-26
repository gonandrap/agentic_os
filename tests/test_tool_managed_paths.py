"""A file a TOOL rewrites must not reach a worker's commit.

Spec docs/superpowers/specs/2026-09-25-serena-config-churn-and-tool-managed-files.md.
`.serena/project.yml` is tracked and Serena regenerates it on activation, inside the
worker's worktree, where `git add -A` stages the churn into a pull request about
something else. The worktree's index is told the file is not the worker's to report.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from jarvis import catalog, config_version, dispatch, hooks, ops, timeline
from jarvis.catalog import CatalogError, OsConfig, parse_catalog
from jarvis.hooks import handle_hook
from jarvis.project_store import ProjectStore

SERENA = ".serena/project.yml"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True,
                         capture_output=True, text=True)
    return out.stdout


@pytest.fixture()
def wo(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return ops.create_work_order("proj_a", "make the thing work", origin="jarvis",
                                 description="The user's original ask, verbatim.")


def commit_everything(project: Path) -> None:
    git(project, "config", "user.email", "t@example.invalid")
    git(project, "config", "user.name", "Test")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "init")


def make_worktree(project: Path, wo_id: str, tracked=(SERENA,)) -> Path:
    """The worktree Claude Code's `--worktree` flag would have created, plus the
    tracked tool-managed files the checkout carries."""
    for rel in tracked:
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("committed: true\n")
    commit_everything(project)
    tree = project / ".claude" / "worktrees" / wo_id
    git(project, "worktree", "add", "-q", str(tree), "-b", wo_id)
    store = ProjectStore(project)
    try:
        store.update_work_order(wo_id, worktree=wo_id)
    finally:
        store.close()
    return tree


def env(project: Path, wo_id: str, paths=(SERENA,)) -> dict[str, str]:
    return {
        "JARVIS_WO_ID": wo_id,
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
        hooks.TOOL_MANAGED_PATHS_ENV: json.dumps(list(paths)),
    }


def session_start(cwd: Path) -> dict[str, object]:
    return {"hook_event_name": "SessionStart", "session_id": "sess-1",
            "source": "startup", "cwd": str(cwd)}


def flags(tree: Path, rel: str) -> str:
    """The index flag letter `git ls-files -v` reports — `S` is skip-worktree."""
    out = git(tree, "ls-files", "-v", "--", rel).strip()
    return out.split(" ", 1)[0] if out else ""


def events(project: Path, wo_id: str, kind="tool_managed_paths") -> list[dict]:
    store = ProjectStore(project)
    try:
        return [json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
                for e in store.list_events(wo_id) if e["kind"] == kind]
    finally:
        store.close()


# -- 1. the worktree's index stops reporting the file ------------------------------------


def test_marks_each_configured_path_in_the_worktree(wo, project):
    tree = make_worktree(project, wo["id"], tracked=(SERENA, "tool/other.yml"))

    handle_hook(session_start(tree), env(project, wo["id"],
                                        paths=(SERENA, "tool/other.yml")))

    assert flags(tree, SERENA) == "S"
    assert flags(tree, "tool/other.yml") == "S"
    assert events(project, wo["id"])[0]["marked"] == [SERENA, "tool/other.yml"]


def test_the_rewritten_file_is_invisible_to_status_and_add(wo, project):
    """The point of the flag: the tool keeps rewriting, git stops staging."""
    tree = make_worktree(project, wo["id"])
    handle_hook(session_start(tree), env(project, wo["id"]))

    (tree / SERENA).write_text("regenerated: by the installed release\n")
    git(tree, "add", "-A")

    assert git(tree, "status", "--short").strip() == ""
    assert git(tree, "diff", "--cached", "--name-only").strip() == ""


def test_marking_twice_is_a_no_op_and_records_nothing_the_second_time(wo, project):
    """`SessionStart` fires once per TURN; a row per turn would make the record a
    hook log (timeline.py:19-21)."""
    tree = make_worktree(project, wo["id"])
    handle_hook(session_start(tree), env(project, wo["id"]))
    handle_hook(session_start(tree), env(project, wo["id"]))

    assert len(events(project, wo["id"])) == 1
    assert flags(tree, SERENA) == "S"


# -- 2. anything that is not this work order's worktree ----------------------------------


def test_does_nothing_in_the_shared_checkout(wo, project):
    """The dev checkout's own churn is the user's to see and commit."""
    make_worktree(project, wo["id"])

    handle_hook(session_start(project), env(project, wo["id"]))

    assert flags(project, SERENA) == "H"
    assert events(project, wo["id"]) == []


def test_does_nothing_under_another_work_orders_worktree(wo, project):
    tree = make_worktree(project, wo["id"])
    other = project / ".claude" / "worktrees" / "wo-99999999"
    git(project, "worktree", "add", "-q", str(other), "-b", "wo-99999999")

    handle_hook(session_start(other), env(project, wo["id"]))

    assert flags(other, SERENA) == "H"
    assert flags(tree, SERENA) == "H"
    assert events(project, wo["id"]) == []


# -- 3. untracked, absent, and never fatal ----------------------------------------------


def test_untracked_and_absent_paths_are_skipped_and_the_hook_still_answers(wo, project):
    tree = make_worktree(project, wo["id"])
    (tree / SERENA).unlink()  # tracked, gone from the worktree

    result = handle_hook(session_start(tree),
                         env(project, wo["id"], paths=(SERENA, "tool/untracked.yml")))

    assert result["hookSpecificOutput"]["additionalContext"], "the hook's own answer"
    assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    payload = events(project, wo["id"])[0]
    assert payload["skipped"] == {SERENA: "absent", "tool/untracked.yml": "untracked"}
    assert payload["marked"] == []


def test_a_broken_git_never_breaks_the_session(wo, project, monkeypatch):
    tree = make_worktree(project, wo["id"])

    def boom(*a, **k):
        raise OSError("no git on this box")

    monkeypatch.setattr(subprocess, "run", boom)

    result = handle_hook(session_start(tree), env(project, wo["id"]))

    assert result["hookSpecificOutput"]["additionalContext"]


def test_no_env_means_the_mechanism_is_off(wo, project):
    """A work order dispatched before this landed carries no such env."""
    tree = make_worktree(project, wo["id"])
    bare = env(project, wo["id"])
    bare.pop(hooks.TOOL_MANAGED_PATHS_ENV)

    assert handle_hook(session_start(tree), bare) is not None
    assert flags(tree, SERENA) == "H"
    assert events(project, wo["id"]) == []


def test_unparseable_env_is_off_and_silent(wo, project):
    tree = make_worktree(project, wo["id"])
    bad = {**env(project, wo["id"]), hooks.TOOL_MANAGED_PATHS_ENV: "not json"}

    assert handle_hook(session_start(tree), bad) is not None
    assert flags(tree, SERENA) == "H"
    assert events(project, wo["id"]) == []


# -- 4. the list is a setting -----------------------------------------------------------


def test_a_project_override_reaches_the_worker_and_the_hook(wo, project, tmp_path):
    cat = parse_catalog({
        "os": {},
        "projects": [{"name": "proj_a", "path": str(project),
                      "worktree": {"tool_managed_paths": ["tool/other.yml"]}}],
    })
    spec = cat.project("proj_a")
    assert spec.worktree.tool_managed_paths == ("tool/other.yml",)

    settings = json.loads(dispatch._write_worker_settings(spec, wo).read_text())
    written = settings["env"][hooks.TOOL_MANAGED_PATHS_ENV]
    assert json.loads(written) == ["tool/other.yml"]

    tree = make_worktree(project, wo["id"], tracked=(SERENA, "tool/other.yml"))
    handle_hook(session_start(tree),
                {"JARVIS_WO_ID": wo["id"], "JARVIS_PROJECT": "proj_a",
                 "JARVIS_PROJECT_PATH": str(project),
                 hooks.TOOL_MANAGED_PATHS_ENV: written})

    assert flags(tree, "tool/other.yml") == "S"
    assert flags(tree, SERENA) == "H", "the fleet default was REPLACED, not merged"


def test_the_fleet_default_is_the_serena_config(project):
    assert OsConfig().worktree.tool_managed_paths == (SERENA,)
    assert catalog.DEFAULT_TOOL_MANAGED_PATHS == (SERENA,)

    resolved = config_version.resolve(
        parse_catalog({"os": {}, "projects": [{"name": "proj_a", "path": str(project)}]}))
    assert resolved["os.worktree.tool_managed_paths"] == [SERENA]
    assert resolved["projects.proj_a.worktree.tool_managed_paths"] == [SERENA]


def test_the_default_dispatch_env_carries_it(wo, project, catalog_file):
    spec = catalog.load_catalog(catalog_file).project("proj_a")

    settings = json.loads(dispatch._write_worker_settings(spec, wo).read_text())

    assert json.loads(settings["env"][hooks.TOOL_MANAGED_PATHS_ENV]) == [SERENA]


@pytest.mark.parametrize("value, message", [
    (".serena/project.yml", "must be a list of paths"),
    (["/etc/passwd"], "must be repo-relative"),
    (["../../etc/passwd"], "must not contain"),
])
def test_a_path_that_could_escape_the_worktree_is_refused(project, value, message):
    """The value is handed to `git -C <worktree>`."""
    with pytest.raises(CatalogError) as e:
        parse_catalog({"os": {"worktree": {"tool_managed_paths": value}}, "projects": []})
    assert message in str(e.value)


def test_the_same_refusals_apply_to_a_project_override(project):
    with pytest.raises(CatalogError) as e:
        parse_catalog({"os": {}, "projects": [
            {"name": "proj_a", "path": str(project),
             "worktree": {"tool_managed_paths": ".serena/project.yml"}}]})
    assert "projects[0] (proj_a).worktree.tool_managed_paths" in str(e.value)


# -- 5. it is circuitry, not the user's story -------------------------------------------


def test_the_event_is_debug_only():
    assert "tool_managed_paths" in timeline.DEBUG_KINDS
