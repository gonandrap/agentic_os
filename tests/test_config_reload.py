"""A config change reaching a RUNNING fleet: `Daemon.reload_catalog` and the drift check (docs/superpowers/specs/2026-08-27-the-config-console.md §4)"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from jarvis import config_version as cv, invariants, ops
from jarvis.catalog import load_catalog, parse_catalog
from jarvis.central_store import ARCHIVED, CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """`ops.set_config` refuses a worker session, and the suite is routinely run by one (kn-650b6f24)"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def write_catalog(path: Path, project: Path, *, model: str = "sonnet",
                  projects: list[str] = ["proj_a"], **os_keys) -> dict:
    document = {
        "os": {"defaults": {"model": model}, "notifications": {"sinks": ["log"]},
               **os_keys},
        "projects": [{"name": name, "path": str(project), "description": "test"}
                     for name in projects],
    }
    path.write_text(json.dumps(document, indent=2))
    return document


@pytest.fixture()
def catalog_file(tmp_path) -> Path:
    project = tmp_path / "proj_a"
    project.mkdir()
    path = tmp_path / "catalog.json"
    write_catalog(path, project)
    return path


@pytest.fixture()
def daemon(catalog_file, jarvis_home) -> Daemon:
    return Daemon(load_catalog(catalog_file))


def edit(path: Path, **os_keys) -> None:
    """Rewrite the catalog's `os` block, moving the mtime past the daemon's stamp"""
    document = json.loads(path.read_text())
    document["os"].update(os_keys)
    _rewrite(path, document)


def _rewrite(path: Path, document: dict) -> None:
    # clock-exempt: class 4 — forges a future mtime for the real stat() the daemon's catalog reload makes
    time.sleep(0.01)
    path.write_text(json.dumps(document, indent=2))
    os.utime(path, (time.time() + 1, time.time() + 1))


def inbox() -> list[dict]:
    central = CentralStore()
    try:
        return [i for i in central.unacked_inbox() if i["project"] == "os"]
    finally:
        central.close()


# -- the guard: an unchanged file replaces nothing


def test_a_first_tick_over_an_untouched_file_reloads_nothing(daemon):
    """The baseline is seeded in `__init__`, not on the first tick"""
    daemon.catalog.os.neo.enabled = False
    daemon.catalog.os.default_model = "haiku"

    assert daemon.reload_catalog() is False
    daemon.tick()

    assert daemon.catalog.os.neo.enabled is False
    assert daemon.catalog.os.default_model == "haiku"


def test_a_file_touched_but_not_changed_replaces_nothing(daemon, catalog_file):
    """The hash is the second guard, and this is the case it exists for: an editor save"""
    daemon.catalog.os.neo.enabled = False
    _rewrite(catalog_file, json.loads(catalog_file.read_text()))

    assert daemon.reload_catalog() is False
    assert daemon.catalog.os.neo.enabled is False


def test_a_catalog_with_no_source_path_is_a_no_op(jarvis_home, tmp_path):
    """A `Catalog` parsed in memory has no file to watch, and must not become one"""
    project = tmp_path / "proj_a"
    project.mkdir()
    path = tmp_path / "catalog.json"
    document = write_catalog(path, project)
    daemon = Daemon(parse_catalog(document))

    assert daemon.catalog.source_path is None
    assert daemon.reload_catalog() is False
    daemon.tick()
    assert inbox() == []


# -- the reload itself


def test_a_settings_change_reaches_the_daemon_on_the_next_tick(daemon, catalog_file):
    edit(catalog_file, defaults={"model": "opus"}, validation={"enabled": True})

    daemon.tick()

    assert daemon.catalog.os.default_model == "opus"
    assert daemon.catalog.os.validation.enabled is True
    assert inbox() == []


def test_the_catalog_is_stable_for_one_whole_tick(daemon, catalog_file):
    """`self.catalog` is replaced at the top of the tick and nowhere else: a pool thread holding a config..."""
    seen: list[str] = []

    def during_the_tick(project, store):
        edit(catalog_file, defaults={"model": "opus"})
        seen.append(daemon.catalog.os.default_model)

    daemon.route_outbox = during_the_tick  # type: ignore[method-assign]
    daemon.tick()

    assert seen == ["sonnet"], "the mid-tick edit was visible inside the same tick"
    daemon.tick()
    assert daemon.catalog.os.default_model == "opus"


# -- what is refused, and how loudly


def test_a_broken_catalog_keeps_the_last_good_one_and_warns_exactly_once(
        daemon, catalog_file):
    """One bad file must not take down every project in the fleet"""
    # clock-exempt: class 4 — the kernel's own mtime on the broken catalog must outrun the daemon's stamp
    time.sleep(0.01)
    catalog_file.write_text("{ this is not json")

    for _ in range(4):
        daemon.tick()

    assert daemon.catalog.os.default_model == "sonnet"
    items = inbox()
    assert len(items) == 1
    assert "NOT applied" in items[0]["title"]
    assert str(catalog_file) in items[0]["body"]


def test_a_repaired_catalog_reloads_and_re_arms_the_warning(daemon, catalog_file):
    # clock-exempt: class 4 — os.utime forges the repaired catalog's mtime for a real stat()
    time.sleep(0.01)
    catalog_file.write_text("{ this is not json")
    daemon.tick()
    assert len(inbox()) == 1

    write_catalog(catalog_file, catalog_file.parent / "proj_a", model="opus")
    os.utime(catalog_file, (time.time() + 1, time.time() + 1))
    daemon.tick()

    assert daemon.catalog.os.default_model == "opus"
    assert daemon.catalog_reload_warned is False


def test_an_added_project_is_refused_and_the_item_says_jarvis_start(
        daemon, catalog_file, tmp_path):
    """Settings only"""
    document = json.loads(catalog_file.read_text())
    document["projects"].append({"name": "proj_b", "path": str(tmp_path / "proj_a"),
                                 "description": "new"})
    document["os"]["defaults"]["model"] = "opus"
    _rewrite(catalog_file, document)

    daemon.tick()

    assert [p.name for p in daemon.catalog.projects] == ["proj_a"]
    # The settings in the same file are refused with it: a half-applied catalog is a configuration nobody...
    assert daemon.catalog.os.default_model == "sonnet"
    items = inbox()
    assert len(items) == 1
    assert "jarvis start" in items[0]["body"]


def test_a_removed_project_is_refused_too(jarvis_home, tmp_path):
    path = tmp_path / "catalog.json"
    project = tmp_path / "proj_a"
    project.mkdir()
    write_catalog(path, project, projects=["proj_a", "proj_b"])
    # Registered, as `start_os` leaves every project a live daemon runs (offboard spec §6.1)
    register("proj_a", "proj_b", project=project)
    daemon = Daemon(load_catalog(path))

    write_catalog(path, project, projects=["proj_a"])
    # clock-exempt: class 4 — os.utime forges the mtime that makes the removed-project catalog look newer
    os.utime(path, (time.time() + 1, time.time() + 1))
    daemon.tick()

    assert [p.name for p in daemon.catalog.projects] == ["proj_a", "proj_b"]
    assert "jarvis start" in inbox()[0]["body"]


def register(*names: str, project: Path, archived: tuple[str, ...] = ()) -> None:
    central = CentralStore()
    try:
        for name in names:
            central.upsert_project(name, str(project))
        for name in archived:
            central.set_project_status(name, ARCHIVED)
    finally:
        central.close()


# -- offboarding: a project leaves a running fleet (docs/superpowers/specs/2026-10-28-offboard-a-project.md §6.1)


@pytest.mark.parametrize("purged", [False, True], ids=["archived", "purged"])
def test_an_offboarded_project_leaves_with_the_settings_in_the_same_write(
        jarvis_home, tmp_path, purged):
    """Built while both are active; archive or purge, then drop it AND change a setting in ONE write"""
    path = tmp_path / "catalog.json"
    project = tmp_path / "proj_a"
    project.mkdir()
    document = write_catalog(path, project, projects=["proj_a", "proj_b"])
    # No store verb deletes a `projects` row, so "purged" is proj_b never having one
    register("proj_a", *(() if purged else ("proj_b",)), project=project)
    daemon = Daemon(load_catalog(path))
    daemon.tick()
    assert {p.name for p in daemon.catalog.projects} == {"proj_a", "proj_b"}
    assert "proj_b" in daemon.stores
    held = daemon.stores["proj_b"]

    if not purged:
        register(project=project, archived=("proj_b",))
    document["projects"] = [p for p in document["projects"] if p["name"] != "proj_b"]
    document["os"]["defaults"]["model"] = "opus"
    _rewrite(path, document)
    daemon.tick()

    assert [p.name for p in daemon.catalog.projects] == ["proj_a"]
    assert daemon.catalog.os.default_model == "opus"
    assert not [i for i in inbox() if i["title"] == "a catalog change was NOT applied"]
    assert "proj_b" not in daemon.stores
    with pytest.raises(sqlite3.ProgrammingError):
        held.conn.execute("SELECT 1")


def test_an_archived_project_is_evicted_before_the_catalog_changes(jarvis_home, tmp_path):
    """The registry alone evicts it, and its store closes; the file is the user's (§6.1, §6.2.4)"""
    path = tmp_path / "catalog.json"
    project = tmp_path / "proj_a"
    project.mkdir()
    write_catalog(path, project, projects=["proj_a", "proj_b"])
    register("proj_a", "proj_b", project=project)
    daemon = Daemon(load_catalog(path))
    daemon.tick()
    assert "proj_b" in daemon.stores

    register(project=project, archived=("proj_b",))
    daemon.tick()

    assert [p.name for p in daemon.catalog.projects] == ["proj_a"]
    assert "proj_b" not in daemon.stores
    assert not inbox()


# -- INV-CONFIG-DRIFT


@pytest.fixture()
def registered(catalog_file) -> Path:
    """A catalog registered and adopted, the way a started-and-configured OS leaves it"""
    central = CentralStore()
    central.set_state("catalog_path", str(catalog_file))
    central.close()
    ops.adopt_config(reason="the fleet's first")
    return catalog_file


def drift() -> list:
    return [v for v in invariants.check_os() if v.invariant == "INV-CONFIG-DRIFT"]


def test_no_drift_when_the_file_is_the_head_version(registered):
    assert drift() == []


def test_a_hand_edit_behind_the_records_back_is_drift(registered):
    on_disk = json.loads(registered.read_text())
    on_disk["os"]["defaults"]["model"] = "opus"
    _rewrite(registered, on_disk)

    found = drift()
    assert len(found) == 1
    v = found[0]
    assert v.repaired is False and v.repair == ""
    # Both hashes, because the user's next move is to decide which of them is right...
    assert cv.version_id(on_disk) in v.detail
    central = CentralStore()
    try:
        head = central.head_config_version()["id"]
    finally:
        central.close()
    assert head in v.detail
    # ...and both remedies, which are opposites: keep the file, or keep the record
    assert "jarvis config adopt" in v.detail
    assert f"jarvis config restore {head}" in v.detail


def test_a_config_set_leaves_no_drift(registered):
    """The write path records what it writes"""
    ops.set_config("os.defaults.model", "opus")
    assert drift() == []


def test_drift_is_silent_with_no_ledger_and_with_no_catalog(catalog_file):
    """A fleet that predates the console has nothing to be behind"""
    assert drift() == []
    central = CentralStore()
    central.set_state("catalog_path", str(catalog_file))
    central.close()
    assert drift() == []


def test_drift_is_a_doctor_check_only(registered):
    """`check_os()` has one caller, `ops.run_doctor`"""
    assert invariants.check_config_drift in invariants.OS_INVARIANTS
    assert invariants.check_config_drift not in invariants.INVARIANTS

    on_disk = json.loads(registered.read_text())
    on_disk["os"]["defaults"]["model"] = "opus"
    _rewrite(registered, on_disk)

    daemon = Daemon(load_catalog(registered))
    store = ProjectStore(Path(json.loads(registered.read_text())["projects"][0]["path"]))
    try:
        found = daemon.check_invariants(daemon.catalog.projects[0], store)
    finally:
        store.close()
    assert [v for v in (found or []) if v.invariant == "INV-CONFIG-DRIFT"] == []
    assert drift(), "the precondition: the file IS drifted"


# -- §6.1: the release rebase


def stale_head(catalog_file: Path, **resolved_overrides) -> dict:
    """A head version written under an OLDER build"""
    document = json.loads(catalog_file.read_text())
    resolved = cv.resolve(load_catalog(catalog_file))
    resolved.update(resolved_overrides)
    central = CentralStore()
    try:
        return central.add_config_version(
            document, resolved, actor="user", reason="before the upgrade",
            source_path=str(catalog_file), schema_version="0.0.1")
    finally:
        central.close()


def test_a_release_that_moves_a_default_writes_a_row_of_its_own(daemon, catalog_file):
    """Without it an upgrade is a behaviour change with no row"""
    from jarvis.bugreport import jarvis_version

    old = stale_head(catalog_file, **{"os.defaults.autocompact_window": 123456})

    row = daemon.rebase_config_for_release()

    assert row is not None
    assert row["actor"] == "release"
    assert row["id"] != old["id"], "the same document under a new build is a new row"
    assert row["document"] == old["document"], "the rebase must not rewrite the file"
    assert f"upgrade 0.0.1 → {jarvis_version()}" in row["reason"]
    assert "os.defaults.autocompact_window 123456 →" in row["reason"]
    central = CentralStore()
    try:
        assert central.head_config_version()["id"] == row["id"]
    finally:
        central.close()


def test_the_rebase_is_addressed_by_build_and_only_for_release_rows(daemon,
                                                                    catalog_file):
    """The salt is scoped to `actor="release"`: an ordinary edit that lands back on an old document must..."""
    document = json.loads(catalog_file.read_text())
    stale_head(catalog_file, **{"os.defaults.autocompact_window": 123456})
    row = daemon.rebase_config_for_release()

    assert row["id"] == cv.version_id(document, build=row["schema_version"])
    assert cv.version_id(document) != row["id"]
    # ...and re-running the same rebase is the same fact, so it writes nothing more
    assert daemon.rebase_config_for_release() is None


def test_an_upgrade_that_moves_nothing_writes_nothing(daemon, catalog_file):
    stale_head(catalog_file)
    assert daemon.rebase_config_for_release() is None
    central = CentralStore()
    try:
        assert len(central.config_versions()) == 1
    finally:
        central.close()


def test_a_rebased_head_is_neither_drift_nor_something_to_adopt(daemon, catalog_file):
    """The trap in option (a): every reader compares DOCUMENTS"""
    central = CentralStore()
    central.set_state("catalog_path", str(catalog_file))
    central.close()
    stale_head(catalog_file, **{"os.defaults.autocompact_window": 123456})
    daemon.rebase_config_for_release()

    assert drift() == []
    assert ops.adopt_config()["adopted"] is False
    assert ops.config_show()["drift"] is False


def test_a_head_this_build_cannot_parse_is_skipped(daemon, catalog_file):
    """A historical document is evidence"""
    central = CentralStore()
    try:
        central.add_config_version({"projects": [{"name": "x"}]}, {"os.x": 1},
                                   actor="user", schema_version="0.0.1")
    finally:
        central.close()

    assert daemon.rebase_config_for_release() is None


# -- §10.3: the one end-to-end assertion


def _git(cwd: Path, *args: str) -> None:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                   capture_output=True, text=True)


@pytest.fixture()
def started(tmp_path, jarvis_home, fake_claude, claude_json):
    """A booted OS over a real repository, validation OFF, and ONE daemon"""
    project = make_git_project(tmp_path, "proj_a")
    _git(project, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    claude_json(project)
    path = tmp_path / "catalog.json"
    write_catalog(path, project, validation={"enabled": False})
    ops.start_os(str(path), foreground=True)
    return Daemon(load_catalog(path)), project, path


def _run_one(daemon: Daemon, project: Path, title: str) -> dict:
    """Create, dispatch and commit a change — a work order ready to finish"""
    from jarvis import worker_session

    wo = ops.create_work_order("proj_a", title)
    daemon.tick_count = 0
    daemon.tick()
    store = ProjectStore(project)
    try:
        # clock-exempt: class 3 — spin-waits for a real worker subprocess's turn to leave running
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            list(worker_session.poll(store))
            turn = store.latest_turn(wo["id"])
            if turn is not None and turn["state"] != "running":
                break
            time.sleep(0.02)
    finally:
        store.close()
    tree = project / ".claude" / "worktrees" / wo["id"]
    if not tree.is_dir():
        _git(project, "worktree", "add", "-q", "-b", f"b-{wo['id']}", str(tree), "main")
    (tree / "app.py").write_text(f"print({title!r})\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", title)
    return wo


def test_turning_validation_on_reaches_a_running_fleet_without_a_restart(started):
    """§10.3, and the before/after pairing is what makes it falsifiable: "it validated" alone is satisfied..."""
    daemon, project, _ = started

    before = _run_one(daemon, project, "before")
    # `_run_one` COMMITS to the work order's branch (#232)
    assert ops.finish(before["id"], "done", evidence="ran the tests",
                      pr_url="https://github.com/x/y/pull/1")["status"] \
        == "waiting_pr_merge"
    store = ProjectStore(project)
    try:
        assert store.validation_rounds(wo_id=before["id"]) == []
    finally:
        store.close()

    ops.set_config("validation.enabled", True, project="proj_a", reason="trying it")
    daemon.tick_count = 0
    daemon.tick()
    # The reload is the thing under test, so assert the daemon's OWN copy moved
    assert daemon.catalog.project("proj_a").validation.enabled is True

    after = _run_one(daemon, project, "after")
    assert ops.finish(after["id"], "done", evidence="ran the tests",
                      pr_url="https://github.com/x/y/pull/2")["status"] == "validating"

    central = CentralStore()
    try:
        head = central.head_config_version()
        history = central.config_versions()
    finally:
        central.close()
    store = ProjectStore(project)
    try:
        rounds = store.validation_rounds(wo_id=after["id"])
    finally:
        store.close()
    assert len(rounds) == 1
    assert rounds[0]["config_version"] == head["id"]
    written = [v for v in history if v["actor"] == "user"]
    assert len(written) == 1 and written[0]["reason"] == "trying it"
