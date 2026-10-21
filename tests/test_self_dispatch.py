"""The shipped OS creates no work nobody typed: `os.autonomy.self_dispatch` ships off."""

import pytest

from jarvis import cli, ops
from jarvis.catalog import CatalogError, load_catalog, parse_catalog
from jarvis.daemon import Daemon
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


def test_a_red_main_dispatches_no_fix(catalog_file):
    # Returns before it reads the project, so no tracker or `gh` is touched.
    assert Daemon(load_catalog(catalog_file)).dispatch_base_fix(None, None) is None
