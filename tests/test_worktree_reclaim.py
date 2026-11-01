"""A settled order's checkout is given back, and the disk warns before SQLite fails (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md)"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis import db, disk, ops, timeline
from jarvis.catalog import (CatalogError, OsConfig, ProjectSpec, load_catalog,
                            parse_catalog)
from jarvis.central_store import CentralStore
from jarvis.daemon import DISK_LOW_KEY, Daemon
from jarvis.project_store import ProjectStore

HOUR = 3600.0


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return jarvis_home


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def checkout(project: Path, wo_id: str) -> Path:
    """A real git worktree where the dispatcher would have made one"""
    path = project / ".claude" / "worktrees" / wo_id
    subprocess.run(["git", "-C", str(project), "worktree", "add", "-q", "-b", wo_id,
                    str(path)], check=True)
    return path


def settled(store: ProjectStore, project: Path, *, status: str = "completed",
            ago: float = 48 * HOUR) -> tuple[str, Path]:
    wo = store.create_work_order("reclaim me")
    store.update_work_order(wo["id"], worktree=wo["id"])
    store.set_status(wo["id"], status)
    path = checkout(project, wo["id"])
    store.conn.execute("UPDATE work_orders SET updated_at=? WHERE id=?",
                       (db.now() - ago, wo["id"]))
    store.conn.commit()
    return wo["id"], path


def events(store: ProjectStore, wo_id: str) -> list[dict]:
    return [e for e in store.events_of_kind(wo_id, disk.EVENT)]


def spec(project: Path, hours: int = 24) -> ProjectSpec:
    cfg = OsConfig()
    cfg.worktree.reclaim_after_hours = hours
    return ProjectSpec(name="proj_a", path=project, worktree=cfg.worktree)


# -- §1 the reclaim sweep


def test_a_completed_order_past_the_grace_period_loses_its_checkout(store, project):
    wo_id, path = settled(store, project)

    reclaimed, kept = disk.sweep(store, spec(project))

    assert (reclaimed, kept) == ([wo_id], [])
    assert not path.exists()
    assert len(events(store, wo_id)) == 1


def test_the_reclaim_also_removes_the_orders_pytest_temproot(store, project, tmp_path,
                                                             monkeypatch):
    """Per-order pytest roots must not pile up in the temp dir (#723)"""
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    wo_id, _ = settled(store, project)
    root = disk.pytest_temproot(wo_id)
    (root / "pytest-of-u" / "pytest-0").mkdir(parents=True)

    assert disk.sweep(store, spec(project))[0] == [wo_id]
    assert not root.exists()


def test_the_branch_survives_the_removal(store, project):
    """Committed work is never what a reclaim risks"""
    wo_id, path = settled(store, project)
    (path / "work.txt").write_text("a commit nobody has merged\n")
    subprocess.run(["git", "-C", str(path), "add", "work.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "work"], check=True)

    assert disk.sweep(store, spec(project))[0] == [wo_id]

    head = subprocess.run(["git", "-C", str(project), "rev-parse", wo_id],
                          capture_output=True, text=True, check=True)
    assert head.stdout.strip()


def test_inside_the_grace_period_nothing_is_touched(store, project):
    wo_id, path = settled(store, project, ago=2 * HOUR)

    assert disk.sweep(store, spec(project)) == ([], [])
    assert path.is_dir()
    assert events(store, wo_id) == []


def test_an_uncommitted_file_keeps_the_checkout(store, project):
    wo_id, path = settled(store, project)
    (path / "half-done.py").write_text("nobody reviewed this\n")

    reclaimed, kept = disk.sweep(store, spec(project))

    assert (reclaimed, kept) == ([], [wo_id])
    assert path.is_dir()
    assert events(store, wo_id) == []


def test_a_failed_order_keeps_its_checkout_for_retry(store, project):
    """`wo retry` resumes in place"""
    _, path = settled(store, project, status="failed")

    assert disk.sweep(store, spec(project)) == ([], [])
    assert path.is_dir()


def test_zero_hours_turns_the_sweep_off(store, project):
    _, path = settled(store, project)

    assert disk.sweep(store, spec(project, hours=0)) == ([], [])
    assert path.is_dir()


def test_a_checkout_already_gone_is_recorded_and_not_looked_at_again(store, project):
    wo_id, path = settled(store, project)
    subprocess.run(["git", "-C", str(project), "worktree", "remove", "--force",
                    str(path)], check=True)

    assert disk.sweep(store, spec(project))[0] == [wo_id]
    assert events(store, wo_id)[0]["payload"]
    # The event is what keeps the row out of the next sweep
    assert disk.sweep(store, spec(project)) == ([], [])


def test_the_sweep_is_bounded_per_pass(store, project):
    ids = [settled(store, project)[0] for _ in range(3)]

    first, _ = disk.sweep(store, spec(project), limit=2)
    second, _ = disk.sweep(store, spec(project), limit=2)

    assert len(first) == 2
    assert sorted(first + second) == sorted(ids)


def test_the_timeline_names_the_reclaim(store, project):
    label, detail = timeline._describe(disk.EVENT, {"removed": True, "branch": "wo-1"})
    assert label == "Checkout reclaimed"
    assert "wo-1" in detail
    assert disk.EVENT not in timeline.DEBUG_KINDS


# -- §2 the warning


def test_the_volumes_are_deduplicated_by_filesystem(jarvis_home, catalog_file):
    """One tmpfs holds both the home and the project in the suite"""
    volumes = disk.state_volumes(load_catalog(catalog_file))

    assert len(volumes) == 1


def low(monkeypatch, free_gb: float) -> None:
    monkeypatch.setattr(disk, "state_volumes", lambda catalog: [
        disk.Volume(Path("/state"), int(free_gb * disk.BYTES_PER_GB),
                    100 * disk.BYTES_PER_GB)])


def inbox() -> list[dict]:
    central = CentralStore()
    try:
        return central.unacked_inbox()
    finally:
        central.close()


def test_a_volume_under_the_floor_raises_one_item(started, catalog_file, monkeypatch):
    d = Daemon(load_catalog(catalog_file))
    low(monkeypatch, 1.5)

    assert d.check_disk_space() == "low"
    assert d.check_disk_space() == "warned"

    items = inbox()
    assert len(items) == 1
    assert "1.5 GB free on /state" in items[0]["title"]
    assert "os.disk_free_warn_gb" in items[0]["body"]
    assert "database or disk is full" in items[0]["body"]
    assert items[0]["level"] == "warning"


def test_the_warning_is_re_armed_only_once_space_comes_back(started, catalog_file,
                                                            monkeypatch):
    d = Daemon(load_catalog(catalog_file))
    low(monkeypatch, 1.0)
    assert d.check_disk_space() == "low"

    low(monkeypatch, 40.0)
    assert d.check_disk_space() == "ok"

    low(monkeypatch, 1.0)
    assert d.check_disk_space() == "low"
    assert len(inbox()) == 2


def test_a_healthy_volume_says_nothing(started, catalog_file, monkeypatch):
    d = Daemon(load_catalog(catalog_file))
    low(monkeypatch, 60.0)

    assert d.check_disk_space() == "ok"
    assert inbox() == []
    central = CentralStore()
    try:
        assert not (central.get_state(DISK_LOW_KEY) or "")
    finally:
        central.close()


def test_the_floor_has_an_off_switch(started, catalog_file, monkeypatch):
    cat = load_catalog(catalog_file)
    cat.os.disk_free_warn_gb = 0
    low(monkeypatch, 0.1)

    assert Daemon(cat).check_disk_space() == "off"
    assert inbox() == []


def test_the_disk_check_never_stalls_the_tick(started, catalog_file, monkeypatch):
    monkeypatch.setattr(disk, "state_volumes",
                        lambda catalog: (_ for _ in ()).throw(OSError("no statvfs")))
    Daemon(load_catalog(catalog_file)).tick()      # must not raise


# -- the settings


def test_the_defaults_are_the_shipped_ones():
    cfg = parse_catalog({"os": {}, "projects": []}).os
    assert cfg.worktree.reclaim_after_hours == 24
    assert cfg.disk_free_warn_gb == 5.0


@pytest.mark.parametrize("value", [-1, "soon", True, 1.5])
def test_a_bad_grace_period_is_refused_at_boot(value):
    with pytest.raises(CatalogError, match="reclaim_after_hours"):
        parse_catalog({"os": {"worktree": {"reclaim_after_hours": value}},
                       "projects": []})


@pytest.mark.parametrize("value", [-0.5, "lots", True])
def test_a_bad_floor_is_refused_at_boot(value):
    with pytest.raises(CatalogError, match="disk_free_warn_gb"):
        parse_catalog({"os": {"disk_free_warn_gb": value}, "projects": []})


def test_a_project_may_override_the_grace_period(project):
    cat = parse_catalog({
        "os": {"worktree": {"reclaim_after_hours": 24}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "worktree": {"reclaim_after_hours": 1}}]})
    assert cat.project("proj_a").worktree.reclaim_after_hours == 1
