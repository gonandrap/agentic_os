"""Renders of the bill, captured as files, so the surface can be reviewed and not
just asserted about.

Every other test here asks "is the arithmetic right". This one asks the question a
reviewer actually has — "show me the page" — and writes the answer to
`$JARVIS_BILL_EVIDENCE`. It is skipped unless that is set, so it costs the suite
nothing; run it with the variable pointed at a directory and the HTML lands there.

It runs inside the ordinary test fixtures, which means it inherits the isolation gate:
no live state, no live spend database, nothing but a temporary home.
"""

from __future__ import annotations

import json
import os

import pytest

from jarvis import agent_usage, ops
from jarvis.ui.app import create_app

from tests.test_cost_report import (  # noqa: F401
    add_turn, assistant_row, give_session, recorded_usage, registered, store,
    transcripts,
)

EVIDENCE = os.environ.get("JARVIS_BILL_EVIDENCE", "")

pytestmark = pytest.mark.skipif(not EVIDENCE, reason="set JARVIS_BILL_EVIDENCE=<dir>")


def test_render_a_bill_with_every_actor_on_it(store, registered, transcripts, capsys):
    """One order that spent in all three ways, rendered to HTML and to the terminal.

    The OS half is the half the user could not see: a Neo answer, five panel seats, a
    digest and the worker's own subprocesses, each on its own row and each expandable.
    """
    from fastapi.testclient import TestClient

    wo = store.create_work_order("what an order with every kind of spend costs", "")
    give_session(store, wo["id"], "sess-evidence")
    transcripts(
        "sess-evidence",
        [assistant_row("m1", write=226_000, read=3_000_000, out=47_000, at=1_001)],
        subagents=[([assistant_row("s1", write=129_000, read=2_000_000, out=34_000,
                                   at=1_005)],
                    {"agentType": "jarvis-architect",
                     "description": "Architect: validation panel decomposition"})])
    # The turn's own envelope COVERS the subagent, which is the real relationship —
    # `modelUsage` counts every model call the turn made — so the transcript and the
    # recorded turn agree and the bill has no gap line to explain.
    turn = add_turn(store, wo["id"], dict(recorded_usage(4.43), usage_v=2, input=82,
                                          cache_write=226_000 + 129_000,
                                          cache_read=3_000_000 + 2_000_000,
                                          output=47_000 + 34_000))
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (1_000.0, 1_800.0, turn["id"]))
    store.conn.commit()

    agent_usage.record("neo_answer", project="proj_a", wo_id=wo["id"], label="question",
                       model="claude-opus-5", question_id=121,
                       usage={"total_cost_usd": 0.0431, "input": 12,
                              "cache_write": 18_204, "cache_read": 121_880,
                              "output": 1_842})
    for seat, out in (("premise", 1_120), ("record", 980), ("blast", 1_640),
                      ("taste", 760), ("chair", 2_010)):
        agent_usage.record("panel_seat", project="proj_a", wo_id=wo["id"], label=seat,
                           model="claude-opus-5", question_id=121,
                           usage={"total_cost_usd": 0.0288, "input": 9,
                                  "cache_write": 12_400, "cache_read": 88_300,
                                  "output": out})
    agent_usage.record("digest", project="proj_a", wo_id=wo["id"], label="",
                       model="claude-haiku-4-5-20251001",
                       usage={"total_cost_usd": 0.0019, "input": 4,
                              "cache_write": 2_100, "cache_read": 14_600,
                              "output": 310})
    for _ in range(3):
        agent_usage.record(agent_usage.WORKER_SUBPROCESS, project="proj_a",
                           wo_id=wo["id"], label="pytest evals/llm",
                           model="claude-opus-5",
                           usage={"total_cost_usd": 0.0104, "input": 6,
                                  "cache_write": 4_900, "cache_read": 31_100,
                                  "output": 420})

    b = ops.bill(wo["id"])
    assert b["checks"]["balanced"], b["checks"]["problems"]
    assert {line["key"] for line in b["actors"]} == {"worker", "jarvis", "subprocesses"}

    client = TestClient(create_app())
    for name, url in (("bill", f"/cost/proj_a/{wo['id']}"),
                      ("work_order", f"/wo/proj_a/{wo['id']}")):
        page = client.get(url)
        assert page.status_code == 200
        with open(f"{EVIDENCE}/{name}.html", "w") as f:
            f.write(page.text)
    with open(f"{EVIDENCE}/bill.json", "w") as f:
        json.dump(b, f, indent=1, default=str)

    from jarvis import cli
    capsys.readouterr()
    cli.main(["cost", wo["id"]])
    with open(f"{EVIDENCE}/cost.txt", "w") as f:
        f.write(capsys.readouterr().out)
