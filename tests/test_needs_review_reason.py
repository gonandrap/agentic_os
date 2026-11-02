"""A `needs_review` work order says why, and nobody acks for the user"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis import invariants, ops
from jarvis.cli import main
from jarvis.invariants import (
    IDLE_NO_FINISH_BLOCKER,
    PARKED_BLOCKER,
    PR_CLOSED_BLOCKER,
    STALE_FINISH_BLOCKER,
    true_blockers,
)
from jarvis.project_store import ProjectStore
from jarvis.timeline import build_timeline
from jarvis.ui.app import create_app


@pytest.fixture()
def registered(jarvis_home, catalog_file, project):
    """proj_a on the registry, so `ops` can find a work order by id"""
    ops.start_os(str(catalog_file), foreground=True)
    return project


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


def _idle_needs_review(project, title: str = "the one that went quiet") -> dict:
    """`Daemon.settle_work_order`'s shape: a worker that stopped without finishing"""
    wo = ops.create_work_order("proj_a", title)
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
        store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def _closed_pr_needs_review(project, title: str = "the one that was refused") -> dict:
    """A `needs_review` blocker the user MAY ack: the idle one is refused (issue 1456)"""
    wo = ops.create_work_order("proj_a", title)
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review", pr_state="CLOSED")
        store.flag_attention(wo["id"], PR_CLOSED_BLOCKER)
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def test_ack_is_refused_on_an_idle_park_and_names_the_way_out(registered):
    """An acked idle park vanished from every derived view (issue 1456)"""
    wo = _idle_needs_review(registered)

    with pytest.raises(ops.OpsError, match=f"jarvis wo send {wo['id']}"):
        ops.ack_attention(wo["id"])
    assert ops.ack_attention(all_projects=True)["skipped"] == [
        {"wo_id": wo["id"], "reason": IDLE_NO_FINISH_BLOCKER}]

    store = ProjectStore(registered)
    try:
        row = store.get_work_order(wo["id"])
        assert row["needs_attention"] == 1
        assert true_blockers(store, row) == [IDLE_NO_FINISH_BLOCKER]
    finally:
        store.close()


# -- (c) the blocker names the way out


def test_the_idle_blocker_ends_with_a_command_like_both_its_siblings():
    """A diagnosis with no action is half an attention item"""
    for blocker in (IDLE_NO_FINISH_BLOCKER, PARKED_BLOCKER, STALE_FINISH_BLOCKER):
        assert "`jarvis wo send`" in blocker


# -- (a) an ack the OS makes for itself dismisses nothing


def test_ack_os_flag_leaves_a_blocker_the_user_never_saw_standing(registered):
    """The bug itself, at the primitive"""
    project = registered
    wo = _idle_needs_review(project)

    ops.ack_os_flag(wo["id"])

    store = ProjectStore(project)
    try:
        row = store.get_work_order(wo["id"])
        assert row["needs_attention"] == 1
        assert row["attention_reason"] == IDLE_NO_FINISH_BLOCKER
        assert row["acknowledged_blockers"] is None
        assert store.events_of_kind(wo["id"], "acknowledged") == []
        # And it is still on the attention list, tick after tick
        assert true_blockers(store, row) == [IDLE_NO_FINISH_BLOCKER]
    finally:
        store.close()


def test_ack_os_flag_puts_the_flag_down_when_nothing_is_left(registered):
    """The other half, and the reason it is not `clear_attention`: the user's own earlier dismissals are..."""
    project = registered
    wo = ops.create_work_order("proj_a", "quiet")
    store = ProjectStore(project)
    try:
        store.ack_attention(wo["id"], ["something the user really did dismiss"])
        store.flag_attention(wo["id"], "cost alarm 7 needs you")
    finally:
        store.close()

    ops.ack_os_flag(wo["id"])

    store = ProjectStore(project)
    try:
        row = store.get_work_order(wo["id"])
        assert row["needs_attention"] == 0
        assert row["attention_reason"] is None
        assert json.loads(row["acknowledged_blockers"]) == [
            "something the user really did dismiss"]
    finally:
        store.close()


def test_ack_os_flag_re_raises_against_the_blocker_that_is_actually_left(registered):
    """The flag the OS raised said "cost alarm"; what is really owed is the idle worker"""
    project = registered
    wo = _idle_needs_review(project)
    store = ProjectStore(project)
    try:
        store.flag_attention(wo["id"], "cost alarm 7 needs you")
    finally:
        store.close()

    ops.ack_os_flag(wo["id"])

    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["attention_reason"] \
            == IDLE_NO_FINISH_BLOCKER
    finally:
        store.close()


def test_ack_os_flag_writes_no_timeline_row_when_the_reason_is_unchanged(registered):
    """`flag_attention` writes an event per call"""
    project = registered
    wo = _idle_needs_review(project)
    store = ProjectStore(project)
    try:
        before = len(store.events_of_kind(wo["id"], "attention"))
    finally:
        store.close()

    ops.ack_os_flag(wo["id"])
    ops.ack_os_flag(wo["id"])

    store = ProjectStore(project)
    try:
        assert len(store.events_of_kind(wo["id"], "attention")) == before
    finally:
        store.close()


def test_the_users_own_ack_still_records_who_made_it(registered):
    """`jarvis wo ack` is unchanged — and now says on the record that it was them"""
    project = registered
    wo = _closed_pr_needs_review(project)

    ops.ack_attention(wo["id"])

    store = ProjectStore(project)
    try:
        row = store.get_work_order(wo["id"])
        assert row["needs_attention"] == 0
        assert json.loads(row["acknowledged_blockers"]) == [PR_CLOSED_BLOCKER]
        (event,) = store.events_of_kind(wo["id"], "acknowledged")
        assert json.loads(event["payload"])["by"] == "you"
    finally:
        store.close()


# -- (b) the reason survives the ack, on every surface


def test_the_cli_show_carries_what_was_already_seen(registered, capsys):
    """`jarvis wo show` on an acked `needs_review` used to print `attention_reason: None` and nothing else..."""
    project = registered
    wo = _closed_pr_needs_review(project)
    ops.ack_attention(wo["id"])

    main(["wo", "show", wo["id"], "--json"])

    detail = json.loads(capsys.readouterr().out)
    assert detail["status"] == "needs_review"
    assert detail["attention_reason"] is None
    assert detail["seen_blockers"] == [PR_CLOSED_BLOCKER]


def test_the_cli_list_says_why_an_unflagged_order_is_open(registered, capsys):
    project = registered
    wo = _closed_pr_needs_review(project)
    ops.ack_attention(wo["id"])

    main(["wo", "list"])

    out = capsys.readouterr().out
    assert "already seen" in out
    assert PR_CLOSED_BLOCKER in out


def test_the_dashboard_page_says_why_after_the_got_it_button(client, project):
    wo = _closed_pr_needs_review(project)
    client.post(f"/wo/proj_a/{wo['id']}/ack")

    page = client.get(f"/wo/proj_a/{wo['id']}")

    assert "already seen" in page.text
    assert "closed without merging" in page.text


# -- (d) the acknowledgement reads as prose


def test_the_acknowledged_event_is_prose_and_names_who_acked(registered):
    """It reached the user as `{"blockers": ["…\\u2014 nothing it started…"]}`"""
    project = registered
    wo = _closed_pr_needs_review(project)
    ops.ack_attention(wo["id"])

    store = ProjectStore(project)
    try:
        rows = build_timeline(store.get_work_order(wo["id"]),
                              store.list_events(wo["id"]), [])
    finally:
        store.close()

    (entry,) = [r for r in rows if r["label"].startswith("Acknowledged")]
    assert entry["label"] == "Acknowledged by you"
    assert entry["detail"] == PR_CLOSED_BLOCKER
    assert "{" not in entry["detail"]


def test_a_legacy_acknowledgement_is_attributed_to_nobody(registered):
    """Rows written before `by` existed include the ones the OS wrote on the user's behalf"""
    project = registered
    wo = _idle_needs_review(project)
    store = ProjectStore(project)
    try:
        store.add_event(wo["id"], "acknowledged", {"blockers": [IDLE_NO_FINISH_BLOCKER]})
        rows = build_timeline(store.get_work_order(wo["id"]),
                              store.list_events(wo["id"]), [])
    finally:
        store.close()

    (entry,) = [r for r in rows if r["label"].startswith("Acknowledged")]
    assert entry["label"] == "Acknowledged"


def test_acknowledged_decodes_the_column_for_every_surface(registered):
    """One reader, so `true_blockers` and the surfaces can never disagree about what counts as seen"""
    project = registered
    wo = _idle_needs_review(project)
    store = ProjectStore(project)
    try:
        assert invariants.acknowledged(store.get_work_order(wo["id"])) == []
        store.ack_attention(wo["id"], [IDLE_NO_FINISH_BLOCKER])
        row = store.get_work_order(wo["id"])
        assert invariants.acknowledged(row) == [IDLE_NO_FINISH_BLOCKER]
        assert true_blockers(store, row) == []
    finally:
        store.close()
