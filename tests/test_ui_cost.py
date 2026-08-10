"""The dashboard's spend surfaces: the `/cost` page and the per-work-order line.

`jarvis cost` shipped with no dashboard surface at all, so the one question the feature
exists to answer — where did my tokens go — could only be asked from a terminal.
`tests/test_usage.py` covers the transcript parser and `tests/test_cost_report.py` the
attribution; what is left here is mostly the ways a spend figure can LIE. A pruned
transcript rendered as zero turns a gap in the evidence into a claim about the spend, and
a page that 500s reading a file Jarvis does not own takes a decision surface down with
it.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops, usage  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def transcript(tmp_path, monkeypatch):
    """Write a fake Claude Code transcript and point `usage` at it."""
    root = tmp_path / "transcripts"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, *, write_tok: int = 0, read: int = 0, out: int = 0):
        row = {"type": "assistant",
               "message": {"id": f"m-{session_id}", "model": "claude-opus-5",
                           "usage": {"input_tokens": 0,
                                     "cache_creation_input_tokens": write_tok,
                                     "cache_read_input_tokens": read,
                                     "output_tokens": out}}}
        (root / "-proj" / f"{session_id}.jsonl").write_text(json.dumps(row) + "\n")

    return write


def give_session(project, wo_id: str, session_id: str) -> None:
    """Attach a session to a work order — the join `cost_report` reads spend through."""
    store = ProjectStore(project)
    try:
        store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                           (session_id, wo_id))
    finally:
        store.close()


def test_cost_page_lists_work_orders_dearest_first(client, project, transcript):
    cheap = ops.create_work_order("proj_a", "a small ask")
    dear = ops.create_work_order("proj_a", "a large ask")
    transcript("sess-cheap", write_tok=1_000, out=100)
    transcript("sess-dear", write_tok=900_000, out=50_000)
    give_session(project, cheap["id"], "sess-cheap")
    give_session(project, dear["id"], "sess-dear")

    page = client.get("/cost")
    assert page.status_code == 200
    assert page.text.index("a large ask") < page.text.index("a small ask")
    # Said on the page, not only in the docs: the user is on a subscription, so a bare
    # dollar figure is most likely to be misread as an invoice.
    assert "not a bill" in page.text


def test_cost_page_says_when_a_transcript_is_gone_instead_of_saying_zero(
        client, project, transcript):
    """An unmeasurable cost and a zero cost are different answers.

    Rendering them the same is the one thing this page must never do: someone opens it
    precisely to find out where the bill came from, and a silent omission invites a
    conclusion the evidence does not support.
    """
    wo = ops.create_work_order("proj_a", "measured long ago")
    give_session(project, wo["id"], "sess-that-was-pruned")

    page = client.get("/cost")
    assert page.status_code == 200
    assert "no transcript left to measure" in page.text
    assert "1 whose transcript Claude Code has" in page.text


def test_cost_page_can_be_scoped_to_one_project(client, project, transcript):
    wo = ops.create_work_order("proj_a", "scoped ask")
    transcript("sess-scoped", write_tok=1_000, out=100)
    give_session(project, wo["id"], "sess-scoped")

    assert "scoped ask" in client.get("/cost?project=proj_a").text
    unknown = client.get("/cost?project=nope")
    assert unknown.status_code == 200
    assert "not registered" in unknown.text


def test_the_work_order_page_shows_what_that_work_order_cost(client, project,
                                                             transcript):
    wo = ops.create_work_order("proj_a", "priced task")
    transcript("sess-priced", write_tok=200_000, out=10_000)
    give_session(project, wo["id"], "sess-priced")

    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "200k in / 10k out" in page.text
    assert "1 turn" in page.text


def test_a_work_order_with_no_transcript_shows_no_cost_line_at_all(client, project,
                                                                   transcript):
    """Not "$0.00" — the work order has not been dispatched, so nothing is known."""
    wo = ops.create_work_order("proj_a", "never dispatched")
    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "fleet →" not in page.text


def test_a_broken_transcript_read_never_takes_the_work_order_page_down(
        client, project, monkeypatch):
    """The work order page carries the gate decision and the assumption review.

    A spend figure is the least important thing on it, so a failure reading files Jarvis
    does not own — Claude Code prunes them on its own schedule and owns their format —
    has to cost the line, not the page.
    """
    wo = ops.create_work_order("proj_a", "still readable")

    def boom(*args, **kwargs):
        raise OSError("transcript root vanished mid-read")

    monkeypatch.setattr(ops, "cost_report", boom)
    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "still readable" in page.text
    assert "fleet →" not in page.text


def test_the_cost_tab_is_reachable_from_every_page(client):
    """A surface nobody can find is the bug this work order was filed about."""
    assert '<a href="/cost"' in client.get("/").text
    assert 'class="here"' in client.get("/cost").text
