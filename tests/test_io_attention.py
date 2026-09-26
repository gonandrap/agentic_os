"""Attention for improvement orders — exactly one item, flag-once by construction.

Section 6.2 of docs/superpowers/specs/2026-09-23-improvement-orders.md. There is NO ack
verb for a feature-order-level flag, so flag-once is proven the way the section says:
raise it, tick repeatedly, assert nothing re-raises or RE-WRITES it (kn-089de524), then
decide the findings and assert it is down and stays down.

Feature orders are pinned against improvement orders in this same file: the two kinds
share one composition block in `ops.os_status`, and a change to one silently changing the
other is what these paired assertions exist to catch.
"""

from __future__ import annotations

import pytest

from jarvis import findings, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import a_report

TICKS = 7  # > RECONCILE_EVERY_TICKS, so the reconcile pass runs more than once


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def analysing(started, store, improvement_order):
    """An improvement order in `planning` with its analyst wired to it as a child."""
    analyst = store.create_work_order("analyse it", description="the observation",
                                      kind="planner", status="running",
                                      parent_id=improvement_order["id"])
    store.update_feature_order(improvement_order["id"], plan_wo_id=analyst["id"],
                               status="planning")
    return improvement_order, analyst


def io_items(io_id):
    return [a for a in ops.os_status()["attention"] if a.get("fo_id") == io_id]


def headline(store, io_id):
    fo = store.get_feature_order(io_id)
    from jarvis import db
    return findings.review_headline(fo, db.from_json(fo["plan"], None))


def flag_row(store, io_id):
    fo = store.get_feature_order(io_id)
    return (fo["needs_attention"], fo["attention_reason"], fo["updated_at"])


def test_an_improvement_order_in_planning_raises_nothing(analysing, store):
    io, analyst = analysing

    assert not store.get_feature_order(io["id"])["needs_attention"]
    assert io_items(io["id"]) == []


def test_submit_findings_raises_exactly_one_item_with_the_headline_verbatim(
        analysing, store):
    io, _ = analysing

    ops.submit_findings(io["id"], a_report())

    row = store.get_feature_order(io["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == headline(store, io["id"])
    assert len(io_items(io["id"])) == 1


def test_the_flag_is_never_re_raised_or_re_written_by_a_tick(analysing, store, started):
    io, _ = analysing
    ops.submit_findings(io["id"], a_report())
    before = flag_row(store, io["id"])

    for _ in range(TICKS):
        started.tick()

    assert flag_row(store, io["id"]) == before
    assert len(io_items(io["id"])) == 1


def test_deciding_every_finding_lowers_the_flag_and_it_stays_down(analysing, store,
                                                                 started):
    io, _ = analysing
    ops.submit_findings(io["id"], a_report())

    ops.review_findings(io["id"], accept_all=True)

    row = store.get_feature_order(io["id"])
    assert not row["needs_attention"]
    assert row["attention_reason"] is None
    assert io_items(io["id"]) == []

    for _ in range(TICKS):
        started.tick()

    assert not store.get_feature_order(io["id"])["needs_attention"]
    assert io_items(io["id"]) == []


def test_the_item_names_jarvis_io_review_and_carries_no_progress_prefix(analysing,
                                                                       store):
    io, analyst = analysing
    ops.submit_findings(io["id"], a_report())
    # The analyst is settled by the submission; flagged here so the rollup has something
    # to collapse — the property under test is that a child of an improvement order never
    # gets its own line.
    store.flag_attention(analyst["id"], "the analyst wants you")

    items = io_items(io["id"])
    assert len(items) == 1
    item = items[0]
    assert item["decide"] == f"jarvis io review {io['id']}"
    # No progress prefix: the reason OPENS on the headline's counts. The rolled-up
    # child's clause is appended after it, as it is for a feature order.
    assert item["reason"].startswith(headline(store, io["id"]))
    assert item["reason"].startswith(f"{len(a_report()['findings'])} findings on")
    assert analyst["id"] in item["rolled_up"]
    assert [a for a in ops.os_status()["attention"]
            if a.get("wo_id") == analyst["id"]] == []


def test_a_feature_orders_item_keeps_its_progress_prefix_and_fo_show(started, store):
    fo = store.create_feature_order("ship the thing", description="do the thing")
    store.update_feature_order(fo["id"], status="plan_review")
    store.flag_feature_attention(fo["id"], "its plan needs you")

    items = [a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"]]
    assert len(items) == 1
    item = items[0]
    assert item["decide"] == f"jarvis fo show {fo['id']}"
    label = ops.feature_progress(store, store.get_feature_order(fo["id"]))["label"]
    assert item["reason"] == f"{label} — its plan needs you"
