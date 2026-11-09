"""Requestor filters, chips and `--mine` (docs/superpowers/specs/2026-11-09-yours-requestor-stamp-and-tree-notice.md §5)"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import cli, ops  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def orders(jarvis_home, fake_claude, catalog_file, live_daemon, project):
    """One user and one OS order of each kind, a child of the user feature, and inbox rows"""
    ops.start_os(str(catalog_file), foreground=True)
    mine = ops.create_work_order("proj_a", "mine-wo")
    theirs = ops.create_work_order("proj_a", "theirs-wo", requestor="os")
    fo = ops.create_feature_order("proj_a", "mine-fo", description="d")
    os_fo = ops.create_feature_order("proj_a", "theirs-fo", description="d", requestor="os")
    io = ops.create_improvement_order("proj_a", "mine-io", description="d", refs=["wo-1"])
    os_io = ops.create_improvement_order("proj_a", "theirs-io", description="d",
                                         refs=["wo-1"], requestor="os")
    store = ProjectStore(project)
    try:
        # OS-shaped child: the store copies the user parent's stamp
        kid = store.create_work_order("kid-wo", origin="os", kind="planner",
                                      parent_id=fo["id"])
        legacy = store.create_work_order("legacy-wo")  # NULL: walked, reads `you`
        # The dashboard collapses `pending` into a count; running rows are drawn
        for wo in (mine, theirs, kid, legacy):
            store.set_status(wo["id"], "running")
    finally:
        store.close()
    central = CentralStore()
    try:
        central.add_inbox("proj_a", "note-mine", wo_id=mine["id"])
        central.add_inbox("proj_a", "note-theirs", wo_id=theirs["id"])
        central.add_inbox("proj_a", "note-loose")
        central.add_inbox("proj_a", "note-gone", wo_id="wo-deadbeef")
    finally:
        central.close()
    return {"mine": mine, "theirs": theirs, "fo": fo, "os_fo": os_fo, "io": io,
            "os_io": os_io, "kid": kid, "legacy": legacy, "project": project}


@pytest.fixture()
def client(orders):
    return TestClient(create_app(), follow_redirects=False)


def _shown(text, titles):
    return {t for t in titles if t in text}


WO_TITLES = ("mine-wo", "theirs-wo", "kid-wo", "legacy-wo", "mine-fo", "theirs-fo")


def test_the_classifier_reads_the_column_and_walks_only_a_null_row(orders):
    store = ProjectStore(orders["project"])
    try:
        rows = {k: store.get_work_order(orders[k]["id"])
                for k in ("mine", "theirs", "kid", "legacy")}
        got = {k: ops.is_user_requested(store, r) for k, r in rows.items()}
    finally:
        store.close()
    assert rows["kid"]["requestor"] == "user"
    assert rows["legacy"]["requestor"] is None
    assert got == {"mine": True, "theirs": False, "kid": True, "legacy": True}
    # A child of a user feature reads `you`, though `launched_by_os` says otherwise
    assert ops.launched_by_os(rows["kid"])
    assert ops.wo_tags(rows["kid"], "proj_a")[-1] == "you"
    assert ops.wo_tags(rows["theirs"], "proj_a")[-1] == "os"


@pytest.mark.parametrize("path", ["/", "/project/proj_a"])
def test_requestor_filter_on_dashboard_and_project_page(client, path):
    sep = "&" if "?" in path else "?"
    every = _shown(client.get(path).text, WO_TITLES)
    assert every == set(WO_TITLES)
    assert _shown(client.get(f"{path}{sep}requestor=any").text, WO_TITLES) == every
    assert _shown(client.get(f"{path}{sep}requestor=you").text, WO_TITLES) == \
        {"mine-wo", "kid-wo", "legacy-wo", "mine-fo"}
    assert _shown(client.get(f"{path}{sep}requestor=os").text, WO_TITLES) == \
        {"theirs-wo", "theirs-fo"}
    # Composes with the type filter
    assert _shown(client.get(f"{path}{sep}requestor=os&type=work").text, WO_TITLES) == \
        {"theirs-wo"}


def test_any_is_todays_output_and_leaves_no_chip(client):
    for path in ("/", "/project/proj_a", "/inbox"):
        page = client.get(f"{path}?requestor=any").text
        assert "requestor: any" not in page
        assert 'name="requestor"' in page


def test_requestor_filter_on_the_inbox(client):
    titles = ("note-mine", "note-theirs", "note-loose", "note-gone")
    plain = client.get("/inbox").text
    assert _shown(plain, titles) == set(titles)
    assert "Ack all" in plain
    assert _shown(client.get("/inbox?requestor=any").text, titles) == set(titles)
    you = client.get("/inbox?requestor=you").text
    assert _shown(you, titles) == {"note-mine"}
    # Filtered: "Ack shown" acks only the rows shown
    assert "Ack shown" in you
    assert _shown(client.get("/inbox?requestor=os").text, titles) == \
        {"note-theirs", "note-loose", "note-gone"}


@pytest.mark.parametrize("verb,mine,theirs", [
    ("wo", ("mine", "kid", "legacy"), ("theirs",)),
    ("fo", ("fo",), ("os_fo",)),
    ("io", ("io",), ("os_io",)),
])
def test_mine_on_each_list_verb(orders, capsys, verb, mine, theirs):
    cli.main([verb, "list", "--json"])
    every = {r["id"]: r["requestor_view"] for r in json.loads(capsys.readouterr().out)}
    assert {orders[k]["id"] for k in mine + theirs} <= set(every)
    assert all(every[orders[k]["id"]] == "os" for k in theirs)
    cli.main([verb, "list", "--mine", "--json"])
    kept = {r["id"] for r in json.loads(capsys.readouterr().out)}
    assert {orders[k]["id"] for k in mine} <= kept
    assert not kept & {orders[k]["id"] for k in theirs}
    assert all(every[i] == "you" for i in kept)
    # The human listing carries the chip
    cli.main([verb, "list"])
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if orders[theirs[0]]["id"] in ln)
    assert "[os]" in line
