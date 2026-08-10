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
