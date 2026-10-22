"""Moving through the console — `/go/<id>` (docs/superpowers/specs/2026-10-22-a-console-you-can-move-through.md)"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from jarvis import holds, ops  # noqa: E402
from tests.test_ui_debug import client, dispatched, started  # noqa: E402,F401


# -- §1. one resolver for every id the OS mints

def test_go_sends_a_work_order_id_to_its_page(client, dispatched):  # noqa: F811
    wo_id = dispatched["wo_id"]
    r = client.get(f"/go/{wo_id}")
    assert r.status_code == 303
    assert r.headers["location"] == f"/wo/proj_a/{wo_id}"


def test_go_sends_a_feature_order_through_order_path(client):  # noqa: F811
    fo = ops.create_feature_order("proj_a", "a big ask", description="lots")
    assert client.get(f"/go/{fo['id']}").headers["location"] == f"/fo/proj_a/{fo['id']}"


@pytest.mark.parametrize("ref, where", [
    ("q12", "/neo/question/12"),
    ("12", "/neo/question/12"),
    ("gate-7", "/gates#gate-7"),
    ("kn-0123abcd", "/knowledge#kn-0123abcd"),
    ("mv-0123abcd", "/morpheus#mv-0123abcd"),
    ("bl-0123abcd", "/backlog#bl-0123abcd"),
])
def test_go_resolves_the_numbered_and_fleet_wide_ids(client, ref, where):  # noqa: F811
    assert client.get(f"/go/{ref}").headers["location"] == where


def test_an_id_that_resolves_nowhere_lands_on_search_not_a_404(client):  # noqa: F811
    r = client.get("/go/wo-deadbeef")
    assert r.status_code == 303
    assert r.headers["location"] == "/search?q=wo-deadbeef"
    assert client.get("/go/issue-42").headers["location"] == "/search?q=%2342"


# -- §2. every page about one order carries the same strip

def test_every_page_about_an_order_carries_its_nav(client, dispatched):  # noqa: F811
    wo_id = dispatched["wo_id"]
    for path in (f"/wo/proj_a/{wo_id}", f"/wo/proj_a/{wo_id}/debug",
                 f"/wo/proj_a/{wo_id}/prompt", f"/cost/proj_a/{wo_id}"):
        page = client.get(path).text
        assert 'class="order-nav"' in page, path
        assert f'href="/wo/proj_a/{wo_id}/prompt"' in page or "prompts</span>" in page, path
        assert "/config?scope=projects.proj_a" in page, path
        assert 'href="/project/proj_a/prompt"' in page, path


def test_the_sidebar_lists_the_registered_projects(client):  # noqa: F811
    assert 'href="/project/proj_a"' in client.get("/inbox").text


# -- §3. a hold links to what held it

def test_a_hold_carries_the_key_it_was_paired_on_through_every_cut():
    events = [{"kind": "question_asked", "ts": 100.0, "payload": '{"neo_question_id": 41}'},
              {"kind": "neo_answered", "ts": 160.0, "payload": '{"neo_question_id": 41}'}]
    [hold] = holds._episodes(events)
    assert hold.key == 41
    # the key rides along without changing what a hold IS
    assert hold == holds.Hold(holds.NEO_QUESTION, 100.0, 160.0)
    # ...and survives a working turn cutting it in two, and the merge
    cut = holds._merge(holds._outside([hold], [(120.0, 130.0)], 200.0), 200.0)
    assert [h.key for h in cut] == [41, 41]


@pytest.mark.parametrize("cause, key, href", [
    (holds.NEO_QUESTION, 41, "/neo/question/41"),
    (holds.GATE, 7, "/gates#gate-7"),
    (holds.VALIDATION, 2, "#round-2"),
    (holds.MESSAGE, 9, "#msg-9"),
])
def test_the_debug_page_links_each_hold_to_what_held_it(
        client, dispatched, monkeypatch, cause, key, href):  # noqa: F811
    wo_id = dispatched["wo_id"]
    real = ops.diagnose

    def with_a_hold(*args, **kwargs):
        payload = real(*args, **kwargs)
        payload["holds"]["episodes"] = [{
            "cause": cause, "phrase": holds.HOLD_CAUSES[cause], "key": key,
            "open": False, "seconds": 60.0, "seconds_human": "1m",
            "started": 0.0, "ended": 60.0}]
        return payload

    monkeypatch.setattr(ops, "diagnose", with_a_hold)
    page = client.get(f"/wo/proj_a/{wo_id}/debug").text
    assert href in page


# -- §4. the prompts, on screen

def test_an_order_s_prompts_page_shows_every_turn_verbatim(client, dispatched):  # noqa: F811
    wo_id = dispatched["wo_id"]
    turns = dispatched["store"].list_turns(wo_id)
    page = client.get(f"/wo/proj_a/{wo_id}/prompt")
    assert page.status_code == 200
    for t in turns:
        assert f'id="turn-{t["seq"]}"' in page.text
    assert "measure me" in page.text   # the order's title is in the prompt it was sent


def test_the_project_prompt_preview_is_composed_by_dispatch(client):  # noqa: F811
    page = client.get("/project/proj_a/prompt")
    assert page.status_code == 200
    assert ops.PREVIEW_WO["id"] in page.text
    assert "OPERATION.md" in page.text


def test_an_unknown_project_s_prompt_is_a_404_not_a_500(client):  # noqa: F811
    assert client.get("/project/nope/prompt").status_code == 404


# -- §5. the Neo question links back to where it came from

def test_a_neo_question_links_to_its_order_and_its_debug_page(client, dispatched):  # noqa: F811
    wo_id = dispatched["wo_id"]
    q = ops.ask_question(wo_id, "reuse the session?", "proj_a")
    page = client.get(f"/neo/question/{q['question_id']}").text
    assert f'/wo/proj_a/{wo_id}#q-{q["question_id"]}' in page
    assert f"/wo/proj_a/{wo_id}/debug#dbg-held" in page
