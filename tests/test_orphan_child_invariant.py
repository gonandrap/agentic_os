"""INV-FEATURE-ORPHAN-CHILD — a settled parent may not leave a live or flagged child unseen"""

from __future__ import annotations

import pytest

from jarvis import invariants, ops
from jarvis.project_store import ProjectStore


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


BUDGET_LINE = ("budget spent — $2.00 of its feature's slice of $2.00; raise it with "
               "`jarvis wo budget <id> <amount>` or close it")


def the_wo_6be2ab21_shape(store: ProjectStore, kind: str = "investigation",
                          child_kind: str = "investigator") -> tuple[dict, dict]:
    """A `completed` investigation whose investigator sits budget_exhausted and flagged"""
    fo = store.create_feature_order(title="why is wo-x stuck", description="d", kind=kind)
    child = store.create_work_order(title="investigate", description="d",
                                    origin="jarvis", kind=child_kind,
                                    parent_id=fo["id"])
    store.set_status(child["id"], "running")
    store.set_status(child["id"], "budget_exhausted")
    store.flag_attention(child["id"], BUDGET_LINE)
    store.set_feature_status(fo["id"], "completed")
    return store.get_feature_order(fo["id"]), store.get_work_order(child["id"])


def orphan(found: list) -> list:
    return [v for v in found if v.invariant == invariants.INV_FEATURE_ORPHAN_CHILD]


def test_it_fires_on_the_wo_6be2ab21_shape_and_flags_the_parent(store):
    """THE FINDING: the parent asks for the user, naming both ids and the child's reason"""
    fo, child = the_wo_6be2ab21_shape(store)

    found = orphan(invariants.check_project(store))

    assert [v.subject_id for v in found] == [fo["id"]]
    assert found[0].repaired
    parent = store.get_feature_order(fo["id"])
    assert parent["needs_attention"]
    assert fo["id"] in parent["attention_reason"]
    assert child["id"] in parent["attention_reason"]
    assert BUDGET_LINE in parent["attention_reason"]


def test_the_child_status_and_flag_are_left_alone(store):
    """MUST NOT silently settle the child: whatever the flag was about stays owed"""
    fo, child = the_wo_6be2ab21_shape(store)

    invariants.check_project(store)

    after = store.get_work_order(child["id"])
    assert after["status"] == "budget_exhausted"
    assert after["needs_attention"]


def test_the_flagged_parent_shows_on_the_default_listing(store, jarvis_home, fake_claude,
                                                         catalog_file):
    """A settled parent is hidden behind --all; a flagged one is not"""
    ops.start_os(str(catalog_file), foreground=True)
    fo, _ = the_wo_6be2ab21_shape(store)
    assert not store.get_feature_order(fo["id"])["needs_attention"]

    invariants.check_project(store)

    assert store.get_feature_order(fo["id"])["needs_attention"]
    assert fo["id"] in [r["id"] for r in ops.list_investigation_orders("proj_a")]


def test_closing_the_child_lowers_the_parent_flag_on_the_next_tick(store):
    """Derived, so self-clearing: the user answers the child and the parent goes quiet"""
    fo, child = the_wo_6be2ab21_shape(store)
    invariants.check_project(store)
    store.set_status(child["id"], "completed")
    store.clear_attention(child["id"])

    invariants.check_project(store)

    assert not store.get_feature_order(fo["id"])["needs_attention"]


def test_a_terminal_unflagged_investigator_is_not_an_orphan(store):
    """A verdict submitted and settled `completed` with no flag is the healthy case"""
    fo = store.create_feature_order(title="t", description="d", kind="investigation")
    child = store.create_work_order(title="i", description="d", origin="jarvis",
                                    kind="investigator", parent_id=fo["id"])
    store.set_status(child["id"], "completed")
    store.set_feature_status(fo["id"], "completed")

    assert not orphan(invariants.check_project(store))
    assert not store.get_feature_order(fo["id"])["needs_attention"]


def test_a_live_unflagged_child_of_a_settled_parent_fires(store):
    """Non-terminal is enough: an analyst left `running` under a settled improvement order"""
    fo = store.create_feature_order(title="t", description="d", kind="improvement")
    child = store.create_work_order(title="a", description="d", origin="jarvis",
                                    kind="analyst", parent_id=fo["id"])
    store.set_status(child["id"], "running")
    store.set_feature_status(fo["id"], "cancelled")

    found = orphan(invariants.check_project(store))

    assert [v.subject_id for v in found] == [fo["id"]]
    reason = store.get_feature_order(fo["id"])["attention_reason"]
    assert child["id"] in reason and fo["id"] in reason


def test_a_parent_already_asking_for_the_user_keeps_its_own_reason(store):
    """A parent flagged for something else keeps that reason: it is never overwritten"""
    fo, child = the_wo_6be2ab21_shape(store, kind="feature", child_kind="worker")
    store.set_feature_status(fo["id"], "cancelled")
    store.flag_feature_attention(fo["id"], "wo-dead failed")

    assert not orphan(invariants.check_project(store))
    assert store.get_feature_order(fo["id"])["attention_reason"] == "wo-dead failed"


def test_a_hidden_or_superseded_child_is_answered_for(store):
    """The user decluttered or superseded it: that is the user having seen it"""
    fo, child = the_wo_6be2ab21_shape(store)
    store.update_work_order(child["id"], hidden=1)

    assert not orphan(invariants.check_project(store))


def test_doctor_reports_without_repairing(store):
    """`jarvis doctor` without --repair is a pure read"""
    fo, _ = the_wo_6be2ab21_shape(store)

    found = orphan(invariants.check_project(store, repair=False))

    assert found
    assert not store.get_feature_order(fo["id"])["needs_attention"]
