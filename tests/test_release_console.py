"""/release: pending work, the prefix cut, traced releases (docs/superpowers/specs/2026-11-01-release-console.md)"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis import db, morpheus, ops, release
from jarvis.catalog import MorpheusConfig
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("jarvis_home")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def os_repo(tmp_path, monkeypatch):
    """jarvis-0.1.0, then #2 (an improvement's child, failed in prod) and #3 pending"""
    repo = tmp_path / "jarvis_os"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first (#1)")
    _git(repo, "tag", "jarvis-0.1.0")
    central = CentralStore()
    try:
        central.upsert_project("jarvis_os", str(repo), "the OS")
        central.conn.commit()
    finally:
        central.close()
    store = ProjectStore(repo)
    try:
        io = store.create_feature_order(title="why do gates stall", kind="improvement",
                                        metadata={ops.EVIDENCE_REFS_KEY: ["wo-0000beef"]})
        child = store.create_work_order("fix the stall", parent_id=io["id"])
        plain = store.create_work_order("tidy")
    finally:
        store.close()
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"[{child['id']}] fix the stall (#2)")
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"[{plain['id']}] tidy (#3)")
    central = CentralStore()
    try:
        item = morpheus.enqueue(central, MorpheusConfig(), morpheus.PullRequestBody(
            number=2, url="https://github.com/o/r/pull/2", title="x",
            body="## How to validate in production\n\nlook\n"), project="jarvis_os", repo="o/r")
        central.update_morpheus_item(item["id"], state="failed")
    finally:
        central.close()
    real = ops._release_repo
    monkeypatch.setattr(ops, "_release_repo",
                        lambda project="": real(project) if project else ("jarvis_os", repo))
    return {"repo": repo, "io": io["id"], "child": child["id"], "plain": plain["id"],
            "mv": item["id"], "sha2": _git(repo, "rev-parse", "HEAD~1"),
            "sha3": _git(repo, "rev-parse", "HEAD")}


def test_pending_is_newest_first_traced_and_coloured(os_repo):
    central = CentralStore()
    try:  # an order with no PR is no release row, and no crash (monitoring-orders spec §2)
        morpheus.create(central, project="jarvis_os", title="disk", prompt="watch the disk")
    finally:
        central.close()
    report = ops.release_overview()
    newest, older = report["pending"]
    assert (newest["pr"], newest["rides"], older["pr"], older["rides"]) == (3, 2, 2, 1)
    assert older["validation"] == "failed" and older["morpheus"]["id"] == os_repo["mv"]
    assert older["origin"] == "improvement" and older["wo"]["id"] == os_repo["child"]
    assert [h["id"] for h in older["trace"]] == [os_repo["io"], "wo-0000beef"]
    assert newest["validation"] == "unwatched" and newest["origin"] == "work order"
    assert [r["tag"] for r in report["releases"]] == ["jarvis-0.1.0"]


def test_a_cut_ships_the_prefix_and_pins_its_base(os_repo):
    wo = ops.release_cut(os_repo["sha2"])
    store = ProjectStore(os_repo["repo"])
    try:
        row = store.get_work_order(wo["id"])
    finally:
        store.close()
    meta = db.from_json(row["metadata"], {})
    assert meta[release.BATCH_KEY] == [] and meta[release.BASE_KEY] == os_repo["sha2"]
    assert f"--wo {wo['id']} --base {os_repo['sha2']}" in row["description"]
    assert "fix the stall (#2)" in row["description"] and "tidy (#3)" not in row["description"]
    with pytest.raises(ops.OpsError, match="already shipping a release"):
        ops.release_cut(os_repo["sha3"])


def test_a_base_that_is_not_pending_is_refused(os_repo):
    with pytest.raises(ops.OpsError, match="not pending"):
        ops.release_cut("f" * 40)


def test_the_pages_link_every_hop(os_repo):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    client = TestClient(create_app())
    page = client.get("/release")
    assert page.status_code == 200 and 'href="/release"' in page.text
    assert f'value="{os_repo["sha3"]}"' in page.text and "Add all" in page.text
    assert f"/go/{os_repo['io']}" in page.text and "/release/jarvis-0.1.0" in page.text
    assert client.get("/release?origin=improvement").text.count('type="radio" name="base"') == 1
    assert client.get("/release/jarvis-0.1.0").status_code == 200
    assert client.get("/release/jarvis-9.9.9").status_code == 404
    cut = client.post("/release/cut", data={"base": os_repo["sha3"]}, follow_redirects=False)
    assert cut.status_code == 303 and cut.headers["location"].startswith("/wo/jarvis_os/wo-")
    page = client.get("/release").text
    assert "release in flight" in page


def test_another_project_is_picked_tagged_and_cut_outside_the_os_machinery(os_repo, tmp_path):
    """Spec 2026-11-01 §3"""
    repo = tmp_path / "app"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    central = CentralStore()
    try:
        central.upsert_project("app", str(repo), "an app")
        central.conn.commit()
    finally:
        central.close()
    assert "app" in ops.release_projects()
    report = ops.release_overview("app")
    assert (report["project"], report["is_os"], len(report["pending"])) == ("app", False, 2)
    _git(repo, "tag", "v1")
    assert ops.release_overview("app")["newest"] == "v1"
    _git(repo, "commit", "-q", "--allow-empty", "-m", "three")
    _git(repo, "tag", "app-0.1.0")
    report = ops.release_overview("app")  # an app-* tag no longer hides the other streams
    assert (report["tag_glob"], report["newest"]) == ("*", "app-0.1.0")
    assert [r["tag"] for r in report["releases"]] == ["app-0.1.0", "v1"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "four")
    wo = ops.release_cut(_git(repo, "rev-parse", "HEAD"), "app")
    store = ProjectStore(repo)
    try:
        row = store.get_work_order(wo["id"])
    finally:
        store.close()
    meta = db.from_json(row["metadata"], {})
    assert release.BATCH_KEY not in meta and not release.is_release_order(row)
    assert "app-X.Y.Z" not in row["description"] and "shipit" not in row["description"]
    assert "its own scheme" in row["description"]
    with pytest.raises(ops.OpsError, match="already shipping a release"):
        ops.release_cut(_git(repo, "rev-parse", "HEAD"), "app")
    with pytest.raises(ops.OpsError, match="no project 'nope'"):
        ops.release_overview("nope")

    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    client = TestClient(create_app())
    page = client.get("/release?project=app").text
    assert "Release — app" in page and '/release/app-0.1.0?project=app' in page
    assert client.get("/release/app-0.1.0?project=app").status_code == 200
    assert 'name="project" value="app"' in client.get("/release/app-0.1.0?project=app").text


def test_a_release_page_colours_a_failed_pr(os_repo):
    _git(os_repo["repo"], "tag", "jarvis-0.1.1")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    page = TestClient(create_app()).get("/release/jarvis-0.1.1").text
    assert f'class="mono tone-bad" href="/mo/{os_repo["mv"]}"' in page
    assert "needs-you" in page


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    return path


def _commit(repo: Path, msg: str, tag: str = "") -> None:
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    if tag:
        _git(repo, "tag", tag)


def test_two_streams_whose_name_and_order_disagree_are_decided_by_ancestry(tmp_path):
    """io-fad638fd: v0.1.17 sorts after pipeline-v0.1.7 by name but is OLDER on main"""
    from jarvis import release_view

    repo = _repo(tmp_path / "pfw")
    _commit(repo, "a (#1)", "v0.1.16")
    _commit(repo, "b (#2)", "v0.1.17")
    _commit(repo, "c (#3)", "pipeline-v0.1.6")
    _commit(repo, "d (#4)", "pipeline-v0.1.7")
    _commit(repo, "e (#5)")
    rows = release_view.tags(repo, "*")
    assert [r["tag"] for r in rows] == ["pipeline-v0.1.7", "pipeline-v0.1.6", "v0.1.17", "v0.1.16"]
    assert [r["previous"] for r in rows] == ["pipeline-v0.1.6", "v0.1.17", "v0.1.16", ""]
    assert [c["pr"] for c in release_view.commits(repo, "main", [r["tag"] for r in rows])] == [5]
    by = {r["tag"]: r for r in rows}
    assert [c["pr"] for c in release_view.commits(
        repo, "pipeline-v0.1.6", by["pipeline-v0.1.6"]["follows"])] == [3]
    assert [c["pr"] for c in release_view.commits(repo, "v0.1.16", by["v0.1.16"]["follows"])] == [1]


def test_os_tags_on_release_branches_chain_as_before(tmp_path):
    """jarvis-* tags sit on bump commits off main, so no tag is an ancestor of the next"""
    from jarvis import release_view

    repo = _repo(tmp_path / "os")
    _commit(repo, "one (#1)")
    for n, prs in ((0, (2,)), (1, (3, 4))):
        for pr in prs:
            _commit(repo, f"work (#{pr})")
        _git(repo, "checkout", "-q", "-b", f"release/jarvis-0.1.{n}")
        _commit(repo, f"Release jarvis-0.1.{n}", f"jarvis-0.1.{n}")
        _git(repo, "checkout", "-q", "main")
    _git(repo, "checkout", "-q", "-b", "release/jarvis-0.1.2")  # re-release of 0.1.1's base
    _commit(repo, "Release jarvis-0.1.2", "jarvis-0.1.2")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "later (#5)")
    rows = release_view.tags(repo, "jarvis-*")
    assert [(r["tag"], r["previous"]) for r in rows] == [
        ("jarvis-0.1.2", "jarvis-0.1.1"), ("jarvis-0.1.1", "jarvis-0.1.0"), ("jarvis-0.1.0", "")]
    assert release_view.commits(repo, "jarvis-0.1.2", rows[0]["follows"]) == []
    assert [c["pr"] for c in release_view.commits(repo, "jarvis-0.1.1", rows[1]["follows"])] == [4, 3]
    assert [c["pr"] for c in release_view.commits(repo, "main", [r["tag"] for r in rows])] == [5]
