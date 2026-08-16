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


def add_recorded_turn(project, wo_id: str, cost: float, peak: int,
                      window: int = 1_000_000) -> None:
    """A settled turn with its recorded usage envelope, as `_reap` would leave it."""
    store = ProjectStore(project)
    try:
        kind = "message" if store.list_turns(wo_id) else "dispatch"
        turn = store.create_turn(wo_id, kind=kind, prompt="p")
        usage = {"total_cost_usd": cost, "input": 2, "cache_write": 2558,
                 "cache_read": 45689, "cache_1h": 2558, "cache_5m": 0, "output": 941,
                 "api_calls": 1, "context_peak": peak, "context_window": window,
                 "duration_api_ms": 1000, "cost_by_model": {"claude-opus-5": cost}}
        store.finish_turn(turn["id"], "done", result="r", cost_usd=cost, num_turns=1,
                          usage_json=json.dumps(usage))
    finally:
        store.close()


def test_cost_page_links_each_work_order_to_its_turn_drilldown(client, project,
                                                               transcript):
    wo = ops.create_work_order("proj_a", "drillable")
    transcript("sess-drill", write_tok=1_000, out=10)
    give_session(project, wo["id"], "sess-drill")

    page = client.get("/cost")
    assert page.status_code == 200
    assert f'/cost/proj_a/{wo["id"]}' in page.text


def test_the_drilldown_shows_the_turn_table_and_context_growth(client, project):
    """The page this feature exists for: a bloated work order's cost curve, turn by
    turn — each turn's own cost and its context peak against the model's window."""
    wo = ops.create_work_order("proj_a", "bloating one")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    add_recorded_turn(project, wo["id"], 0.07, 90_000)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "exact" in page.text                       # provenance: recorded
    assert "dispatch" in page.text and "message" in page.text
    assert "9.0%" in page.text                        # /context occupancy, turn 2
    assert "48k" in page.text and "90k" in page.text  # peak per turn: growth visible


def test_the_drilldown_labels_a_transcript_only_work_order_an_estimate(
        client, project, transcript):
    """The fallback path must never dress an estimate up as the record."""
    wo = ops.create_work_order("proj_a", "pre capture")
    transcript("sess-pre", write_tok=200_000, out=10_000)
    give_session(project, wo["id"], "sess-pre")

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "estimate" in page.text


# -- what Jarvis itself spent ---------------------------------------------------------
#
# The half of the bill that had no surface at all: Neo answering the work order's
# questions, the panel's seats deliberating on them, the digest shortening the result.
# The page has to show the TOTAL and the SPLIT — a total alone hides that the OS spends
# on a work order at all, and a worker figure alone reads as the whole bill.


def add_os_calls(wo_id: str, *, seats: int = 0, neo_calls: int = 0,
                 output: int = 1_000) -> None:
    """Record OS-side calls against a work order, as the daemon's would be."""
    from jarvis import agent_usage

    for i in range(neo_calls):
        agent_usage.record("neo_answer", project="proj_a", wo_id=wo_id,
                           label="question", model="claude-opus-5", question_id=i + 1,
                           usage={"total_cost_usd": 0.02, "input": 10,
                                  "cache_write": 5_000, "cache_read": 20_000,
                                  "output": output})
    for seat in ("premise", "record", "blast", "taste", "chair")[:seats]:
        agent_usage.record("panel_seat", project="proj_a", wo_id=wo_id, label=seat,
                           model="claude-opus-5", question_id=1,
                           usage={"total_cost_usd": 0.01, "input": 10,
                                  "cache_write": 2_000, "cache_read": 8_000,
                                  "output": output})


def test_the_cost_page_splits_the_fleet_total_into_workers_and_jarvis(
        client, project, transcript):
    wo = ops.create_work_order("proj_a", "an ask that asked back")
    transcript("sess-split", write_tok=100_000, out=5_000)
    give_session(project, wo["id"], "sess-split")
    add_os_calls(wo["id"], neo_calls=2, seats=5)

    page = client.get("/cost")
    assert page.status_code == 200
    assert "workers ~$" in page.text and "jarvis ~$" in page.text
    assert "7 calls" in page.text
    # And per work order, with what the spend went ON: five seats is a shape a total
    # can never make visible.
    assert "5 panel seat" in page.text and "2 Neo answering" in page.text


def test_a_work_order_with_no_transcript_still_shows_what_jarvis_spent_on_it(
        client, project):
    """The OS's own calls are the OS's own record. They do not depend on a file Claude
    Code is free to prune, so pruning must not hide them."""
    wo = ops.create_work_order("proj_a", "pruned but not free")
    give_session(project, wo["id"], "sess-long-gone")
    add_os_calls(wo["id"], neo_calls=3)

    fleet = client.get("/cost")
    # "$x+": the jarvis half is known, the worker half is not — never a bare total that
    # would read as the whole bill.
    assert "+" in fleet.text and "no transcript left to measure" in fleet.text
    assert "jarvis ~$" in fleet.text

    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert "jarvis" in page.text and "3 calls" in page.text
    assert "not measurable" in page.text


def test_the_work_order_page_shows_the_split_not_just_the_total(client, project,
                                                                transcript):
    wo = ops.create_work_order("proj_a", "priced with help")
    transcript("sess-helped", write_tok=200_000, out=10_000)
    give_session(project, wo["id"], "sess-helped")
    add_os_calls(wo["id"], neo_calls=1)

    page = client.get(f"/wo/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "worker" in page.text and "jarvis" in page.text and "1 call" in page.text


def test_the_drilldown_lists_jarvis_own_calls_one_row_per_seat(client, project):
    """The table that answers "why did an order I never touched cost three dollars"."""
    wo = ops.create_work_order("proj_a", "panelled")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)
    add_os_calls(wo["id"], seats=5, neo_calls=1)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "What jarvis itself spent on" in page.text
    for seat in ("premise", "record", "blast", "taste", "chair"):
        assert seat in page.text
    assert "panel seat 5" in page.text


def test_the_drilldown_says_so_when_jarvis_spent_nothing(client, project):
    """Zero here is a real answer — the work order asked Neo nothing — and it is worth
    saying, because the reader's next question is where the rest went."""
    wo = ops.create_work_order("proj_a", "self-sufficient")
    add_recorded_turn(project, wo["id"], 0.05, 48_000)

    page = client.get(f"/cost/proj_a/{wo['id']}")
    assert page.status_code == 200
    assert "asked Neo nothing" in page.text


def test_the_cost_tab_is_reachable_from_every_page(client):
    """A surface nobody can find is the bug this work order was filed about."""
    assert '<a href="/cost"' in client.get("/").text
    assert 'class="here"' in client.get("/cost").text
