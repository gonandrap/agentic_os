"""Review and apply an onboarding draft (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §5)"""

from __future__ import annotations

import html
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import interview, ops, wiring  # noqa: E402
from jarvis.testing import make_git_project  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402
from jarvis.ui.onboard_pages import section_url  # noqa: E402

NAME = "newproj"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.onboard_*` writes refuse one"""
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


def head():
    return next((r for r in ops.config_history(limit=50) if r["head"]), None)


def defaults(client, section, name=NAME):
    r = client.post(f"/onboard/{name}/edit", data={
        "rev": ops.onboard_view(name)["rev"], "section": section, "verb": "defaults"})
    assert r.status_code == 303, r.text


def apply(client, name=NAME, **data):
    return client.post(f"/onboard/{name}/apply",
                       data={"rev": ops.onboard_view(name)["rev"], **data})


def test_review_is_not_shadowed_by_the_section_catch_all(client):
    r = client.get(f"/onboard/{NAME}/review")
    assert r.status_code == 200
    assert "review and apply" in r.text.lower()
    # Control: the catch-all still owns a real section and refuses an unknown one
    assert client.get(f"/onboard/{NAME}/evolution").status_code == 200
    assert client.get(f"/onboard/{NAME}/nosuch").status_code == 404


def test_each_changed_path_links_to_its_own_section(client, project):
    interview.new_draft(project, "proj_a")
    view = ops.onboard_view("proj_a")
    picked: dict[str, dict] = {}
    for s in view["sections"]:
        for k in s["knobs"]:
            if (s["key"] not in picked and len(picked) < 2 and not k["safety"]
                    and k["kind"] in ("asked", "classified")
                    and k["path"].startswith("projects.proj_a.")
                    and isinstance(k["value"], bool)):
                picked[s["key"]] = k
    assert len(picked) == 2
    for key, k in picked.items():
        r = client.post("/onboard/proj_a/edit", data={
            "rev": ops.onboard_view("proj_a")["rev"], "section": key, "verb": "set",
            "path": k["path"], "value": "false" if k["value"] else "true"})
        assert r.status_code == 303, r.text
    page = client.get("/onboard/proj_a/review").text
    assert len(ops.onboard_view("proj_a")["review"]["rows"]) == 2
    for key, k in picked.items():
        href = html.escape(section_url("proj_a", key, k["path"]))
        assert f'<a class="mono" href="{href}">{k["path"]}</a>' in page


def test_apply_refuses_open_must_haves_stale_rev_and_a_missing_reason(client):
    missing = ops.onboard_view(NAME)["progress"]["must_have_missing"]
    for key in missing[:-1]:
        defaults(client, key)
    before = head()
    r = apply(client)  # no reason yet: a given one is saved even when apply refuses
    assert r.status_code == 400 and f"decide first: {missing[-1]}" in html.unescape(r.text)
    assert head() == before

    defaults(client, missing[-1])
    r = client.post(f"/onboard/{NAME}/apply", data={"rev": "0" * 64})
    assert r.status_code == 400 and ops.ONBOARD_STALE in html.unescape(r.text)
    assert head() == before

    r = apply(client)
    with pytest.raises(ops.OpsError) as refusal:
        ops.onboard_apply(ops.onboard_view(NAME)["draft_path"], dry_run=True)
    assert r.status_code == 400 and str(refusal.value) in html.unescape(r.text)
    assert head() == before

    r = apply(client, reason="new project, defaults")
    assert r.status_code == 200, r.text
    after = head()
    assert after["id"] != (before or {}).get("id") and after["actor"] == "onboard"
    assert after["id"] in r.text


def test_a_new_project_is_told_to_run_jarvis_start(client):
    for key in ops.onboard_view(NAME)["progress"]["must_have_missing"]:
        defaults(client, key)
    view = ops.onboard_edit(NAME, "reason", rev=ops.onboard_view(NAME)["rev"],
                            value="a new project")
    dry = ops.onboard_apply(view["draft_path"], dry_run=True)
    assert dry["restart_needed"]
    assert dry["restart_why"] in html.unescape(client.get(f"/onboard/{NAME}/review").text)
    page = html.unescape(apply(client, reason="a new project").text)
    assert f"jarvis start --catalog {dry['catalog']}" in page
    assert dry["restart_why"] in page
