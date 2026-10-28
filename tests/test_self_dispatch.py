"""The shipped OS creates no work nobody typed: `os.autonomy.self_dispatch` ships off"""

import pytest

from jarvis import cli, ops
from jarvis.catalog import CatalogError, load_catalog, parse_catalog
from jarvis.daemon import Daemon
from jarvis.central_store import CentralStore
from jarvis.testing import FIXTURE_BUG_REPO, with_origin
from tests.test_base_red import MAIN_RED, filings, orders, poll, sweep
from tests.test_issue_lifecycle import fleet  # noqa: F401
from tests.test_neo_dispatch import asked, started, work_orders  # noqa: F401


def test_self_dispatch_and_the_stuck_sweep_ship_off():
    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.autonomy.self_dispatch is False
    assert cat.os.fleet_health.enabled is False
    assert cat.projects[0].fleet_health.enabled is False


def test_the_switch_must_be_a_bool_and_is_a_safety_key():
    with pytest.raises(CatalogError, match="self_dispatch"):
        parse_catalog({"os": {"autonomy": {"self_dispatch": "yes"}}, "projects": []})
    assert ops.safety_key("os.autonomy.self_dispatch")


def test_an_agent_cannot_expedite_its_own_bug(fleet, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-agent")
    assert cli.main(["bug", "report", "t", "-d", "d", "-e", "e", "-a", "a",
                     "-p", "low", "--expedite"]) == 0
    assert "--expedite ignored" in capsys.readouterr().err
    assert not fleet.wo_id()


def test_the_user_can_still_expedite(fleet):
    pickup = fleet.file_bug(priority="low", expedite=True)["pickup"]
    assert pickup["wo_id"] and pickup["wo_id"] == fleet.wo_id()


def test_a_neo_confirmed_blocker_dispatches_nothing(fleet):
    pickup = fleet.file_bug(priority="blocker")["pickup"]
    assert "nothing is dispatched" in pickup["reason"]
    fleet.triage(approve=True)
    assert not fleet.wo_id()


def test_a_landed_expedited_fix_ships_no_release(fleet):
    pickup = fleet.file_bug(priority="low", expedite=True)["pickup"]
    fleet.land(pickup["wo_id"])
    assert fleet.releases() == []


def test_neo_files_no_cleanup_order(asked, project):
    assert not [w for w in work_orders(project) if w["origin"] == "neo"]


@pytest.fixture()
def red_tracker(jarvis_home, fake_claude, catalog_file, project, fake_gh):
    """`proj_a` IS the OS's own tracker, and its `main` is red"""
    ops.start_os(str(catalog_file), foreground=True)
    with_origin(project, FIXTURE_BUG_REPO)
    fake_gh.set_default_branch(FIXTURE_BUG_REPO, "main")
    fake_gh.set_runs("main", [MAIN_RED])
    return Daemon(load_catalog(catalog_file))


def test_a_red_main_dispatches_no_fix(red_tracker, fake_gh):
    """Nothing filed, nothing dispatched"""
    tracker = red_tracker
    store = tracker.store_for(tracker.catalog.project("proj_a"))
    poll(tracker, store)
    sweep(tracker, store)
    assert filings(fake_gh) == 0 and orders(store) == []
    central = CentralStore()
    try:
        [row] = central.steward_decisions()
    finally:
        central.close()
    assert (row["origin_kind"], row["outcome"], row["applied"]) == ("red_main",
                                                                     "dispatch", 0)
