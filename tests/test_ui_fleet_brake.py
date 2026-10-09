"""The dashboard half of `jarvis pause` / `jarvis resume` (issue #843).

The brake shipped CLI-only; a user looking at the dashboard could not see that the fleet
was stopped, nor stop it. These pin the banner every page carries, the three controls,
and that each control is the COMMAND — `ops.pause_fleet` / `ops.resume_fleet` — writing
the same record `jarvis status` reads.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import fleet, ops  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402

BANNER = "FLEET PAUSED"


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


def _pause():
    central = CentralStore()
    try:
        return fleet.load_pause(central)
    finally:
        central.close()


def _text(response) -> str:
    return " ".join(response.text.split())


def _pending(project) -> dict:  # noqa: ARG001 — the fixture registers proj_a
    wo = ops.create_work_order("proj_a", "held by the brake")
    return wo


# -- the banner ------------------------------------------------------------------------


def test_no_banner_and_a_pause_form_while_the_fleet_runs(client):
    page = client.get("/").text
    assert BANNER not in page
    assert 'action="/fleet/pause"' in page
    assert 'action="/fleet/resume"' not in page


def test_the_banner_is_on_every_page_while_paused(client, project):
    wo = _pending(project)
    ops.pause_fleet(reason="spend is out of control", allow=[wo["id"]])

    for path in ("/", "/project/proj_a", f"/wo/proj_a/{wo['id']}", "/inbox", "/gates"):
        page = _text(client.get(path))
        assert f"{BANNER}</span> — spend is out of control" in page, path
        assert "· since " in page, path
        # the allow-list, each id a link to its order
        assert f'<a class="mono" href="/wo/proj_a/{wo["id"]}">{wo["id"]}</a>' in page, path
        assert 'action="/fleet/resume"' in page, path
    # while paused, the dashboard offers resume instead of a second pause
    assert 'action="/fleet/pause"' not in client.get("/").text


def test_an_empty_allow_list_says_nothing_is_let_through(client):
    ops.pause_fleet()
    assert "Allowed through: nothing" in _text(client.get("/"))


def test_the_ramp_is_shown_when_one_is_in_force(client):
    central = CentralStore()
    try:
        central.set_state(fleet.REOPENED_KEY, str(time.time() - 60))
        central.set_state(fleet.BREAKER_KEY, str(time.time() - 60))
    finally:
        central.close()
    page = _text(client.get("/"))
    assert "RAMPING UP" in page
    assert f"at most {fleet.BREAKER_RAMP_CAP} worker turn in flight until" in page
    assert "breaker tripped" in page
    assert BANNER not in page


# -- the controls ----------------------------------------------------------------------


def test_the_pause_form_pauses_the_fleet(client):
    r = client.post("/fleet/pause", data={"reason": "  going to bed  ", "next": "/"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    pause = _pause()
    assert pause is not None and pause.reason == "going to bed" and not pause.allow


def test_a_second_pause_press_does_not_wipe_the_allow_list(client, project):
    """Re-pausing REPLACES the allow-list; a page rendered before the pause must not."""
    wo = _pending(project)
    ops.pause_fleet(reason="first", allow=[wo["id"]])
    r = client.post("/fleet/pause", data={"reason": "second", "next": "/"})
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    pause = _pause()
    assert pause.reason == "first" and pause.allow == {wo["id"]}


def test_let_through_adds_the_order_to_the_allow_list(client, project):
    wo = _pending(project)
    ops.pause_fleet()
    back = f"/wo/proj_a/{wo['id']}"
    page = client.get(back).text
    assert "Let this order through" in page

    r = client.post("/fleet/allow", data={"order_id": wo["id"], "next": back})
    assert r.status_code == 303 and r.headers["location"] == back
    assert _pause().allow == {wo["id"]}
    page = client.get(back).text
    assert "allowed through the pause" in page
    assert "Let this order through" not in page


def test_held_rows_in_the_listings_carry_the_button(client, project):
    wo = _pending(project)
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "running")  # a row on the dashboard, not a count
    finally:
        store.close()
    ops.pause_fleet()
    for path in ("/", "/project/proj_a"):
        page = client.get(path).text
        assert f'name="order_id" value="{wo["id"]}"' in page, path
    ops.resume_fleet([wo["id"]])
    for path in ("/", "/project/proj_a"):
        page = client.get(path).text
        assert f'name="order_id" value="{wo["id"]}"' not in page, path
        assert "allowed through the pause" in page, path


def test_no_let_through_button_while_the_fleet_runs(client, project):
    wo = _pending(project)
    assert 'action="/fleet/allow"' not in client.get(f"/wo/proj_a/{wo['id']}").text


def test_an_unknown_order_is_flashed_not_allowed(client):
    ops.pause_fleet()
    r = client.post("/fleet/allow", data={"order_id": "wo-nosuch00", "next": "/"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/?error=")
    assert _pause().allow == frozenset()
    page = client.get(r.headers["location"]).text
    assert "error-flash" in page and "wo-nosuch00" in page and "not found" in page


def test_let_through_when_not_paused_is_flashed(client, project):
    wo = _pending(project)
    r = client.post("/fleet/allow", data={"order_id": wo["id"], "next": "/"})
    page = client.get(r.headers["location"]).text
    assert "the fleet is not paused" in page
    assert _pause() is None


@pytest.mark.parametrize("kind,segment", [("feature", "fo"), ("improvement", "io"),
                                          ("investigation", "inv")])
def test_the_allow_list_links_each_order_kind_to_its_own_page(client, project, kind,
                                                              segment):
    """Issue #997 in the banner: the strip branched on `fo-`, so an allow-listed
    `io-`/`inv-` id fell through to `find_work_order`, resolved to None and lost its
    link entirely."""
    from jarvis.ui.app import _order_href

    store = ProjectStore(project)
    try:
        row = store.create_feature_order("held by the brake", kind=kind)
    finally:
        store.close()

    assert _order_href(row["id"]) == f"/{segment}/proj_a/{row['id']}"
    ops.pause_fleet(reason="spend", allow=[row["id"]])
    assert f'href="/{segment}/proj_a/{row["id"]}"' in client.get("/").text


def test_an_order_that_no_longer_resolves_has_no_link(client):
    """The function VALIDATES existence — a stale allow-list entry is text, not a 404."""
    from jarvis.ui.app import _order_href

    assert _order_href("io-nosuch00") is None
    assert _order_href("wo-nosuch00") is None


def test_resume_all_lifts_the_pause(client, project):
    ops.pause_fleet(reason="x")
    r = client.post("/fleet/resume", data={"next": "/project/proj_a"})
    assert r.status_code == 303 and r.headers["location"] == "/project/proj_a"
    assert _pause() is None
    assert BANNER not in client.get("/").text
    # and `jarvis status` agrees
    assert ops.os_status()["fleet"]["paused"] is False


def test_the_controls_will_not_redirect_off_site(client):
    r = client.post("/fleet/pause", data={"next": "//evil.example/"})
    assert r.headers["location"] == "/"
    r = client.post("/fleet/resume", data={"next": "https://evil.example/"})
    assert r.headers["location"] == "/"
