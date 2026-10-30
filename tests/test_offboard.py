"""`jarvis offboard`: archive or purge a project, in the safe order (docs/superpowers/specs/2026-10-28-offboard-a-project.md §7)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import bootstrap, cli, ops, schedule, search
from jarvis.catalog import load_catalog
from jarvis.central_store import ARCHIVED, CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """`ops.offboard_project` refuses a worker session, and the suite is run by one"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def in_catalog(catalog_file: Path) -> bool:
    return "proj_a" in [p["name"] for p in json.loads(catalog_file.read_text())["projects"]]


def status() -> str | None:
    central = CentralStore()
    try:
        row = central.get_project("proj_a")
    finally:
        central.close()
    return row and row["status"]


def test_archive_keeps_history_and_survives_start(started, catalog_file, project):
    store = ProjectStore(project)
    wo = store.create_work_order("zanzibar quokka migration", description="the ask")
    store.update_work_order(wo["id"], status="completed")
    assert (project / ".claude" / "settings.json").exists()

    out = ops.offboard_project("proj_a", catalog_path=str(catalog_file), reason="done")

    assert status() == ARCHIVED and out["registry"] == ARCHIVED
    assert (project / ".jarvis" / "jarvis.db").exists()
    assert not (project / ".claude" / "settings.json").exists()
    assert not in_catalog(catalog_file) and out["catalog"]["version"]
    hits = search.search("zanzibar quokka")
    assert [(h["id"], h["project"]) for h in hits] == [(wo["id"], "proj_a")]
    central = CentralStore()
    assert central.get_inbox(out["inbox_id"])["project"] == "os"
    assert central.head_config_version()["reason"] == "done"
    central.close()

    ops.stop_os()
    ops.start_os(str(catalog_file), foreground=True)

    assert status() == ARCHIVED
    assert not (project / ".claude" / "settings.json").exists()


def test_purge_after_archive_deletes_everything(started, catalog_file, project):
    store = ProjectStore(project)
    wo = store.create_work_order("zanzibar quokka migration", description="the ask")
    store.update_work_order(wo["id"], status="completed")
    central = CentralStore()
    central.add_backlog("proj_a", "someday")
    central.close()
    neo = NeoStore()
    neo.add_learning("prefers squash merges", project="proj_a")
    neo.close()
    ops.offboard_project("proj_a", catalog_path=str(catalog_file), reason="done")

    out = ops.offboard_project("proj_a", purge=True, catalog_path=str(catalog_file))

    assert status() is None and out["registry"] == "deleted"
    assert out["purged"]["os"]["backlog"] == 1 and out["purged"]["neo"]["learnings"] == 1
    assert not (project / ".jarvis").exists()
    assert search.search("zanzibar quokka") == []
    assert out["catalog"]["in_catalog"] is False
    central = CentralStore()
    assert "proj_a" in central.offboarded_names()  # a later leak reads as offboarded, not unknown
    central.close()


@pytest.mark.parametrize("purge", [False, True])
def test_open_order_refuses_without_force(started, catalog_file, project, purge):
    store = ProjectStore(project)
    wo = store.create_work_order("parked", description="the ask")
    store.update_work_order(wo["id"], status="waiting_pr_merge")

    with pytest.raises(ops.OpsError) as e:
        ops.offboard_project("proj_a", purge=purge, catalog_path=str(catalog_file),
                             reason="done")

    assert wo["id"] in str(e.value) and "waiting_pr_merge" in str(e.value)
    assert "--force" in str(e.value)
    assert status() == "active" and in_catalog(catalog_file)
    assert (project / ".claude" / "settings.json").exists()

    out = ops.offboard_project("proj_a", purge=purge, force=True,
                               catalog_path=str(catalog_file), reason="done")

    assert wo["id"] in out["cancelled"]
    if not purge:
        assert store.get_work_order(wo["id"])["status"] == "cancelled"


def test_dry_run_calls_no_writer(started, catalog_file, project, monkeypatch):
    calls: list[str] = []
    real_strip = bootstrap.unbootstrap_project

    def strip(spec, *, purge, dry_run):
        if not dry_run:
            calls.append("unbootstrap_project")
        return real_strip(spec, purge=purge, dry_run=dry_run)

    monkeypatch.setattr(bootstrap, "unbootstrap_project", strip)
    for cls, name in ((CentralStore, "purge_project"), (NeoStore, "purge_project"),
                      (CentralStore, "set_project_status"),
                      (CentralStore, "delete_project")):
        monkeypatch.setattr(cls, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(ops, "_commit_document", lambda *a, **k: calls.append("commit"))
    before = catalog_file.read_text()

    for purge in (False, True):
        ops.offboard_project("proj_a", purge=purge, dry_run=True,
                             catalog_path=str(catalog_file), reason="done")

    assert calls == []
    assert catalog_file.read_text() == before
    assert (project / ".claude" / "settings.json").exists()


def test_dry_run_plan_matches_the_real_run(started, catalog_file, project):
    plan = ops.offboard_project("proj_a", dry_run=True, catalog_path=str(catalog_file),
                                reason="done")
    out = ops.offboard_project("proj_a", catalog_path=str(catalog_file), reason="done")

    assert plan.keys() == out.keys()
    assert plan["dry_run"] and not out["dry_run"]
    would = [a for a in plan["strip"]["actions"] if not a.startswith("(dry run")]
    assert len(would) == len(out["strip"]["actions"]) > 0
    assert plan["catalog"]["removed"] == out["catalog"]["removed"]


@pytest.mark.parametrize("purge", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_os_owning_project_is_refused(started, catalog_file, project, monkeypatch,
                                      purge, force):
    install = str(Path(schedule.__file__).resolve().parent)
    monkeypatch.setitem(schedule._ORIGIN_CACHE, install, ("gonandrap", "agentic_os"))
    monkeypatch.setitem(schedule._ORIGIN_CACHE, str(project), ("gonandrap", "agentic_os"))
    assert schedule.os_project([("proj_a", project)]) == "proj_a"

    with pytest.raises(ops.OpsError, match="runs the OS itself"):
        ops.offboard_project("proj_a", purge=purge, force=force,
                             catalog_path=str(catalog_file), reason="done")

    assert status() == "active" and in_catalog(catalog_file)


@pytest.mark.parametrize("purge", [False, True])
def test_undecidable_owner_fails_closed(started, catalog_file, project, monkeypatch,
                                        purge):
    def broken(*a, **k):
        raise OSError("origin unreadable")

    monkeypatch.setattr(schedule, "os_project", broken)

    with pytest.raises(ops.OpsError, match="origin unreadable"):
        ops.offboard_project("proj_a", purge=purge, force=True,
                             catalog_path=str(catalog_file), reason="done")

    assert status() == "active" and in_catalog(catalog_file)
    assert (project / ".claude" / "settings.json").exists()


def test_missing_reason_refuses_before_any_write(started, catalog_file, project):
    before = catalog_file.read_text()

    with pytest.raises(ops.OpsError, match="--reason"):
        ops.offboard_project("proj_a", catalog_path=str(catalog_file))

    assert status() == "active" and catalog_file.read_text() == before
    assert (project / ".claude" / "settings.json").exists()


def test_moved_project_dir_still_cleans_registry(started, catalog_file, project):
    project.rename(project.with_name("moved"))

    out = ops.offboard_project("proj_a", catalog_path=str(catalog_file), reason="done")

    assert status() == ARCHIVED and not in_catalog(catalog_file)
    assert any("path does not exist" in w for w in out["strip"]["warnings"])
    assert not project.exists()


def test_failed_strip_still_moves_registry_and_catalog(started, catalog_file, project,
                                                       monkeypatch):
    real_strip = bootstrap.unbootstrap_project

    def strip(spec, *, purge, dry_run):
        if not dry_run:
            raise PermissionError("read-only repo")
        return real_strip(spec, purge=purge, dry_run=dry_run)

    monkeypatch.setattr(bootstrap, "unbootstrap_project", strip)

    out = ops.offboard_project("proj_a", catalog_path=str(catalog_file), reason="done")

    assert status() == ARCHIVED and not in_catalog(catalog_file)
    assert out["strip"] == {"actions": [], "warnings": ["strip failed: read-only repo"]}


def test_worker_may_not_offboard(started, catalog_file, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-12345678")

    with pytest.raises(ops.OpsError, match="may not `jarvis offboard`"):
        ops.offboard_project("proj_a", catalog_path=str(catalog_file))


def test_cli_dry_run_prints_counts_first(started, catalog_file, capsys):
    assert cli.main(["offboard", "proj_a", "--dry-run", "--reason", "done",
                     "--catalog", str(catalog_file)]) == 0

    out = capsys.readouterr().out
    assert out.splitlines()[1].startswith("  orders: 0 open, 0 cancelled")
    assert "would remove .claude/settings.json" in out
    assert status() == "active" and in_catalog(catalog_file)


def test_cli_purge_needs_yes(started, catalog_file, monkeypatch):
    monkeypatch.setattr(ops, "offboard_project", lambda *a, **k: pytest.fail("called"))

    with pytest.raises(SystemExit, match=r"deletes <project>/\.jarvis/"):
        cli.main(["offboard", "proj_a", "--purge", "--catalog", str(catalog_file)])
