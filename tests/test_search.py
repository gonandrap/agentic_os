"""`jarvis search`, its fan-out and the dashboard page — docs/superpowers/specs/
2026-09-23-artifact-search.md.

THE POINT OF THE VERB IS THE SETTLED RECORD, so every fixture here closes the thing it
files: a search that only finds live work is the surface the user already had. The
strings are distinctive for the reason tests/test_alarm_review.py says — a page is
proved to be rendering the hit, not echoing the query.
"""

from __future__ import annotations

import pytest

from jarvis import ops, search
from jarvis.cli import build_parser
from jarvis.central_store import CentralStore
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

LOGIN = "the login spinner never stops on a cold cache"
NOWHERE = "no fixture in this module says this sentence anywhere"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)


def _wo(title, description="", *, status="completed", summary=""):
    wo = ops.create_work_order("proj_a", title, description)
    path = ops.find_work_order(wo["id"])[1]
    store = ProjectStore(path)
    try:
        store.set_status(wo["id"], status, result_summary=summary)
    finally:
        store.close()
    return wo["id"]


def _client():
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def test_finds_a_completed_work_order(started):
    """The order this feature exists for: settled, off every listing's first page."""
    wo_id = _wo("fix the login spinner", LOGIN, summary="cache key was wrong")
    hits = search.search("login spinner")
    assert [h["id"] for h in hits] == [wo_id]
    hit = hits[0]
    assert hit["kind"] == "work_order"
    assert hit["status"] == "completed"
    assert hit["project"] == "proj_a"
    assert hit["url"] == f"/wo/proj_a/{wo_id}"
    assert hit["ref"] == f"jarvis wo show {wo_id}"


def test_a_hidden_order_is_still_findable(started):
    """`wo hide` drops an order from LISTINGS. Search is retrieval, not a listing."""
    wo_id = _wo("fix the login spinner", LOGIN)
    ops.hide_work_order(wo_id)
    assert [h["id"] for h in search.search("spinner")] == [wo_id]


def test_title_outranks_a_body_mention(started):
    """Weighting is the whole difference between a ranking and a pile."""
    body = _wo("something else entirely", f"while doing it, {LOGIN}")
    titled = _wo("the login spinner itself")
    hits = search.search("login spinner")
    assert [h["id"] for h in hits] == [titled, body]


def test_word_or_not_phrase(started):
    """Two of three words is the hit the user meant (kn-c6e8fbf0)."""
    wo_id = _wo("the login spinner")
    assert [h["id"] for h in search.search("login spinner cache")] == [wo_id]
    assert search.search(NOWHERE) == []


def test_an_id_typed_in_full_is_a_jump(started):
    """An exact id outranks a row that merely mentions it."""
    target = _wo("a quiet title")
    _wo("a work order about another one", f"follows on from {target}")
    assert search.search(target)[0]["id"] == target


def test_empty_query_returns_nothing(started):
    """A search verb is not a listing: no query means no answer, not everything."""
    _wo("fix the login spinner")
    assert search.search("   ") == []


def _one_of_every_kind():
    """File one record of each searchable kind, all saying "login spinner"."""
    wo_id = _wo("fix the login spinner")
    path = ops.find_work_order(wo_id)[1]
    store = ProjectStore(path)
    try:
        fo = store.create_feature_order("rebuild the login spinner", "a big one")
        alarm = store.add_alarm(wo_id, "long-turn", 1, "login spinner turn is burning")
        store.conn.execute(
            "INSERT INTO approvals (wo_id, ts, kind, command, justification) "
            "VALUES (?, ?, ?, ?, ?)",
            (wo_id, 1.0, "pr_merge", "gh pr merge 7", "the login spinner fix"))
        gate_id = str(store.conn.execute("SELECT MAX(id) AS m FROM approvals")
                      .fetchone()["m"])
    finally:
        store.close()
    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "retire the login spinner")
        kn = central.add_knowledge("the login spinner is driven by a cold cache",
                                   project="proj_a", topic="ui")
    finally:
        central.close()
    neo = NeoStore()
    try:
        qid = neo.ask("proj_a", wo_id, "should the login spinner block the page?")["id"]
        neo.record_answer(qid, "no, the spinner is cosmetic")
    finally:
        neo.close()
    return {"work_order": wo_id, "feature_order": fo["id"], "alarm": alarm["id"],
            "gate": gate_id, "backlog": item["id"], "neo_question": str(qid),
            "knowledge": kn["id"]}


def test_every_kind_is_searched(started):
    """The six kinds the order named, plus knowledge (Neo question 528)."""
    filed = _one_of_every_kind()
    hits = search.search("login spinner")
    assert {h["kind"]: h["id"] for h in hits} == filed
    assert dict(search.counts(hits))["work_order"] == 1


def test_every_hit_is_actionable_from_either_surface(started):
    """A hit carries a page that renders and a `jarvis` command that exists — for EVERY
    kind, not just the work order. Spec section 4."""
    filed = _one_of_every_kind()
    client = _client()
    parser = build_parser()
    seen = set()
    for hit in search.search("login spinner"):
        seen.add(hit["kind"])
        assert client.get(hit["url"].split("#")[0]).status_code == 200, hit
        words = hit["ref"].split()
        assert words[0] == "jarvis" and words[-1] == filed[hit["kind"]], hit
        parser.parse_args(words[1:])   # the command the hit prints is a real one
    assert seen == set(search.KINDS)


def test_kind_filter_and_unknown_kind(started):
    _wo("fix the login spinner")
    assert search.search("login", kinds=("backlog",)) == []
    with pytest.raises(ValueError):
        search.search("login", kinds=("nonsense",))


def test_project_scope(started, tmp_path, jarvis_home):
    """The same verb, narrowed to the project the user is looking at."""
    _wo("fix the login spinner")
    central = CentralStore()
    try:
        central.upsert_project("proj_b", str(tmp_path / "proj_b"))
        other = central.add_backlog("proj_b", "the login spinner, elsewhere")
    finally:
        central.close()
    assert {h["id"] for h in search.search("login spinner", project="proj_b")} \
        == {other["id"]}
    with pytest.raises(ops.OpsError):
        search.search("login", project="proj_nope")


def test_snippet_shows_the_match_not_the_opening(started):
    _wo("a quiet title", "x" * 400 + f" {LOGIN}")
    snippet = search.search("spinner")[0]["snippet"]
    assert "spinner" in snippet
    assert snippet.startswith("…")


def test_cli_prints_hits_and_counts(started, capsys):
    from jarvis.cli import main

    wo_id = _wo("fix the login spinner", summary="cache key was wrong")
    assert main(["search", "login spinner"]) == 0
    out = capsys.readouterr().out
    assert "work_order 1" in out
    assert wo_id in out
    assert f"jarvis wo show {wo_id}" in out

    assert main(["search", NOWHERE]) == 0
    assert "nothing matches" in capsys.readouterr().out


def test_cli_json(started, capsys):
    import json

    from jarvis.cli import main

    wo_id = _wo("fix the login spinner")
    assert main(["search", "spinner", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["id"] == wo_id


def test_page_renders_hits(started):
    wo_id = _wo("fix the login spinner", LOGIN)
    page = _client().get("/search", params={"q": "login spinner"}).text
    assert wo_id in page
    assert f"/wo/proj_a/{wo_id}" in page
    assert NOWHERE not in page


def test_page_is_scoped_by_project(started, tmp_path):
    wo_id = _wo("fix the login spinner")
    central = CentralStore()
    try:
        central.upsert_project("proj_b", str(tmp_path / "proj_b"))
        central.add_backlog("proj_b", "the login spinner, elsewhere")
    finally:
        central.close()
    page = _client().get("/search", params={"q": "login spinner",
                                            "project": "proj_b"}).text
    assert wo_id not in page
    assert "elsewhere" in page


def test_page_survives_a_project_that_does_not_exist(started):
    """A deep link the user edited is not a 500: the scope falls back to the fleet."""
    _wo("fix the login spinner")
    page = _client().get("/search", params={"q": "spinner", "project": "ghost"})
    assert page.status_code == 200
    assert "Something went wrong" not in page.text


def test_the_box_is_on_every_page(started):
    """Search reachable without knowing it exists — the UX complaint, precisely."""
    client = _client()
    for path in ("/", f"/project/proj_a", "/backlog"):
        page = client.get(path).text
        assert 'action="/search"' in page
    project_page = client.get("/project/proj_a").text
    assert 'name="project" value="proj_a"' in project_page
