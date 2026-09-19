"""Money the budget accounting used to lose, and the two places it lost it (issue #471).

`budget.spent` is `SUM(wo_turns.cost_usd)`, and `finish_turn` SETs that column. So every
settle that omitted a cost wrote NULL over a turn that had really spent money, and the
one late repair the OS had (`ops._turn_usage` -> `set_turn_usage`) rewrote `usage_json`
and left the column alone. A row could say it cost $4.19 and be summed as $0.00 by the
query the enforcement runs on every dispatch.

Observed on wo-2d259874 (production, jarvis-0.10.10). None of it was covered, because
`test_budget.py`'s `bill_the_turn` helper opens a turn and settles it in the same breath
with a cost always supplied: no test had ever asked the accounting a question about a
turn reaped WITHOUT an envelope.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from jarvis import budget, ops, usage, worker_session
from jarvis.project_store import (
    COST_FROM_ENVELOPE,
    COST_FROM_TRANSCRIPT,
    ProjectStore,
)


SESSION = "sess-blind-spot"


def stamp(ts: float) -> str:
    """A unix time in the RFC-3339-with-a-Z shape Claude Code writes."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def call_row(at: float, *, write: int = 0, read: int = 0, out: int = 0,
             mid: str = "msg-1", model: str = "claude-opus-5") -> dict:
    return {
        "type": "assistant",
        "timestamp": stamp(at),
        "message": {"id": mid, "model": model, "usage": {
            "input_tokens": 0,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
            "output_tokens": out,
        }},
    }


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict]) -> None:
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return write


@pytest.fixture()
def store(jarvis_home, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    s = ProjectStore(project)
    yield s
    s.close()


def a_turn_that_ran(store: ProjectStore, wo_id: str) -> dict:
    """An open turn that started a minute ago, with one $25 API call inside it.

    Back-dated rather than started now, so the window is unambiguous: a call row's
    timestamp is a microsecond-rounded round trip through RFC-3339, and one written at
    exactly `started_at` lands on either side of it depending on the fraction.
    """
    turn = store.create_turn(wo_id, kind="dispatch", prompt="work")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (time.time() - 60, turn["id"]))
    return store.get_turn(turn["id"])


def an_order(store: ProjectStore, budget_usd: float = 30.0) -> dict:
    """A budgeted work order whose session has a transcript to read."""
    wo = ops.create_work_order("proj_a", "capped", description="do it",
                               budget_usd=budget_usd)
    store.update_work_order(wo["id"], session_id=SESSION)
    return store.get_work_order(wo["id"])


# -- the window ------------------------------------------------------------------------


def test_cost_between_prices_only_the_calls_inside_the_turn(transcripts):
    """A session outlives the turn that died in it, so the window is the whole point."""
    transcripts(SESSION, [
        call_row(1_000.0, out=1_000_000, mid="before"),
        call_row(2_000.0, out=1_000_000, mid="inside"),
        call_row(3_000.0, out=1_000_000, mid="after"),
    ])
    one_call = usage.cost_between(SESSION, 1_900.0, 2_100.0)
    assert one_call == pytest.approx(usage.price_for("claude-opus-5")[1])
    assert usage.cost_between(SESSION, 900.0, 3_100.0) == pytest.approx(3 * one_call)


def test_cost_between_is_zero_when_the_transcript_is_gone(transcripts):
    """An absence, not a guess: the caller leaves `cost_usd` NULL rather than write 0."""
    assert usage.cost_between("no-such-session", 0.0, 9e9) == 0.0


# -- the turn nobody could bill --------------------------------------------------------


def test_a_turn_reaped_without_an_envelope_is_billed_from_the_transcript(
        store, transcripts):
    """A turn killed by a reboot, an OOM, a machine bounce or a cancel wrote no
    `total_cost_usd`. It spent the money anyway, and the transcript still has the calls.
    """
    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])

    settled = worker_session._reap(store, turn, "proj_a")

    assert settled["state"] == "failed"
    assert settled["cost_usd"] == pytest.approx(usage.price_for("claude-opus-5")[1])
    assert settled["cost_source"] == COST_FROM_TRANSCRIPT
    assert budget.spent(store, None, wo["id"]).total_usd == pytest.approx(
        settled["cost_usd"])


def test_a_cancelled_turn_is_billed_the_same_way(store, transcripts):
    """`cancel` is the other `finish_turn` call site that omitted the cost, and it is
    the one aimed at a turn that is certainly running and certainly spending."""
    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])

    worker_session.cancel(store, wo["id"])

    settled = store.get_turn(turn["id"])
    assert settled["cost_usd"] == pytest.approx(usage.price_for("claude-opus-5")[1])
    assert settled["cost_source"] == COST_FROM_TRANSCRIPT


def test_a_turn_that_died_before_its_first_api_call_records_no_cost(store, transcripts):
    """Zero is not the same claim as "nothing on record", and NULL is the honest one."""
    wo = an_order(store)
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="work")
    transcripts(SESSION, [])

    settled = worker_session._reap(store, turn, "proj_a")

    assert settled["cost_usd"] is None
    assert settled["cost_source"] is None


# -- the late repair -------------------------------------------------------------------


def test_the_late_usage_repair_repairs_the_cost_too(store):
    """`ops._turn_usage` re-reads a settled turn's outfile to backfill `usage_json`.
    That envelope carries `total_cost_usd`, and the column the enforcement sums is the
    other one."""
    wo = an_order(store, budget_usd=5.0)
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="work")
    store.finish_turn(turn["id"], "failed", error="no result in the outfile")
    assert store.get_turn(turn["id"])["cost_usd"] is None

    store.set_turn_usage(turn["id"], json.dumps({"total_cost_usd": 4.19}))

    assert budget.spent(store, None, wo["id"]).total_usd == pytest.approx(4.19)
    assert store.get_turn(turn["id"])["cost_source"] == COST_FROM_ENVELOPE


def test_the_repair_prefers_the_envelope_over_the_transcript_floor(store, transcripts):
    """The floor is what the OS has until the CLI's own figure turns up. When it does,
    it wins — it is the exact one, and it is what `jarvis cost` reports."""
    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])
    worker_session._reap(store, turn, "proj_a")

    store.set_turn_usage(turn["id"], json.dumps({"total_cost_usd": 4.19}))

    assert store.get_turn(turn["id"])["cost_usd"] == pytest.approx(4.19)
    assert store.get_turn(turn["id"])["cost_source"] == COST_FROM_ENVELOPE


def test_an_envelope_with_no_cost_does_not_erase_one_already_known(store, transcripts):
    """Coalesced, not set. A repair that nulls the column is the bug it exists to fix."""
    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])
    worker_session._reap(store, turn, "proj_a")
    floor = store.get_turn(turn["id"])["cost_usd"]

    store.set_turn_usage(turn["id"], json.dumps({"input": 10, "output": 5}))

    assert store.get_turn(turn["id"])["cost_usd"] == pytest.approx(floor)
    assert store.get_turn(turn["id"])["cost_source"] == COST_FROM_TRANSCRIPT


def test_a_settled_turn_says_which_reading_billed_it(store):
    """The two figures are not the same currency, so no surface has to guess."""
    wo = an_order(store)
    turn = store.create_turn(wo["id"], kind="message", prompt="work")
    store.finish_turn(turn["id"], "done", result="done", cost_usd=1.25)
    assert store.get_turn(turn["id"])["cost_source"] == COST_FROM_ENVELOPE


# -- the turn in flight ----------------------------------------------------------------
#
# DISPLAY ONLY, ruled by Neo on question 469. The enforcement stays on the two indexed
# queries: the real ceiling is applied at launch (`--max-budget-usd = cap - spent`), and
# a list-price estimate must never be the thing that parks an order and kills a worker
# mid-turn.


def test_the_budget_line_adds_the_turn_that_is_burning_money_now(store, transcripts):
    """The bug as reported: `Budget: $0.00 of $30.00 · $30.00 left` printed directly
    under a bill chip reading `~$3.25`, both current."""
    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])

    shown = ops.work_order_budget(wo["id"], "proj_a")

    assert shown["spent_usd"] == 0.0            # nothing has settled...
    assert shown["in_flight_usd"] == pytest.approx(25.0)   # ...and $25 is already gone
    assert shown["live_spent_usd"] == pytest.approx(25.0)
    assert shown["live_remaining_usd"] == pytest.approx(5.0)


def test_the_enforcement_does_not_see_the_turn_in_flight(store, transcripts):
    """The other half of the same ruling, and the one that has to be pinned: a running
    order must not be parked, nor its next ceiling cut, by an estimate."""
    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])

    assert budget.spent(store, None, wo["id"]).total_usd == 0.0
    cap = budget.ceiling(store, None, store.get_work_order(wo["id"]))
    assert cap.spent_usd == 0.0
    assert budget.exhaustion(store, None, store.get_work_order(wo["id"])) is None


def test_an_order_with_no_turn_in_flight_shows_the_recorded_figure_alone(store):
    """No estimate, no `~`: the recorded total is the whole truth once a turn settles."""
    wo = an_order(store)
    turn = store.create_turn(wo["id"], kind="message", prompt="work")
    store.finish_turn(turn["id"], "done", result="done", cost_usd=2.50)

    shown = ops.work_order_budget(wo["id"], "proj_a")

    assert shown["in_flight_usd"] == 0.0
    assert shown["live_spent_usd"] == pytest.approx(shown["spent_usd"]) == 2.50


def test_the_page_marks_the_live_figure_as_an_estimate(store, transcripts, project):
    """`~` on the page, because half of that number is a list-price estimate and the
    reader is deciding whether to raise a ceiling with it."""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo = an_order(store)
    turn = a_turn_that_ran(store, wo["id"])
    transcripts(SESSION, [call_row(turn["started_at"] + 30, out=1_000_000)])

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text

    assert ">~$25.00</span> of $30.00" in page
    assert "~$5.00 left" in page
