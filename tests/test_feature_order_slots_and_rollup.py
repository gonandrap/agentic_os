"""Phase 3 of feature orders: the per-feature slot cap, and the attention rollup"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC, fixture_spec_section

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def child(key: str, needs: list[str] | None = None) -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} part of the exporter: add the module, wire it into the "
            f"command that calls it, and cover both paths with tests in the existing "
            f"suite. Do not change the public interface of the caller. FORCE_APPROVE"
        ),
        "needs": needs or [],
        "spec_section": fixture_spec_section(key),
    }


def a_feature(daemon, store, *keys: str, max_parallel: int | None = None) -> dict:
    """A released feature order with independent children"""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK,
                                  max_parallel=max_parallel)
    daemon.tick()
    ops.submit_plan(fo["id"], {"summary": "an exporter FORCE_APPROVE",
                               "design_doc": FIXTURE_DESIGN_DOC,
                               "children": [child(k) for k in keys]})
    daemon._neo_drain()
    return store.get_feature_order(fo["id"])


# -- max_parallel: the knob


def test_a_feature_order_is_uncapped_by_default(started, store):
    """The column has existed since Phase 2 and meant nothing"""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)

    assert fo["max_parallel"] is None


def test_the_cap_must_be_at_least_one(started):
    """Zero is not "no cap", it is a feature order that can never dispatch anything"""
    with pytest.raises(ops.OpsError, match="at least 1"):
        ops.create_feature_order("proj_a", "CSV export", description=ASK, max_parallel=0)


def test_promoting_a_backlog_item_can_set_the_cap(started):
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "CSV export", description=ASK)
        out = ops.promote_backlog(item["id"], as_feature=True, max_parallel=2)

        assert ops.show_feature_order(out["fo_id"])["max_parallel"] == 2
    finally:
        central.close()


def test_the_cap_is_refused_on_a_plain_work_order_promotion(started):
    """Refused rather than ignored: a work order has no children to cap"""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "CSV export", description=ASK)
        with pytest.raises(ops.OpsError, match="add --as feature"):
            ops.promote_backlog(item["id"], max_parallel=2)
    finally:
        central.close()


# -- max_parallel: the effect on dispatch


def test_a_capped_feature_dispatches_only_that_many_children(started, store):
    fo = a_feature(started, store, "one", "two", "three", max_parallel=2)

    started.tick()

    running = [c for c in store.feature_children(fo["id"], kinds=("worker",))
               if c["status"] in ("dispatching", "running")]
    assert len(running) == 2
    assert store.count_active_children(fo["id"]) == 2


def test_the_held_child_starts_when_a_sibling_finishes(started, store):
    """The cap holds a slot, it does not drop the work: nothing is written while a child waits"""
    fo = a_feature(started, store, "one", "two", "three", max_parallel=2)
    one, two, three = store.feature_children(fo["id"], kinds=("worker",))
    store.set_status(one["id"], "running")
    store.set_status(two["id"], "running")

    assert store.claim_next_pending() is None, "both slots are taken"

    store.set_status(one["id"], "completed")

    claimed = store.claim_next_pending()
    assert claimed is not None and claimed["id"] == three["id"]


def test_an_uncapped_feature_dispatches_everything_the_project_allows(started, store):
    """The control for the two tests above: same plan, no cap, and all three run"""
    fo = a_feature(started, store, "one", "two", "three")

    started.tick()

    assert store.count_active_children(fo["id"]) == 3


def test_the_cap_does_not_hold_up_unrelated_work(started, store):
    """The same property Phase 1's dependency filter has: the filter is in the row selection"""
    fo = a_feature(started, store, "one", "two", "three", max_parallel=1)
    other = ops.create_work_order("proj_a", "unrelated", description="something else")

    started.tick()

    assert store.count_active_children(fo["id"]) == 1
    assert store.get_work_order(other["id"])["status"] != "pending"


def test_the_planner_is_not_capped_against_its_own_children(started, store):
    """A planner carries `parent_id` too"""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK,
                                  max_parallel=1)

    started.tick()

    planner = store.get_work_order(store.get_feature_order(fo["id"])["plan_wo_id"])
    assert planner["status"] in ("dispatching", "running")


def test_a_capped_child_says_why_it_is_waiting(started, store):
    """`pending` alone promises "will start as soon as a slot frees\""""
    from jarvis import invariants

    fo = a_feature(started, store, "one", "two", max_parallel=1)
    started.tick()

    waiting = [c for c in store.feature_children(fo["id"], kinds=("worker",)) if c["status"] == "pending"]
    assert len(waiting) == 1
    label = invariants.status_label(store, waiting[0])
    assert label.startswith("pending — waiting for a slot in ")
    assert fo["id"] in label and "1/1" in label


def test_a_child_blocked_on_a_dependency_says_that_instead(started, store):
    """Ranked deliberately: a child waiting on a sibling's MERGE is not going to start when a slot frees"""
    from jarvis import invariants

    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK,
                                  max_parallel=1)
    started.tick()
    ops.submit_plan(fo["id"], {"summary": "an exporter FORCE_APPROVE",
                               "design_doc": FIXTURE_DESIGN_DOC,
                               "children": [child("schema"),
                                            child("api", needs=["schema"])]})
    started._neo_drain()
    started.tick()

    api = store.feature_children(fo["id"], kinds=("worker",))[1]
    assert invariants.status_label(store, api).startswith("pending — blocked by")


def test_a_capped_child_is_not_an_attention_item(started, store):
    """Waiting for a slot is the system working"""
    fo = a_feature(started, store, "one", "two", max_parallel=1)
    started.tick()

    waiting = [c for c in store.feature_children(fo["id"], kinds=("worker",)) if c["status"] == "pending"]
    assert waiting and not waiting[0]["needs_attention"]


def test_show_reports_the_cap_and_what_is_using_it(started, store):
    fo = a_feature(started, store, "one", "two", "three", max_parallel=2)
    started.tick()

    detail = ops.show_feature_order(fo["id"])

    assert detail["max_parallel"] == 2
    assert detail["active_children"] == 2


# -- the attention rollup


def flag(store, wo_id: str, reason: str) -> None:
    store.update_work_order(wo_id, needs_attention=1, attention_reason=reason)


def test_three_flagged_children_are_one_line(started, store):
    """The whole point"""
    fo = a_feature(started, store, "one", "two", "three")
    started.tick()
    for c in store.feature_children(fo["id"], kinds=("worker",)):
        flag(store, c["id"], "assumption needs a decision")

    attention = ops.os_status()["attention"]

    assert [a["fo_id"] for a in attention if a.get("fo_id")] == [fo["id"]]
    assert not [a for a in attention if a["wo_id"]]


def test_the_line_says_how_many_and_which(started, store):
    """Collapsed, not hidden: the user has to be able to tell one flagged child from three without opening..."""
    fo = a_feature(started, store, "one", "two", "three")
    started.tick()
    kids = store.feature_children(fo["id"], kinds=("worker",))
    flag(store, kids[0]["id"], "assumption needs a decision")

    item = next(a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"])

    assert "1 of its work orders need you" in item["reason"]
    assert kids[0]["id"] in item["reason"]
    assert "assumption needs a decision" in item["reason"]
    assert item["rolled_up"] == [kids[0]["id"]]
    assert item["decide"] == f"jarvis fo show {fo['id']}"


def test_the_line_carries_the_progress_count(started, store):
    """`3/6 done, 1 needs you` — the count is what tells the user whether this is a feature in trouble or..."""
    fo = a_feature(started, store, "one", "two", "three")
    started.tick()
    kids = store.feature_children(fo["id"], kinds=("worker",))
    store.set_status(kids[0]["id"], "completed")
    flag(store, kids[1]["id"], "assumption needs a decision")

    item = next(a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"])

    assert item["reason"].startswith("1/3 done — ")


def test_a_standalone_work_order_still_gets_its_own_line(started, store):
    """The control"""
    wo = ops.create_work_order("proj_a", "unrelated", description="something else")
    store.update_work_order(wo["id"], needs_attention=1, attention_reason="failed")

    attention = ops.os_status()["attention"]

    assert [a["wo_id"] for a in attention if a["wo_id"]] == [wo["id"]]


def test_the_children_keep_their_own_flags(started, store):
    """Presentation only"""
    fo = a_feature(started, store, "one", "two")
    started.tick()
    kids = store.feature_children(fo["id"], kinds=("worker",))
    flag(store, kids[0]["id"], "assumption needs a decision")

    ops.os_status()

    assert store.get_work_order(kids[0]["id"])["needs_attention"] == 1
    detail = ops.show_feature_order(fo["id"])
    assert [c["id"] for c in detail["children"] if c["needs_attention"]] == [kids[0]["id"]]


def test_a_flagged_feature_with_no_flagged_children_still_gets_its_line(started, store):
    """The Phase 2 case — an escalated plan — must survive the rollup unchanged"""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    started.tick()
    store.update_feature_order(fo["id"], needs_attention=1,
                              attention_reason="Neo escalated the plan")

    item = next(a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"])

    assert "Neo escalated the plan" in item["reason"]
    assert item["rolled_up"] == []


def test_a_failed_feature_reaches_the_strip_at_all(started, store):
    """`failed` is a SETTLED status, so a scan of the open statuses alone never sees it"""
    fo = a_feature(started, store, "one", "two")
    started.tick()
    store.set_status(store.feature_children(fo["id"], kinds=("worker",))[0]["id"], "failed")
    started.tick()  # settle_features marks the feature failed and flags it

    settled = store.get_feature_order(fo["id"])
    assert settled["status"] == "failed" and settled["needs_attention"] == 1
    item = next(a for a in ops.os_status()["attention"] if a.get("fo_id") == fo["id"])
    assert settled["attention_reason"] in item["reason"]


def test_a_failed_feature_still_rolls_its_children_up(started, store):
    """A settled feature order has left the open statuses, but a `failed` one always has flagged children"""
    fo = a_feature(started, store, "one", "two")
    started.tick()
    kids = store.feature_children(fo["id"], kinds=("worker",))
    store.set_status(kids[0]["id"], "failed")
    flag(store, kids[0]["id"], "the worker died")  # what settle_work_order does
    started.tick()

    assert store.get_feature_order(fo["id"])["status"] == "failed"
    attention = ops.os_status()["attention"]
    assert [a["fo_id"] for a in attention if a.get("fo_id")] == [fo["id"]]
    assert not [a for a in attention if a["wo_id"]]
    assert kids[0]["id"] in next(a for a in attention if a.get("fo_id"))["reason"]


def test_status_renders_a_feature_line_without_crashing(started, store, capsys):
    """`jarvis status` read `approval_id` off every item that carried a `decide` key"""
    from jarvis import cli

    fo = a_feature(started, store, "one", "two")
    started.tick()
    flag(store, store.feature_children(fo["id"], kinds=("worker",))[0]["id"], "needs a decision")

    cli.main(["status"])

    out = capsys.readouterr().out
    assert fo["id"] in out
    assert "NEEDS YOUR ATTENTION" in out
    assert f"jarvis fo show {fo['id']}" in out
