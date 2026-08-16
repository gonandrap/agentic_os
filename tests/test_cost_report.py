"""`jarvis cost` — attributing transcript spend back to work orders.

`tests/test_usage.py` covers the parser. This covers the layer above it: which work
orders a report speaks for, and how a feature order's total is assembled. The two
questions that matter here are both about honesty rather than arithmetic — a report
that silently omits a work order, or reports an unmeasurable one as free, is worse
than no report at all, because it invites a conclusion about where the tokens went.
"""

from __future__ import annotations

import json

import pytest

from jarvis import ops, usage
from jarvis.project_store import ProjectStore


def assistant_row(mid: str, *, write: int = 0, read: int = 0, out: int = 0) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": mid, "model": "claude-opus-5",
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": write,
                      "cache_read_input_tokens": read, "output_tokens": out},
        },
    }


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict]):
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return write


@pytest.fixture()
def registered(jarvis_home, project):
    """One registered project, so `cost_report` can resolve it by name."""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(project), "test project")
        central.conn.commit()
    finally:
        central.close()
    return project


@pytest.fixture()
def store(registered):
    s = ProjectStore(registered)
    yield s
    s.close()


def give_session(store, wo_id: str, session_id: str) -> None:
    store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                       (session_id, wo_id))
    store.conn.commit()


# -- what the report speaks for -------------------------------------------------------


def test_a_hidden_work_order_still_counts_toward_the_bill(store, transcripts):
    """Hiding is about attention, not about spend.

    Paired with a visible work order in the same report: asserting only that the
    hidden one appears would pass against a report that ignored `hidden` entirely in
    the other direction, and the totals line is what makes the omission dangerous.
    """
    visible = store.create_work_order("visible", "")
    hidden = store.create_work_order("hidden", "")
    give_session(store, visible["id"], "sess-visible")
    give_session(store, hidden["id"], "sess-hidden")
    store.set_hidden(hidden["id"])
    transcripts("sess-visible", [assistant_row("m1", write=1_000_000)])
    transcripts("sess-hidden", [assistant_row("m1", write=1_000_000)])

    report = ops.cost_report(project="proj_a")

    assert {u["id"] for u in report["units"]} == {visible["id"], hidden["id"]}
    assert report["measured"] == 2
    assert report["totals"]["list_cost_usd"] == pytest.approx(12.5)  # both, not one


def test_a_work_order_with_no_transcript_is_unmeasured_not_free(store, transcripts):
    """An empty transcript directory must not read as "this work order was free"."""
    measured = store.create_work_order("has a transcript", "")
    give_session(store, measured["id"], "sess-there")
    transcripts("sess-there", [assistant_row("m1", write=1_000_000)])
    store.create_work_order("never dispatched", "")

    report = ops.cost_report(project="proj_a")

    assert report["measured"] == 1
    assert report["unmeasured"] == 1
    gone = next(u for u in report["units"] if u["title"] == "never dispatched")
    assert gone["found"] is False
    assert gone["turns"] == 0
    # The measured one still contributes in full, so the total is not merely "small".
    assert report["totals"]["list_cost_usd"] == pytest.approx(6.25)


def test_units_are_ordered_dearest_first(store, transcripts):
    for name, tokens in (("cheap", 100_000), ("dear", 2_000_000), ("middling", 500_000)):
        wo = store.create_work_order(name, "")
        give_session(store, wo["id"], f"sess-{name}")
        transcripts(f"sess-{name}", [assistant_row("m1", write=tokens)])

    report = ops.cost_report(project="proj_a")
    assert [u["title"] for u in report["units"]] == ["dear", "middling", "cheap"]


def test_an_unregistered_project_is_refused_rather_than_reported_empty(registered):
    """An empty report for a typo'd name would read as "this project cost nothing"."""
    with pytest.raises(ops.OpsError, match="not registered"):
        ops.cost_report(project="no_such_project")


# -- feature orders -------------------------------------------------------------------


def test_a_feature_order_rolls_up_its_planner_and_its_children(store, transcripts):
    """The number a reader of `jarvis cost fo-…` wants is the whole feature's bill.

    The planner is the point: it carries `parent_id` like any child but is excluded
    from `feature_children`, so a rollup that used only the children would silently
    drop the single most expensive session of an unfinished feature order — which is
    exactly the shape fo-e353491c was in when this was written.
    """
    fo = store.create_feature_order("a feature", "do the thing")
    planner = store.create_work_order("Plan: a feature", "", kind="planner",
                                      parent_id=fo["id"])
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"])
    child = store.create_work_order("child one", "", parent_id=fo["id"])
    give_session(store, planner["id"], "sess-planner")
    give_session(store, child["id"], "sess-child")
    transcripts("sess-planner", [assistant_row("m1", write=2_000_000)])
    transcripts("sess-child", [assistant_row("m1", write=1_000_000)])

    report = ops.cost_report(target=fo["id"])

    assert report["scope"] == fo["id"]
    assert {u["id"] for u in report["units"]} == {planner["id"], child["id"]}
    assert report["totals"]["list_cost_usd"] == pytest.approx(18.75)  # 12.50 + 6.25


def test_a_work_order_id_reports_only_that_work_order(store, transcripts):
    """The single-unit path, and the control for the rollup above."""
    fo = store.create_feature_order("a feature", "do the thing")
    child = store.create_work_order("child one", "", parent_id=fo["id"])
    other = store.create_work_order("unrelated", "")
    give_session(store, child["id"], "sess-child")
    give_session(store, other["id"], "sess-other")
    transcripts("sess-child", [assistant_row("m1", write=1_000_000)])
    transcripts("sess-other", [assistant_row("m1", write=1_000_000)])

    report = ops.cost_report(target=child["id"])

    assert [u["id"] for u in report["units"]] == [child["id"]]
    assert report["totals"]["list_cost_usd"] == pytest.approx(6.25)


# -- recorded turns: the exact accounting -----------------------------------------------


def recorded_usage(cost: float = 0.05, *, peak: int = 48_249,
                   window: int | None = 1_000_000, calls: int = 1) -> dict:
    """The compact envelope `claude_cli.derive_turn_usage` stores in `usage_json`."""
    return {"total_cost_usd": cost, "input": 2, "cache_write": 2558,
            "cache_read": 45689, "cache_1h": 2558, "cache_5m": 0, "output": 941,
            "api_calls": calls, "context_peak": peak, "context_window": window,
            "duration_api_ms": 15049, "cost_by_model": {"claude-opus-5": cost}}


def add_turn(store, wo_id: str, usage: dict | None, state: str = "done",
             outfile: str = "") -> dict:
    kind = "message" if store.list_turns(wo_id) else "dispatch"
    turn = store.create_turn(wo_id, kind=kind, prompt="p")
    if outfile:
        store.conn.execute("UPDATE wo_turns SET outfile=? WHERE id=?",
                           (outfile, turn["id"]))
    store.finish_turn(turn["id"], state, result="r",
                      cost_usd=usage["total_cost_usd"] if usage else None,
                      num_turns=1,
                      usage_json=json.dumps(usage) if usage else None)
    return store.get_turn(turn["id"])


def test_a_single_work_order_breaks_down_turn_by_turn(store, transcripts):
    """The point of recording: seeing WHERE in a bloated work order the cost rises.

    Each turn carries its own cost, token classes (ephemeral split included) and
    context peak against the model's window, and the aggregate is exactly the sum of
    the turns — no estimation anywhere.
    """
    wo = store.create_work_order("bloating", "")
    add_turn(store, wo["id"], recorded_usage(0.05, peak=48_249))
    add_turn(store, wo["id"], recorded_usage(0.07, peak=90_000, calls=3))

    report = ops.cost_report(target=wo["id"])

    assert report["provenance"] == "recorded"
    detail = report["turns_detail"]
    assert [t["seq"] for t in detail] == [1, 2]
    assert [t["kind"] for t in detail] == ["dispatch", "message"]
    assert all(t["recorded"] for t in detail)
    assert detail[0]["cache_1h"] == 2558 and detail[0]["cache_5m"] == 0
    assert detail[1]["context_peak"] == 90_000
    assert detail[1]["context_pct"] == pytest.approx(9.0)     # of a 1M window
    rec = report["recorded_totals"]
    assert rec["cost_usd"] == pytest.approx(0.12)             # exactly the sum
    assert rec["context_peak"] == 90_000                      # the max, not the sum
    assert rec["api_calls"] == 4
    unit = report["units"][0]
    assert unit["provenance"] == "recorded"
    assert unit["recorded_cost_usd"] == pytest.approx(0.12)


def test_partially_recorded_turns_are_declared_never_silently_mixed(store, transcripts):
    """Half a record must say it is half a record: one number quietly assembled from
    two accounting systems would be trusted as if it were one."""
    wo = store.create_work_order("half recorded", "")
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], None)                 # settled, nothing recorded

    report = ops.cost_report(target=wo["id"])

    assert report["provenance"] == "mixed"
    assert report["turns_recorded"] == 1
    assert report["turns_settled"] == 2
    assert [t["recorded"] for t in report["turns_detail"]] == [True, False]
    # the recorded totals speak only for what was recorded
    assert report["recorded_totals"]["cost_usd"] == pytest.approx(0.05)


def test_a_work_order_with_no_turn_record_stays_on_the_transcript_estimate(
        store, transcripts):
    """The old path is the fallback, and it is labelled as the estimate it is:
    sessions that predate turn capture (or were never Jarvis-driven) have only
    their transcript to speak for them."""
    wo = store.create_work_order("pre-capture", "")
    give_session(store, wo["id"], "sess-old")
    transcripts("sess-old", [assistant_row("m1", write=1_000_000)])

    report = ops.cost_report(target=wo["id"])

    assert report["provenance"] == "transcript"
    assert report["turns_detail"] == []
    assert report["recorded_totals"] is None
    assert report["units"][0]["list_cost_usd"] == pytest.approx(6.25)


def test_missing_usage_is_backfilled_from_the_outfile_on_read(store, transcripts,
                                                              tmp_path):
    """History is recoverable without a migration: a turn reaped before this release
    has no `usage_json`, but its outfile still holds the full envelope. The first
    read parses it AND writes it back, so the record survives outfile pruning."""
    from test_turn_usage import result_json

    outfile = tmp_path / "old-turn.json"
    outfile.write_text(json.dumps(result_json()))
    wo = store.create_work_order("from before", "")
    add_turn(store, wo["id"], None, outfile=str(outfile))

    report = ops.cost_report(target=wo["id"])

    assert report["provenance"] == "recorded"
    assert report["turns_detail"][0]["context_peak"] == 48_249
    # written back: the row now carries the envelope itself...
    assert json.loads(store.list_turns(wo["id"])[0]["usage_json"])["output"] == 941
    # ...so the record outlives the outfile
    outfile.unlink()
    again = ops.cost_report(target=wo["id"])
    assert again["provenance"] == "recorded"
    assert again["turns_detail"][0]["output"] == 941


def test_the_fleet_rollup_carries_the_recorded_cost(store, transcripts):
    """The summary keeps working on transcripts, but the exact figure rides along."""
    wo = store.create_work_order("recorded one", "")
    add_turn(store, wo["id"], recorded_usage(0.25))
    other = store.create_work_order("transcript one", "")
    give_session(store, other["id"], "sess-t")
    transcripts("sess-t", [assistant_row("m1", write=1_000_000)])

    report = ops.cost_report(project="proj_a")

    assert report["totals"]["recorded_cost_usd"] == pytest.approx(0.25)
    by_id = {u["id"]: u for u in report["units"]}
    assert by_id[wo["id"]]["provenance"] == "recorded"
    assert by_id[other["id"]]["provenance"] == "transcript"


def test_the_cli_prints_the_per_turn_table(store, transcripts, capsys):
    from jarvis import cli

    wo = store.create_work_order("printable", "")
    add_turn(store, wo["id"], recorded_usage(0.05, peak=48_249))
    add_turn(store, wo["id"], recorded_usage(0.07, peak=90_000, calls=3))

    assert cli.main(["cost", wo["id"]]) == 0

    out = capsys.readouterr().out
    assert "recorded" in out
    assert "dispatch" in out and "message" in out
    assert "9%" in out                          # context occupancy of the 1M window
    assert "0.07" in out                        # the second turn's own cost


def test_the_cli_declares_a_mixed_record(store, transcripts, capsys):
    from jarvis import cli

    wo = store.create_work_order("half", "")
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], None)

    assert cli.main(["cost", wo["id"]]) == 0

    out = capsys.readouterr().out
    assert "1 of 2" in out
    assert "not recorded" in out


# -- the tax, end to end --------------------------------------------------------------


def test_the_rewrite_tax_survives_into_the_rollup(store, transcripts):
    """The report exists to surface this number, so it is asserted at the top level."""
    wo = store.create_work_order("resumed twice", "")
    give_session(store, wo["id"], "sess-resumed")
    transcripts("sess-resumed", [
        assistant_row("m1", write=1_000_000, read=0),
        assistant_row("m2", write=1_000_000, read=0),   # turn 2: prefix re-sent
        assistant_row("m3", write=1_000_000, read=0),   # turn 3: re-sent again
    ])

    report = ops.cost_report(project="proj_a")

    assert report["totals"]["rewrite_excess"] == 2_000_000
    assert report["totals"]["resume_boundaries"] == 0  # reads never dropped: all zero
    assert report["totals"]["rewrite_cost_usd"] == pytest.approx(11.5)
