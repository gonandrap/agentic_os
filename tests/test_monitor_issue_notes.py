"""A monitoring row's outcome reaches the issue its order fixed (io-b42dfb75)"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis import bugreport, db, issues, morpheus, ops
from jarvis.catalog import Catalog, MorpheusConfig, OsConfig
from jarvis.central_store import CentralStore
from jarvis.github import GitHubError
from jarvis.project_store import ProjectStore
from jarvis.testing import TEST_EPOCH

pytestmark = pytest.mark.usefixtures("neo_projects")



@pytest.fixture()
def central():
    c = CentralStore()
    yield c
    c.close()


@pytest.fixture()
def row(central, tmp_path, monkeypatch):
    """A watched row whose order fixed a closed issue: (item, store, tracker calls)"""
    store = ProjectStore(tmp_path / "proj_a")
    monkeypatch.setattr(ops, "registered_project_paths",
                        lambda **kw: {"proj_a": store.project_path})
    wo = store.create_work_order("fix it", status="completed",
                                 issue_url=f"https://github.com/{bugreport.bug_repo()}/issues/1592")
    pr = morpheus.PullRequestBody(number=7, url="https://github.com/o/r/pull/7",
                                  title=f"[{wo['id']}] fix it", body="")
    item = morpheus.enqueue(central, MorpheusConfig(), pr, project="proj_a", repo="o/r",
                            release_tag="jarvis-0.14.14", now=TEST_EPOCH)
    central.update_morpheus_item(item["id"], state="monitoring", script="exit 75",
                                 last_output="FAIL: /cost took 6.8s")
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(issues, "comment", lambda u, b, repo=None: calls.append(("comment", b)))
    monkeypatch.setattr(issues, "reopen", lambda u, b: calls.append(("reopen", b)))
    monkeypatch.setattr(issues, "view", lambda u: SimpleNamespace(closed=True))
    monkeypatch.setattr(issues, "repo_is_private", lambda r: False)
    yield central.morpheus_item(item["id"]), store, calls
    store.close()


def _told(store, wo_id):
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo_id, ops.MONITOR_ISSUE_EVENT)]


def test_an_unverified_row_comments_on_its_issue_once(central, row):
    item, store, calls = row
    assert ops.tell_monitor_issues() == []  # monitoring owes nothing
    central.update_morpheus_item(item["id"], state="unverified", next_check_ts=None)
    assert len(ops.tell_monitor_issues()) == 1
    [(verb, text)] = calls
    assert verb == "comment" and "jarvis-0.14.14" in text
    assert f"jarvis mo show {item['id']}" in text
    assert ops.tell_monitor_issues() == [] and len(calls) == 1  # a rerun adds nothing
    assert [t["transition"] for t in _told(store, item["wo_id"])] == ["unverified"]
    central.update_morpheus_item(item["id"], state="validated")
    assert ops.tell_monitor_issues() == [] and len(calls) == 1  # validated posts nothing


def test_confirm_reopens_the_issue_and_keeps_output_off_a_public_tracker(central, row,
                                                                         monkeypatch):
    item, store, calls = row
    central.update_morpheus_item(item["id"], state="failed", action="neo:4")
    monkeypatch.setattr(ops, "_morpheus_catalog",
                        lambda: Catalog(os=OsConfig(morpheus=MorpheusConfig()), projects=[]))
    monkeypatch.setattr(ops, "create_work_order", lambda *a, **k: {"id": "wo-fe000001"})
    ops.morpheus_confirm(item["id"], "/cost is slow")
    [(verb, text)] = calls
    assert verb == "reopen" and "wo-fe000001" in text and "6.8s" not in text
    [told] = _told(store, item["wo_id"])
    assert (told["act"], told["fix"], told["output"]) == (
        "reopened", "wo-fe000001", "FAIL: /cost took 6.8s")
    assert ops.tell_monitor_issues() == [] and len(calls) == 1


def test_confirm_puts_the_output_on_a_private_tracker(central, row, monkeypatch):
    item, store, calls = row
    monkeypatch.setattr(issues, "repo_is_private", lambda r: True)
    central.update_morpheus_item(
        item["id"], state="failed",
        resolution=f"{morpheus.CONFIRMED}; fix filed as wo-fe000001: slow")
    ops.tell_monitor_issues()
    [(verb, text)] = calls
    assert verb == "reopen" and "FAIL: /cost took 6.8s" in text


def test_an_issue_off_the_os_tracker_is_noted_on_the_order_once(central, row):
    item, store, calls = row
    store.update_work_order(item["wo_id"], issue_url="https://github.com/else/where/issues/3")
    central.update_morpheus_item(
        item["id"], state="failed",
        resolution=f"{morpheus.CONFIRMED}; fix filed as wo-fe000001: slow")
    ops.tell_monitor_issues()
    ops.tell_monitor_issues()
    [told] = _told(store, item["wo_id"])
    assert calls == [] and told["act"] == "kept on the order"
    assert told["output"] == "FAIL: /cost took 6.8s"


def test_a_tracker_failure_is_not_recorded_so_the_next_pass_retries(central, row,
                                                                     monkeypatch):
    item, store, calls = row
    central.update_morpheus_item(item["id"], state="unverified", next_check_ts=None)

    def down(*a, **k):
        raise GitHubError("gh down")
    monkeypatch.setattr(issues, "comment", down)
    assert ops.tell_monitor_issues() == [] and _told(store, item["wo_id"]) == []
