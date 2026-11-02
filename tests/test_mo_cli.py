"""`jarvis mo` CLI (docs/superpowers/specs/2026-11-01-monitoring-orders.md §5)"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, morpheus
from jarvis.central_store import CentralStore
from jarvis.cli import main

pytestmark = pytest.mark.usefixtures("neo_projects")


@pytest.fixture()
def central():
    c = CentralStore()
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_session(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("mo create must not run a session")
    monkeypatch.setattr(claude_cli, "run_headless_result", boom)


def _created(capsys, *argv: str) -> dict:
    assert main(["mo", "create", "proj_a", "watch it", *argv, "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_create_with_a_prompt_inserts_a_queued_mo_row_and_no_session(central, capsys):
    item = _created(capsys, "--prompt", "the dashboard loads", "--every", "2h",
                    "--deadline", "3d")
    row = central.morpheus_item(item["id"])
    assert row["id"].startswith("mo-") and morpheus.is_monitoring_order_id(row["id"])
    assert row["state"] == "queued" and row["script"] == "" and row["origin"] == "user"
    assert row["prompt"] == "the dashboard loads" and row["every_minutes"] == 120
    assert main(["mo", "create", "proj_a", "again", "--prompt", "x"]) == 0
    assert "armed by the daemon on its next pass" in capsys.readouterr().out


def test_create_with_a_script_is_monitoring_at_once(central, capsys, tmp_path):
    (tmp_path / "s.sh").write_text("exit 75\n")
    item = _created(capsys, "--script", str(tmp_path / "s.sh"))
    row = central.morpheus_item(item["id"])
    assert row["state"] == "monitoring" and row["script"] == "exit 75"
    assert row["script_source"] == "user" and row["prompt"] == ""


@pytest.mark.parametrize("argv", [[], ["--prompt", "p", "--script", "f"]])
def test_create_with_both_or_neither_is_refused_and_inserts_nothing(central, argv):
    with pytest.raises(SystemExit) as e:
        main(["mo", "create", "proj_a", "t", *argv])
    assert e.value.code == 2
    assert central.morpheus_items() == []


def test_a_bad_duration_is_refused(central):
    assert main(["mo", "create", "proj_a", "t", "--prompt", "p", "--every", "soon"]) == 1
    assert central.morpheus_items() == []


def test_morpheus_is_an_alias_of_mo(central, capsys):
    morpheus.create(central, project="proj_a", title="watch", prompt="p")
    assert main(["mo", "status"]) == 0
    mo_out = capsys.readouterr().out
    assert main(["morpheus", "status"]) == 0
    assert capsys.readouterr().out == mo_out and "1 queued" in mo_out and "watch" in mo_out


def test_list_filters_and_cancel_refuses_a_settled_order(central, capsys):
    open_ = morpheus.create(central, project="proj_a", title="open one", prompt="p")
    done = morpheus.create(central, project="proj_b", title="done one", prompt="p")
    assert main(["mo", "cancel", done["id"]]) == 0
    assert central.morpheus_item(done["id"])["state"] == "cancelled"
    capsys.readouterr()
    assert main(["mo", "cancel", done["id"]]) == 1
    assert "already cancelled" in capsys.readouterr().err
    assert main(["mo", "list", "--json"]) == 0
    assert [i["id"] for i in json.loads(capsys.readouterr().out)] == [open_["id"]]
    assert main(["mo", "list", "proj_b", "--all", "--json"]) == 0
    assert [i["id"] for i in json.loads(capsys.readouterr().out)] == [done["id"]]
    assert main(["mo", "list", "--state", "nope"]) == 1


def test_a_verb_refuses_an_id_that_is_not_a_monitoring_order(central):
    assert main(["mo", "show", "wo-1234abcd"]) == 1


def test_show_renders_a_prompt_order(central, capsys):
    item = morpheus.create(central, project="proj_a", title="watch", prompt="look here")
    assert main(["mo", "show", item["id"]]) == 0
    out = capsys.readouterr().out
    assert "PR #" not in out and "prompt:\nlook here" in out and "origin user" in out
