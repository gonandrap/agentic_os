"""The onboarding index (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §6)"""

from __future__ import annotations

import html
import json
import os
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import interview, ops, paths  # noqa: E402
from jarvis.testing import make_git_project  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.onboard_open` refuses one"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    data = json.loads(catalog_file.read_text())
    data["projects"][0]["validation"] = {"enabled": True}  # a value `like` must copy
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


def test_like_opens_a_copied_draft_and_a_broken_draft_is_listed(client, tmp_path):
    repo = make_git_project(tmp_path / "work", "newproj")
    r = client.post("/onboard", data={"path": str(repo), "like": "proj_a"})
    assert r.status_code == 303 and r.headers["location"] == "/onboard/newproj/identity"
    saved = json.loads(interview.draft_file("newproj").read_text())
    assert saved["values"]["projects.newproj.validation.enabled"] is True

    (paths.onboarding_dir() / "broken.json").write_text("{not json")
    page = client.get("/onboard").text
    assert 'href="/onboard/newproj"' in page  # control: the good draft links through
    broken = re.search(r'<tr id="draft-broken">.*?</tr>', page, re.S).group(0)
    assert "does not load:" in broken and 'href="/onboard/broken"' not in broken


def test_copy_edit_apply_is_three_requests(client, tmp_path):
    repo = make_git_project(tmp_path / "work", "newproj")
    r = client.post("/onboard", data={"path": str(repo), "like": "proj_a"})
    assert r.status_code == 303 and r.headers["location"] == "/onboard/newproj/identity"
    assert ops.onboard_view("newproj")["progress"]["must_have_missing"] == []
    r = client.post("/onboard/newproj/edit", data={
        "rev": ops.onboard_view("newproj")["rev"], "section": "identity",
        "verb": "identity", "field": "description", "value": "a copy of proj_a"})
    assert r.status_code == 303 and r.headers["location"] == "/onboard/newproj/review"
    assert "Copied from <b>proj_a</b>" in client.get("/onboard/newproj/review").text
    r = client.post("/onboard/newproj/apply", data={
        "rev": ops.onboard_view("newproj")["rev"], "reason": "like proj_a"})
    assert r.status_code == 200, r.text
    # Control: a fresh draft keeps its must-haves open, and its edits stay on the section
    other = make_git_project(tmp_path / "work", "fresh")
    r = client.post("/onboard", data={"path": str(other)})
    assert r.status_code == 303 and r.headers["location"] == "/onboard/fresh"
    assert ops.onboard_view("fresh")["progress"]["must_have_missing"]
    r = client.post("/onboard/fresh/edit", data={
        "rev": ops.onboard_view("fresh")["rev"], "section": "identity",
        "verb": "identity", "field": "description", "value": "fresh"})
    assert r.headers["location"].startswith("/onboard/fresh/identity")


def test_a_non_git_path_re_renders_with_the_refusal_and_the_path_kept(client, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = client.post("/onboard", data={"path": str(plain), "name": "x"})
    assert r.status_code == 400
    page = html.unescape(r.text)
    assert f"not a git repository: {plain.resolve()}" in page
    assert f'name="path" required value="{plain}"' in page
    assert not interview.draft_file("x").exists()


def test_the_nav_links_to_the_index_and_offers_revise(client):
    page = client.get("/config").text
    assert re.search(r'<a href="/onboard"[^>]*>onboard</a>', page)
    index = client.get("/onboard").text
    assert re.search(r'<input type="hidden" name="name" value="proj_a">', index)
    assert '<option value="proj_a">copy settings from proj_a</option>' in index
