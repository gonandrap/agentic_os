"""The onboarding workspace pages (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §3)"""

from __future__ import annotations

import html
import json
import os
import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import interview, ops, wiring  # noqa: E402
from jarvis.testing import make_git_project  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402
from jarvis.ui.onboard_pages import slug  # noqa: E402

NAME = "newproj"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.onboard_edit` refuses one"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture(autouse=True)
def no_inventory(monkeypatch):
    """`wiring.discover` execs `claude`; the view reads one fixed lever instead"""
    item = wiring.Item(kind="skill", name="s", source="user", lever=f"{wiring.LEVER_SKILL}s")
    monkeypatch.setattr(interview, "wiring_choices",
                        lambda draft, *, document: {"items": [item]})


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file, tmp_path):
    ops.start_os(str(catalog_file), foreground=True)
    interview.new_draft(make_git_project(tmp_path / "work", NAME), NAME)
    return TestClient(create_app(), follow_redirects=False)


def view(name=NAME):
    return ops.onboard_view(name)


def section_of(path, name=NAME):
    return next(s["key"] for s in view(name)["sections"] for k in s["knobs"]
                if k["path"] == path)


def row(page: str, path: str) -> str:
    """The `<tr>` whose id is the knob's anchor"""
    m = re.search(rf'<tr id="k-{re.escape(slug(path))}"[^>]*>.*?</tr>', page, re.S)
    assert m, f"no row for {path}"
    return m.group(0)


def edit(client, section, **fields):
    data = {"rev": view()["rev"], "section": section, **fields}
    return client.post(f"/onboard/{NAME}/edit", data=data)


def text_knob():
    """An asked knob edited as text (evolution.mode refuses junk with the parser's words)"""
    return f"projects.{NAME}.evolution.mode"


def test_section_lists_every_knob_and_the_rail_every_section(client):
    v = view()
    keys = [s["key"] for s in v["sections"]]
    for s in v["sections"]:
        if s["key"] == "review":
            continue
        page = client.get(f"/onboard/{NAME}/{s['key']}").text
        for k in s["knobs"]:
            assert f'id="k-{slug(k["path"])}"' in page, (s["key"], k["path"])
    page = client.get(f"/onboard/{NAME}/evolution").text
    rail = re.findall(r'class="sec" href="/onboard/newproj/([^"]+)"', page)
    assert rail == keys


def test_set_then_reload_shows_draft_and_a_bad_value_is_kept(client):
    path, sec = text_knob(), section_of(text_knob())
    knob = next(k for s in view()["sections"] for k in s["knobs"] if k["path"] == path)
    good = "enforce" if knob["value"] != "enforce" else "shadow"

    r = edit(client, sec, verb="set", path=path, value=good)
    assert r.status_code == 303 and r.headers["location"].endswith(f"#k-{slug(path)}")
    page = client.get(f"/onboard/{NAME}/{sec}").text
    assert f'value="{good}"' in row(page, path) and ">draft<" in row(page, path)

    r = edit(client, sec, verb="set", path=path, value="nonsense-typed")
    assert r.status_code == 400
    assert "must be one of" in html.unescape(r.text)
    assert 'value="nonsense-typed"' in row(r.text, path)


def test_the_filter_index_reaches_other_sections(client):
    page = client.get(f"/onboard/{NAME}/identity").text
    raw = re.search(r'<script type="application/json" id="ob-index">(.*?)</script>',
                    page, re.S).group(1)
    index = json.loads(raw)
    other = text_knob()
    hit = next(k for k in index if k["path"] == other)
    assert hit["section"] == "evolution" != "identity"
    assert hit["href"].startswith(f"/onboard/{NAME}/evolution?focus=")


def test_help_forms_post_section_and_focus(client):
    path = text_knob()
    page = client.get(f"/onboard/{NAME}/evolution").text
    forms = re.findall(r'<form[^>]*action="/onboard/newproj/help">(.*?)</form>', page, re.S)
    fields = [dict(re.findall(r'name="(\w+)" value="([^"]*)"', f)) for f in forms]
    assert {"section": "evolution", "focus": ""} in fields
    assert {"section": "evolution", "focus": path} in fields


def test_a_stale_rev_is_refused_with_the_reload_message(client):
    r = client.post(f"/onboard/{NAME}/edit", data={
        "rev": "0" * 64, "section": "evolution", "verb": "remaining"})
    assert r.status_code == 400
    assert ops.ONBOARD_STALE in html.unescape(r.text)


def test_redirect_404_focus_and_neighbours(client):
    first = view()["progress"]["must_have_missing"][0]
    r = client.get(f"/onboard/{NAME}")
    assert r.status_code == 303 and r.headers["location"] == f"/onboard/{NAME}/{first}"
    assert client.get(f"/onboard/{NAME}/nosuch").status_code == 404
    assert client.get("/onboard/nobody/evolution").status_code == 404

    path = text_knob()
    page = client.get(f"/onboard/{NAME}/evolution", params={"focus": path}).text
    assert page.count('aria-current="true"') == 1
    assert 'aria-current="true"' in row(page, path)
    # Control: without focus no row is marked
    assert 'aria-current="true"' not in client.get(f"/onboard/{NAME}/evolution").text

    keys = [s["key"] for s in view()["sections"]]
    for i, key in enumerate(keys[:-1]):
        page = client.get(f"/onboard/{NAME}/{key}").text
        nxt = re.search(r'<a rel="next" href="/onboard/newproj/([^"]+)"', page).group(1)
        assert nxt == keys[i + 1]
        prev = re.search(r'<a rel="prev" href="/onboard/newproj/([^"]+)"', page)
        assert (prev.group(1) if prev else None) == (keys[i - 1] if i else None)


def test_an_adopted_project_shows_the_catalog_value(client, catalog_file, project):
    interview.new_draft(project, "proj_a")
    knob = next(k for s in view("proj_a")["sections"] if s["key"] not in ("identity", "wiring")
                for k in s["knobs"]
                if k["path"].startswith("projects.proj_a.") and isinstance(k["default"], bool))
    document = json.loads(catalog_file.read_text())
    container, leaf = ops._document_slot(document, knob["path"], create=True)
    container[leaf] = not knob["default"]
    catalog_file.write_text(json.dumps(document))

    sec = section_of(knob["path"], "proj_a")
    page = client.get(f"/onboard/proj_a/{sec}").text
    shown = row(page, knob["path"])
    assert ">catalog<" in shown and json.dumps(not knob["default"]) in shown
    assert "keep these" in page
