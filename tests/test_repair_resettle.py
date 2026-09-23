"""A repaired pull request goes back in the merge queue, and every line about it is true.

GitHub issue #705: three compounding defects on one work order that had actually
finished. The repair returned it to a status whose reason had evaporated mid-repair and
nothing re-derived it (1); the attention line named assumptions nobody still owed a
decision on and the invariant that owns that line declined to look (2); and the
stale-finish check reported the OS's own repair turn as a worker that gave up (3).
"""

from __future__ import annotations

import time

import pytest

from jarvis import invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import (
    STALE_FINISH_BLOCKER,
    check_project,
    parked_reason,
    true_blockers,
)
from jarvis.project_store import ProjectStore
from jarvis.timeline import build_timeline

PR = "https://github.com/acme/proj/pull/7"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def poll(daemon, store):
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def conflicting(fake_gh):
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="main")


@pytest.fixture()
def repaired(started, project, fake_gh, fake_claude, settle_turns):
    """The live shape of wo-dd8668fa, up to the moment the conflict clears.

    Finished behind a pull request with an assumption still pending, so it settles into
    `needs_review`; nudged about a conflict from there; the user accepts the assumption
    WHILE the repair turn is in flight; the worker ends its turn without finishing,
    exactly as the nudge instructs.
    """
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    ops.assume(wo["id"], "used tabs, not spaces")
    assert ops.finish(wo["id"], "opened a PR", pr_url=PR)["status"] == "needs_review"

    conflicting(fake_gh)
    started.tick_count = 0                      # a tick that polls: queues the nudge
    started.tick()
    assert store.queued_messages(wo["id"])
    started.tick_count = 1                      # a tick that delivers but does not poll
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "running"

    ops.review_work_order(wo["id"], accept=True)   # the user decides mid-repair
    assert settle_turns(store)
    started.tick_count = 1                      # settlement replays the snapshot
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    return wo


# -- (1) the status is re-derived when the episode closes ---------------------------


def test_the_repaired_order_goes_back_in_the_merge_queue(started, project, fake_gh,
                                                         repaired):
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "OPEN")                  # the worker resolved it

    started.tick_count = 0
    poll(started, store)

    row = store.get_work_order(repaired["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []
    assert any(e["kind"] == "pr_repair_resettled"
               for e in store.list_events(repaired["id"]))


def test_the_timeline_says_the_os_did_it(started, project, fake_gh, repaired):
    """"It fixed itself" has to be provable — and an unlabelled kind renders as its own
    name beside a JSON blob and still looks fine on the page."""
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "OPEN")
    poll(started, store)

    entries = build_timeline(store.get_work_order(repaired["id"]),
                             store.list_events(repaired["id"]), [])

    assert [e["label"] for e in entries if e["kind"] == "pr_repair_resettled"] == [
        "Back in the merge queue — the reason it was held is gone"]


def test_the_merge_then_ends_it_unattended(started, project, fake_gh, repaired):
    """The point of defect 1: `waiting_pr_merge` is the only status auto-merge looks at,
    so an order held out of it is out of the queue for ever."""
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "OPEN")
    poll(started, store)

    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-23T10:00:00Z")
    poll(started, store)

    assert store.get_work_order(repaired["id"])["status"] == "completed"


def test_a_still_pending_assumption_keeps_the_review(started, project, fake_gh,
                                                     repaired):
    """The rule Neo question 275 set is untouched: only a reason that has GONE is
    re-derived away."""
    store = ProjectStore(project)
    ops.assume(repaired["id"], "and left the old exporter in place")
    fake_gh.set_pr(PR, "OPEN")

    poll(started, store)

    row = store.get_work_order(repaired["id"])
    assert row["status"] == "needs_review"
    assert true_blockers(store, row) == ["1 assumption pending your review"]


def test_a_closed_pull_request_is_never_re_queued(started, project, fake_gh, repaired):
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "CLOSED")

    poll(started, store)

    assert store.get_work_order(repaired["id"])["status"] == "needs_review"


def test_an_order_no_repair_touched_is_left_alone(started, project, fake_gh):
    """`repaired_since_finish` is the bound: without it this re-derives every
    `needs_review` order holding a pull request."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    store.set_status(wo["id"], "needs_review")

    assert not ops.resettle_after_repair(store, wo["id"])
    assert store.get_work_order(wo["id"])["status"] == "needs_review"


def test_the_invariant_reaches_an_order_already_stranded(started, project, fake_gh,
                                                         repaired):
    """The orders the old code left behind — the episode is already closed, so no poll
    will ever call `clear_pr_repair` on them again."""
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "OPEN")
    poll(started, store)
    store.set_status(repaired["id"], "needs_review")   # as the shipped bug left it
    store.flag_attention(repaired["id"], "1 assumption pending your review")

    violations = [v for v in check_project(store)
                  if v.invariant == "INV-REPAIR-RESETTLED"]

    assert len(violations) == 1 and violations[0].repaired
    row = store.get_work_order(repaired["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]


def test_the_invariant_is_quiet_once_it_has_settled(started, project, fake_gh,
                                                    repaired):
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "OPEN")
    poll(started, store)

    assert [v.invariant for v in check_project(store)] == []
    assert [v.invariant for v in check_project(store)] == []


# -- (2) INV-ATTENTION-REASON is symmetrical ----------------------------------------


def test_a_decided_assumption_line_is_rewritten(started, project):
    """The reverse direction, which used to be skipped by construction: the reason names
    assumptions and the derived blocker does not."""
    wo = ops.create_work_order("proj_a", "add feature X")
    store = ProjectStore(project)
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], "3 assumptions pending your review")

    violations = [v for v in check_project(store)
                  if v.invariant == "INV-ATTENTION-REASON"]

    assert len(violations) == 1
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        "worker failed — review and retry"


def test_a_vaguer_but_true_assumption_line_is_left_alone(started, project):
    """The XOR: `ops.assume`'s generic reason against a real pending decision is true,
    and rewriting it would report a violation on every work order that files one."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.assume(wo["id"], "used tabs, not spaces")
    store = ProjectStore(project)

    assert [v.invariant for v in check_project(store)] == []
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        "assumptions pending review"


def test_a_hook_reason_about_something_else_still_survives(started, project):
    """The deliberate asymmetry that is NOT the bug: when neither the stored reason nor
    the derived blocker is about assumptions, the specific one wins."""
    wo = ops.create_work_order("proj_a", "add feature X")
    store = ProjectStore(project)
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], "needs permission to run `npm publish`")

    assert [v.invariant for v in check_project(store)] == []
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        "needs permission to run `npm publish`"


# -- (3) the OS does not punish a worker for obeying it -----------------------------


def test_a_repair_turn_is_not_a_worker_that_gave_up(started, project, repaired):
    """`PR_CONFLICT_NUDGE` tells the worker in capitals not to call `jarvis wo finish`,
    so a turn newer than the finish is the guaranteed outcome of the happy path."""
    store = ProjectStore(project)
    row = store.get_work_order(repaired["id"])
    turn = store.latest_turn(repaired["id"])
    assert store.turn_opened_by(turn) == "pr-conflict"
    assert float(store.events_of_kind(repaired["id"], "finished")[-1]["ts"]) \
        < float(turn["started_at"])

    long_after = float(turn["ended_at"]) + 365 * 24 * 3600

    assert parked_reason(store, row, now=long_after) is None
    assert STALE_FINISH_BLOCKER not in true_blockers(store, row, now=long_after)


def test_a_turn_the_user_started_is_still_judged(started, project, repaired,
                                                 fake_claude, settle_turns):
    """The exemption is about who opened the turn, not about the work order."""
    store = ProjectStore(project)
    ops.send_message(repaired["id"], "have another look at the exporter")
    started.tick_count = 1
    started.tick()
    assert settle_turns(store)
    store.set_status(repaired["id"], "needs_review")

    turn = store.latest_turn(repaired["id"])
    assert store.turn_opened_by(turn) == "jarvis"
    row = store.get_work_order(repaired["id"])
    long_after = float(turn["ended_at"]) + 365 * 24 * 3600

    assert parked_reason(store, row, now=long_after) == STALE_FINISH_BLOCKER


def test_a_dispatch_turn_reports_no_source(started, project):
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)

    assert store.turn_opened_by(store.latest_turn(wo["id"])) == ""
    assert store.turn_opened_by(None) == ""


def test_the_repair_sources_are_the_ones_the_nudge_writes(started, project, fake_gh,
                                                          repaired):
    """The constant `parked_reason` reads and the one `nudge_pr_repair` writes cannot
    drift: both derive from the repair names."""
    assert invariants.PR_REPAIR_SOURCES == tuple(r.source for r in ops.PR_REPAIRS)


# -- all three together -------------------------------------------------------------


def test_the_user_is_asked_for_nothing_at_all(started, project, fake_gh, repaired):
    """What the issue is actually about: the order is done, and the OS says so."""
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "OPEN")
    poll(started, store)
    assert [v.invariant for v in check_project(store)] == []

    row = store.get_work_order(repaired["id"])
    long_after = time.time() + 365 * 24 * 3600

    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row, now=long_after) == []
    assert ops.os_status()["attention"] == []
