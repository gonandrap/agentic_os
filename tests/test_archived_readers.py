"""Archived projects stay readable (docs/superpowers/specs/2026-10-28-offboard-a-project.md §2)"""

from __future__ import annotations

import inspect

import pytest

from jarvis import ops, search
from jarvis.central_store import ARCHIVED, CentralStore
from jarvis.project_store import ProjectStore

LOGIN = "the login spinner never stops on a cold cache"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)


def _archive(name="proj_a"):
    central = CentralStore()
    try:
        central.set_project_status(name, ARCHIVED)
    finally:
        central.close()


def _client():
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def test_signature_is_pinned():
    params = list(inspect.signature(ops.registered_project_paths).parameters.values())
    assert [(p.name, p.kind, p.default) for p in params] == [
        ("include_archived", inspect.Parameter.KEYWORD_ONLY, False)]


def test_registry_hides_archived_unless_asked(started, project):
    _archive()
    assert ops.registered_project_paths() == {}
    assert ops.registered_project_paths(include_archived=True) == {"proj_a": project}


def test_include_archived_writes_nothing(started, jarvis_home):
    _archive()
    files = sorted(jarvis_home.glob("os.db*"))
    before = [(f, f.stat().st_mtime_ns, f.read_bytes()) for f in files]
    ops.registered_project_paths(include_archived=True)
    assert [(f, f.stat().st_mtime_ns, f.read_bytes()) for f in files] == before


def test_search_reaches_and_marks_an_archived_project(started):
    wo = ops.create_work_order("proj_a", "fix the login spinner", LOGIN)
    assert search.search("login spinner")[0]["archived"] is False
    _archive()
    hits = search.search("login spinner")
    assert [(h["id"], h["archived"]) for h in hits] == [(wo["id"], True)]
    assert search.search("login spinner", project="proj_a")[0]["archived"] is True


def test_dashboard_reads_an_archived_project_and_lists_it_nowhere(started, project):
    wo = ops.create_work_order("proj_a", "fix the login spinner", LOGIN)
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order("CSV export", description="the whole ask")
    finally:
        store.close()
    _archive()
    client = _client()
    for url in ("/project/proj_a", f"/wo/proj_a/{wo['id']}",
                f"/fo/proj_a/{fo['id']}", f"/spec/proj_a/{fo['id']}"):
        page = client.get(url)
        assert page.status_code == 200, (url, page.status_code)
        assert "project is archived" in page.text, url
    assert 'href="/project/proj_a"' not in client.get("/inbox").text
    assert "project is archived" not in client.get("/inbox").text
