"""Which configuration a unit ran under, and the one place that stamp is load-bearing"""

from __future__ import annotations

import json

from jarvis import catalog as catalog_mod
from jarvis import cli, config_version as cv, ops
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

from test_validation_loop import (  # noqa: F401  (the fixture and its helpers)
    Validator,
    finish,
    fleet,
    passed,
    rejected,
)


def write_version(fleet, project_validation: dict | None = None, **validation) -> str:
    """One row in the ledger: the fleet's catalog with these `os.validation` keys changed"""
    document = json.loads(fleet.catalog_path.read_text())
    document["os"]["validation"] = {**document["os"].get("validation", {}), **validation}
    if project_validation is not None:
        document["projects"][0]["validation"] = project_validation
    resolved = cv.resolve(catalog_mod.parse_catalog(document))
    central = CentralStore()
    try:
        return central.add_config_version(document, resolved, actor="user",
                                          reason="test")["id"]
    finally:
        central.close()


def rounds(fleet, wo_id: str) -> list[dict]:
    store = fleet.store()
    try:
        return store.validation_rounds(wo_id=wo_id)
    finally:
        store.close()


# -- the work order's stamp


def test_dispatch_stamps_the_version_in_force_on_the_row_and_the_event(fleet):
    """Frozen at dispatch beside model/effort/permission_mode"""
    version = write_version(fleet, max_rounds=2)

    wo = fleet.dispatch()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["config_version"] == version
        dispatched = store.events_of_kind(wo["id"], "dispatched")
        assert [json.loads(e["payload"])["config_version"] for e in dispatched] == [
            version]
    finally:
        store.close()


def test_a_fleet_with_no_ledger_dispatches_and_stamps_nothing(fleet):
    """NULL means "ran before the console existed", never version 1"""
    wo = fleet.dispatch()

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert fresh["config_version"] is None
        assert fresh["status"] == "running"
    finally:
        store.close()


# -- the round's stamp


def test_two_rounds_of_one_work_order_carry_the_version_each_was_opened_under(fleet):
    """The question the work-order stamp cannot answer: a unit judged twice"""
    first_version = write_version(fleet, max_rounds=9)
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    second_version = write_version(fleet, max_rounds=9, diff_chars=1234)
    assert second_version != first_version
    fleet.change(wo["id"], "print('two')\n")
    finish(fleet, wo["id"])

    assert [r["config_version"] for r in rounds(fleet, wo["id"])] == [
        first_version, second_version]


# -- and the stamp is what judges the round


def test_the_round_is_judged_under_its_stamp_and_not_the_live_catalog(fleet):
    """Stamped `max_rounds=1`, live catalog 3: one rejection is the LAST round"""
    write_version(fleet, max_rounds=1)
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
        assert [r["outcome"] for r in store.validation_rounds(wo_id=wo["id"])] == [
            "escalated"]
    finally:
        store.close()


def test_the_stamp_is_read_project_first_the_way_the_live_catalog_is(fleet):
    """The two features meeting: per-project validation is field-level inheritance (`kn-6ca2bcd9`)"""
    write_version(fleet, max_rounds=9, project_validation={"max_rounds": 1})
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


def test_the_same_rejection_with_no_stamp_falls_back_to_the_live_catalog(fleet):
    """The pair"""
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        assert [r["config_version"] for r in
                store.validation_rounds(wo_id=wo["id"])] == [None]
        assert [r["outcome"] for r in store.validation_rounds(wo_id=wo["id"])] == [
            "rejected"]
        assert store.get_work_order(wo["id"])["status"] != "needs_review"
    finally:
        store.close()


def test_a_null_stamp_reads_the_live_catalog_and_not_the_defaults(fleet):
    """`max_rounds=1` in the catalog and nothing in the ledger: the round must escalate"""
    fleet.reconfigure(max_rounds=1)
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


def test_a_stamp_the_ledger_has_lost_is_judged_under_the_live_catalog(fleet):
    """An id with no row is the same problem as no id: nothing recorded to prefer over what is running now"""
    version = write_version(fleet, max_rounds=1)
    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    central = CentralStore()
    try:
        central.conn.execute("DELETE FROM os_config_versions WHERE id=?", (version,))
    finally:
        central.close()

    fleet.drain()

    store = fleet.store()
    try:
        assert [r["outcome"] for r in store.validation_rounds(wo_id=wo["id"])] == [
            "passed"]
    finally:
        store.close()


# -- how it reads


def test_round_line_names_the_version_or_says_not_recorded():
    stamped = {"round": 1, "fingerprint": "aaaa1111", "outcome": "passed", "reason": "",
               "config_version": "cfg-a1b2c3d4e5f6"}
    assert "config cfg-a1b2c3d4e5f6" in ops.round_line(stamped)
    assert "config not recorded" in ops.round_line({**stamped, "config_version": None})


def test_wo_show_carries_a_config_header_and_json_keeps_the_raw_column(fleet, capsys):
    version = write_version(fleet, max_rounds=2)
    write_version(fleet, max_rounds=4)          # the fleet moves on...
    wo = fleet.dispatch()                       # ...after this one was dispatched
    store = fleet.store()
    try:
        store.update_work_order(wo["id"], config_version=version)
    finally:
        store.close()

    assert cli.main(["wo", "show", wo["id"]]) == 0
    out = capsys.readouterr().out
    assert f"config: {version} (1 versions since)" in out
    assert "config_version" not in out          # replaced, not doubled

    assert cli.main(["--json", "wo", "show", wo["id"]]) == 0
    assert json.loads(capsys.readouterr().out)["config_version"] == version


def test_wo_show_says_not_recorded_rather_than_naming_a_version(fleet, capsys):
    """The honesty boundary: a work order that predates the console has no version"""
    write_version(fleet, max_rounds=2)
    store = ProjectStore(fleet.project)
    try:
        wo = store.create_work_order("ran before the console existed")
    finally:
        store.close()

    assert cli.main(["wo", "show", wo["id"]]) == 0
    out = capsys.readouterr().out
    assert "config: not recorded" in out
    assert "cfg-" not in out
