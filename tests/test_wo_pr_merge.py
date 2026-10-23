"""Work orders that end in a pull request: `waiting_pr_merge`, and the title prefix"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, db, gates, github, hooks, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import PR_POLL_EVERY_TICKS, Daemon
from jarvis.invariants import (
    PR_CLOSED_BLOCKER,
    PR_CONFLICT_BLOCKER,
    PR_REPAIR_MAX_ATTEMPTS,
    check_project,
    true_blockers,
)
from jarvis.neo_store import NeoStore
from jarvis.project_store import OPEN_STATUSES, ProjectStore
from jarvis.timeline import build_timeline

PR = "https://github.com/acme/proj/pull/7"
OTHER_PR = "https://github.com/acme/proj/pull/8"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, live_daemon):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


# -- the state


def test_finish_with_a_pr_parks_the_work_order(started, project):
    wo = ops.create_work_order("proj_a", "add feature X")

    out = ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert out["status"] == "waiting_pr_merge"
    store = ProjectStore(project)
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge"
    assert row["pr_url"] == PR
    assert row["result_summary"] == "opened a PR"


def test_finish_without_a_pr_still_completes(started, project):
    wo = ops.create_work_order("proj_a", "answered a question")

    assert ops.finish(wo["id"], "no code needed")["status"] == "completed"


def test_waiting_pr_merge_is_open_but_never_asks_for_the_user(started, project):
    """The whole point: it stays visible without spending the attention budget"""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    store = ProjectStore(project)
    row = store.get_work_order(wo["id"])
    assert row["status"] in OPEN_STATUSES
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []
    # and no invariant thinks that flagless open work order is a bug
    assert [v.invariant for v in check_project(store)] == []
    assert wo["id"] in [w["id"] for w in store.list_work_orders(statuses=OPEN_STATUSES)]
    assert ops.os_status()["attention"] == []


def test_pending_assumptions_outrank_the_pr(started, project):
    """A merge before the decision would accept the assumptions by the back door"""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.assume(wo["id"], "used tabs, not spaces")

    out = ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert out["status"] == "needs_review"
    store = ProjectStore(project)
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"]
    assert row["pr_url"] == PR  # recorded either way — the link is still useful


def test_the_reconciler_leaves_it_parked(started, project, fake_claude, settle_turns):
    """Settlement re-runs every tick; without the pr_url branch it would complete it"""
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    started.tick_count = 0
    started.tick()
    started.tick_count = 0
    started.tick()

    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_the_user_closes_it_after_merging(started, project):
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    out = ops.mark_done(wo["id"])

    assert out["was"] == "waiting_pr_merge"
    assert out["status"] == "completed"


def test_cli_finish_passes_the_pr_through(started, project, capsys):
    wo = ops.create_work_order("proj_a", "add feature X")

    cli.main(["wo", "finish", wo["id"], "--summary", "done", "--pr", PR])

    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    assert PR in capsys.readouterr().out


def test_cli_list_puts_running_then_pr_merges_first(started, project, capsys):
    """`jarvis wo list` orders by what the user can act on, like the dashboard"""
    running = ops.create_work_order("proj_a", "still going")
    merging = ops.create_work_order("proj_a", "waiting on a merge")
    pending = ops.create_work_order("proj_a", "not started yet")  # newest: listed first
    ops.finish(merging["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    store.set_status(running["id"], "running")

    cli.main(["wo", "list"])

    lines = [ln for ln in capsys.readouterr().out.splitlines() if "wo-" in ln]
    order = [next(w["id"] for w in (running, merging, pending) if w["id"] in ln)
             for ln in lines]
    assert order[:2] == [running["id"], merging["id"]]
    assert pending["id"] in order


# -- the way back from `needs_review`


@pytest.fixture()
def reviewable_pr(started, project):
    """Finished behind a PR, held in `needs_review` by one pending assumption"""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.assume(wo["id"], "used tabs, not spaces")
    assert ops.finish(wo["id"], "opened a PR", pr_url=PR)["status"] == "needs_review"
    return wo


def test_accepting_the_assumptions_parks_the_pr_instead_of_completing(
        started, project, reviewable_pr):
    out = ops.review_work_order(reviewable_pr["id"], accept=True)

    assert out["status"] == "waiting_pr_merge"
    store = ProjectStore(project)
    row = store.get_work_order(reviewable_pr["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []


def test_the_parked_work_order_is_still_polled_after_a_review(
        started, project, fake_gh, reviewable_pr):
    """The point of parking it: the merge still ends the work order unattended"""
    ops.review_work_order(reviewable_pr["id"], accept=True, feedback="tabs are fine")
    fake_gh.set_pr(PR, "MERGED")
    store = ProjectStore(project)

    poll(started, store)

    assert store.get_work_order(reviewable_pr["id"])["status"] == "completed"


def test_accepting_without_a_pr_still_completes(started, project):
    """Unchanged for the ordinary case — only a PR-carrying work order is held back"""
    wo = ops.create_work_order("proj_a", "answered a question")
    ops.assume(wo["id"], "read it as a question, not a code change")
    ops.finish(wo["id"], "no code needed")

    out = ops.review_work_order(wo["id"], accept=True)

    assert out["status"] == "completed"
    assert ProjectStore(project).get_work_order(wo["id"])["status"] == "completed"


def test_a_review_after_the_pr_was_closed_does_not_re_park_it(
        started, project, fake_gh, parked):
    """A CLOSED pull request is never going to merge"""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)

    out = ops.review_work_order(parked["id"], accept=True)

    assert out["status"] == "completed"
    assert store.get_work_order(parked["id"])["status"] == "completed"


def test_a_second_pull_request_is_not_judged_by_the_first_ones_closure(
        started, project, fake_gh, parked):
    """PAIRED with the test above, and the pairing is the point: the same `CLOSED` in `pr_state` means..."""
    second = "https://github.com/acme/proj/pull/8"
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)
    assert store.get_work_order(parked["id"])["pr_state"] == "CLOSED"

    out = ops.finish(parked["id"], "opened another one", pr_url=second)

    assert out["status"] == "waiting_pr_merge"
    row = store.get_work_order(parked["id"])
    assert row["status"] == "waiting_pr_merge", "a live pull request left the queue"
    assert row["pr_url"] == second


def test_rejecting_leaves_the_work_order_where_it_was(started, project, reviewable_pr):
    """A rejection is not an ending: the worker gets guidance and the PR is still its problem"""
    out = ops.review_work_order(reviewable_pr["id"], accept=False,
                                feedback="No — spaces, like the rest of the file")

    assert out["status"] == "needs_review"
    assert ProjectStore(project).get_work_order(
        reviewable_pr["id"])["status"] == "needs_review"


# -- the merge poll


@pytest.fixture()
def parked(started, project, fake_gh):
    """A work order finished behind PR, with `gh` ready to be told what happened"""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    return wo


def poll(daemon, store):
    """Run the poll step alone, without the rest of the tick around it"""
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def test_a_merged_pr_completes_the_work_order(started, project, fake_gh, parked):
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z")
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["status"] == "completed"
    assert row["pr_state"] == "MERGED"
    assert not row["needs_attention"]
    # the timeline has to say GitHub ended this, not the user
    events = [e for e in store.list_events(parked["id"]) if e["kind"] == "pr_merged"]
    assert len(events) == 1
    assert json.loads(events[0]["payload"])["merged_at"] == "2026-08-02T10:00:00Z"
    assert not any(e["kind"] == "marked_done" for e in store.list_events(parked["id"]))


def test_an_open_pr_is_left_exactly_where_it_was(started, project, fake_gh, parked):
    """The common case: one `gh` call, no write, no attention"""
    fake_gh.set_pr(PR, "OPEN")
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []


def test_a_closed_pr_asks_for_the_user(started, project, fake_gh, parked):
    """Closed without merging means the delivered work was refused — that needs them"""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["status"] == "needs_review"
    assert row["pr_state"] == "CLOSED"
    assert row["needs_attention"]
    assert row["attention_reason"] == PR_CLOSED_BLOCKER
    assert true_blockers(store, row) == [PR_CLOSED_BLOCKER]
    assert any(e["kind"] == "pr_closed" for e in store.list_events(parked["id"]))


def test_the_closed_pr_reason_survives_the_reconciler(started, project, fake_gh,
                                                      parked):
    """INV-ATTENTION-REASON rewrites any reason `true_blockers` cannot derive"""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)

    violations = list(check_project(store))

    assert [v.invariant for v in violations] == []
    assert store.get_work_order(parked["id"])["attention_reason"] == PR_CLOSED_BLOCKER


def test_a_closed_pr_is_only_reported_once(started, project, fake_gh, parked):
    """`needs_review` is polled now (issue #224)"""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)
    poll(started, store)

    closed = [e for e in store.list_events(parked["id"]) if e["kind"] == "pr_closed"]
    assert len(closed) == 1


def test_a_reopened_then_reclosed_pr_tells_the_user_again(started, project, fake_gh,
                                                          parked):
    """The PAIR of the test above, and the reason its guard is derived from the timeline rather than from..."""
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "CLOSED")
    poll(started, store)
    assert store.get_work_order(parked["id"])["attention_reason"] == PR_CLOSED_BLOCKER

    # Reopened: the refusal has been withdrawn, so the record must stop asserting it
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE")
    poll(started, store)
    row = store.get_work_order(parked["id"])
    assert row["pr_state"] is None
    assert PR_CLOSED_BLOCKER not in true_blockers(store, row)
    assert any(e["kind"] == "pr_reopened" for e in store.list_events(parked["id"]))
    assert [v.invariant for v in check_project(store)] == []

    fake_gh.set_pr(PR, "CLOSED")
    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["attention_reason"] == PR_CLOSED_BLOCKER
    closed = [e for e in store.list_events(parked["id"]) if e["kind"] == "pr_closed"]
    assert len(closed) == 2


def test_a_reopen_does_not_move_the_work_order_back_to_the_merge_queue(
        started, project, fake_gh, parked):
    """Reopening is a button press, not a decision"""
    store = ProjectStore(project)
    fake_gh.set_pr(PR, "CLOSED")
    poll(started, store)
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE")

    poll(started, store)

    assert store.get_work_order(parked["id"])["status"] == "needs_review"


def test_a_closed_pr_is_still_the_users_to_close(started, project, fake_gh, parked):
    """Refused work is not failed work: the ordinary exits still apply"""
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)
    poll(started, store)

    out = ops.mark_done(parked["id"])

    assert out["status"] == "completed"
    assert not ProjectStore(project).get_work_order(parked["id"])["needs_attention"]


def test_nothing_parked_means_no_subprocess(started, project, fake_gh):
    """A fleet with no open pull requests must not pay for this step at all"""
    ops.create_work_order("proj_a", "still going")
    ops.finish(ops.create_work_order("proj_a", "no PR here")["id"], "done")

    poll(started, ProjectStore(project))

    assert fake_gh.calls == []


def test_a_merged_pr_closes_the_backlog_item_behind_it(started, project, fake_gh):
    """Promotion links the two; only the ending was missing"""
    central = CentralStore()
    item = central.add_backlog("proj_a", "add feature X")
    wo = ops.promote_backlog(item["id"])
    ops.finish(wo["wo_id"], "opened a PR", pr_url=PR)
    fake_gh.set_pr(PR, "MERGED")
    store = ProjectStore(project)

    poll(started, store)

    row = central.get_backlog(item["id"])
    assert row is not None and row["status"] == "done"


def test_an_unreadable_gh_warns_once_and_leaves_the_work_order_parked(
        started, project, fake_gh, parked):
    """Silence would leave a dead feature the user never learns about"""
    fake_gh.fail("gh: not authenticated")
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    warnings = [n for n in store.unrouted_notifications() if n["source"] == "pr-poll"]
    assert len(warnings) == 1
    assert "auto-complete on merge is off" in warnings[0]["title"]
    assert "GH_TOKEN" in warnings[0]["body"], "a gh that RAN and failed is credentials"
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"


def test_a_gh_that_is_not_there_is_diagnosed_as_path_not_credentials(
        started, project, parked, monkeypatch):
    """Issue #90"""
    monkeypatch.setenv("JARVIS_GH_BIN", "/nonexistent/gh")
    monkeypatch.setenv("PATH", "/opt/only-this-dir")
    store = ProjectStore(project)

    poll(started, store)

    body = [n for n in store.unrouted_notifications()
            if n["source"] == "pr-poll"][0]["body"]
    assert "PATH" in body and "/opt/only-this-dir" in body
    assert "install_prod_service.sh" in body
    assert "GH_TOKEN" not in body, "gh never ran; credentials advice is a wrong lead"


def test_a_missing_gh_raises_the_type_that_says_so(monkeypatch):
    """The daemon branches on this type to pick its remedy"""
    monkeypatch.setenv("JARVIS_GH_BIN", "/nonexistent/gh")

    with pytest.raises(github.GhUnavailable):
        github.pr_view(PR)
    assert issubclass(github.GhUnavailable, github.GitHubError)


def test_one_unreadable_pr_does_not_hide_the_others(started, project, fake_gh, parked):
    """A deleted repo or a typo'd URL is one work order's problem, not the project's"""
    other = ops.create_work_order("proj_a", "add feature Y")
    ops.finish(other["id"], "opened a PR", pr_url="https://github.com/acme/proj/pull/8")
    fake_gh.set_pr("https://github.com/acme/proj/pull/8", "MERGED")  # PR itself unknown
    store = ProjectStore(project)

    poll(started, store)

    assert store.get_work_order(other["id"])["status"] == "completed"
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"


def test_the_poll_runs_on_its_own_cadence(started, project, fake_gh, parked):
    """Every tick would be four times the API calls for no perceptible gain"""
    fake_gh.set_pr(PR, "OPEN")

    started.tick_count = 0
    started.tick()                       # tick 1: polls
    assert len(fake_gh.calls) == 1
    for _ in range(PR_POLL_EVERY_TICKS - 1):
        started.tick()
    assert len(fake_gh.calls) == 1       # ... and not again until the cadence comes up
    started.tick()
    assert len(fake_gh.calls) == 2


def test_a_merge_notifies_nobody(started, project, fake_gh, parked):
    """`route_new_inbox` has no level filter, so any row here Telegrams the user"""
    fake_gh.set_pr(PR, "MERGED")
    store = ProjectStore(project)

    poll(started, store)

    assert [n for n in store.unrouted_notifications() if n["source"] == "pr-poll"] == []
    assert store.get_work_order(parked["id"])["status"] == "completed"


# -- healing a conflicting pull request


@pytest.fixture()
def parked_worker(project, parked):
    """`parked`, plus the worker session every real one has: `waiting_pr_merge` is reached through `jarvis..."""
    ProjectStore(project).update_work_order(parked["id"], session_id="sess-1")
    return parked


def conflicting(fake_gh, base: str = "main") -> None:
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref=base)


def delivered(store, wo_id: str) -> list[dict]:
    """Pretend the daemon delivered whatever is queued, and hand it back"""
    msgs = store.queued_messages(wo_id)
    for m in msgs:
        store.mark_message(m["id"], "delivered")
    return msgs


def test_a_conflicting_pr_asks_the_worker_to_resolve_it(started, project, fake_gh,
                                                        parked_worker):
    conflicting(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    msgs = store.queued_messages(parked_worker["id"])
    assert len(msgs) == 1
    assert msgs[0]["source"] == "pr-conflict"
    assert "origin/main" in msgs[0]["content"] and PR in msgs[0]["content"]
    # the two things the loop depends on the worker NOT doing (spec §3)
    assert "do NOT call `jarvis wo finish` again" in msgs[0]["content"]
    assert "Do NOT rebase or force-push" in msgs[0]["content"]
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1


def test_the_conflict_nudge_asks_the_user_for_nothing(started, project, fake_gh,
                                                      parked_worker):
    """The point of the feature: a conflict costs the user nothing until it cannot be healed"""
    conflicting(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked_worker["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []
    assert ops.os_status()["attention"] == []


def test_a_second_poll_does_not_nudge_twice(started, project, fake_gh, parked_worker):
    """Between queueing and delivery the work order is still parked_worker"""
    conflicting(fake_gh)
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    assert len(store.queued_messages(parked_worker["id"])) == 1
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1


def test_a_work_order_with_no_session_is_left_alone(started, project, fake_gh, parked):
    """`parked` without the session stamp: `deliver_messages` skips a work order with no conversation"""
    conflicting(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(parked["id"])
    assert store.pr_repair_attempts(parked["id"], "conflict") == 0
    assert not store.get_work_order(parked["id"])["needs_attention"]


def test_the_worker_is_woken_and_parks_itself_back(started, project, fake_gh,
                                                   fake_claude, settle_turns):
    """End to end through the real tick: the conflict resumes the finished session and settlement returns..."""
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    conflicting(fake_gh)

    started.tick_count = 0               # a tick that polls: queues the nudge
    started.tick()
    assert store.queued_messages(wo["id"])

    started.tick_count = 1               # a tick that delivers but does not poll
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "running"
    assert settle_turns(store)

    started.tick_count = 1               # and settlement puts it back where it was
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_three_attempts_and_then_it_is_the_users_problem(started, project, fake_gh,
                                                         parked_worker):
    """A conflict that survives three merges is usually the stacked-PR trap of kn-0a5c449c"""
    conflicting(fake_gh)
    store = ProjectStore(project)

    for _ in range(PR_REPAIR_MAX_ATTEMPTS):
        poll(started, store)
        assert delivered(store, parked_worker["id"])
    assert not store.get_work_order(parked_worker["id"])["needs_attention"]  # still trying

    poll(started, store)

    row = store.get_work_order(parked_worker["id"])
    assert row["status"] == "waiting_pr_merge"
    assert row["needs_attention"]
    assert row["attention_reason"] == PR_CONFLICT_BLOCKER
    assert true_blockers(store, row) == [PR_CONFLICT_BLOCKER]
    assert not store.queued_messages(parked_worker["id"])   # it stopped asking
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == PR_REPAIR_MAX_ATTEMPTS


def test_giving_up_is_recorded_once_however_long_it_stays_broken(started, project,
                                                                 fake_gh, parked_worker):
    conflicting(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS):
        poll(started, store)
        delivered(store, parked_worker["id"])

    for _ in range(3):
        poll(started, store)

    assert len([e for e in store.list_events(parked_worker["id"])
                if e["kind"] == "pr_conflict_unresolved"]) == 1


def test_the_give_up_reason_survives_the_reconciler(started, project, fake_gh, parked_worker):
    """`true_blockers` is the only source of attention reasons"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, parked_worker["id"])
    store.clear_attention(parked_worker["id"])   # as a delivered nudge would have

    violations = list(check_project(store))

    assert [v.invariant for v in violations] == ["INV-ATTENTION-MISSING"]
    row = store.get_work_order(parked_worker["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == PR_CONFLICT_BLOCKER


def test_a_healthy_parked_work_order_is_still_never_flagged(started, project, fake_gh,
                                                            parked_worker):
    """`waiting_pr_merge` derives a blocker for the conflict case alone"""
    fake_gh.set_pr(PR, "OPEN")
    store = ProjectStore(project)

    poll(started, store)

    assert [v.invariant for v in check_project(store)] == []
    assert not store.get_work_order(parked_worker["id"])["needs_attention"]


def test_resolving_the_conflict_clears_it_and_restores_the_budget(started, project,
                                                                  fake_gh, parked_worker):
    """The budget is per episode: a branch that conflicts again next week gets three fresh attempts"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, parked_worker["id"])
    assert store.get_work_order(parked_worker["id"])["needs_attention"]

    fake_gh.set_pr(PR, "OPEN")            # the worker got there in the end
    poll(started, store)

    row = store.get_work_order(parked_worker["id"])
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 0
    assert any(e["kind"] == "pr_conflict_cleared"
               for e in store.list_events(parked_worker["id"]))

    conflicting(fake_gh)                  # and it may conflict all over again
    poll(started, store)
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1


def test_clearing_a_conflict_leaves_an_unrelated_flag_alone(started, project, fake_gh,
                                                            parked_worker):
    conflicting(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    delivered(store, parked_worker["id"])
    store.flag_attention(parked_worker["id"], "something else entirely")

    fake_gh.set_pr(PR, "OPEN")
    poll(started, store)

    row = store.get_work_order(parked_worker["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == "something else entirely"


def test_a_pr_that_merged_after_conflicting_still_completes(started, project, fake_gh,
                                                            parked_worker):
    conflicting(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    delivered(store, parked_worker["id"])

    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-22T10:00:00Z")
    poll(started, store)

    row = store.get_work_order(parked_worker["id"])
    assert row["status"] == "completed"
    assert not row["needs_attention"]


def test_unknown_mergeability_is_not_a_conflict(started, project, fake_gh, parked_worker):
    """GitHub computes mergeability lazily and asking is what starts it"""
    fake_gh.set_pr(PR, "OPEN", mergeable="UNKNOWN")
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(parked_worker["id"])
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 0


def test_unknown_mergeability_does_not_clear_a_conflict_either(started, project,
                                                              fake_gh, parked_worker):
    """"Not known to conflict" is not "known to merge\""""
    conflicting(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    delivered(store, parked_worker["id"])

    fake_gh.set_pr(PR, "OPEN", mergeable="UNKNOWN")
    poll(started, store)

    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1


def test_the_timeline_shows_the_attempts_and_credits_nobody_with_them(
        started, project, fake_gh, parked_worker):
    """When the flag finally fires, the user has to see what was already tried without asking"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, parked_worker["id"])

    entries = build_timeline(store.get_work_order(parked_worker["id"]),
                             store.list_events(parked_worker["id"]),
                             store.list_messages(parked_worker["id"]))
    labels = [e["label"] for e in entries]

    assert labels.count("Merge conflict — asked the worker to resolve it") == 3
    assert "Merge conflict the worker could not resolve — over to you" in labels
    assert labels.count("Jarvis messaged the worker") == 3
    assert "You messaged the worker" not in labels
    assert [e["detail"] for e in entries if e["kind"] == "pr_conflict_nudged"] == [
        "attempt 1 of 3", "attempt 2 of 3", "attempt 3 of 3"]


# -- reading GitHub


def test_pr_view_parses_the_state(fake_gh):
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z")

    pr = github.pr_view(PR)

    assert pr.merged and not pr.closed_unmerged
    assert pr.merged_at == "2026-08-02T10:00:00Z"
    assert github.pr_view(PR).state == "MERGED"


def test_pr_view_reads_mergeability_and_the_base_branch(fake_gh):
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="develop")

    pr = github.pr_view(PR)

    assert pr.conflicting and not pr.mergeable_now
    assert pr.base_ref == "develop"
    assert github.pr_view(PR).state == "OPEN"


def test_a_pr_with_no_mergeability_is_no_conflict(fake_gh):
    """Merged and closed pull requests answer null"""
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-22T10:00:00Z")

    pr = github.pr_view(PR)

    assert not pr.conflicting and not pr.mergeable_now
    assert pr.mergeable is None


def test_pr_view_reads_the_head_sha_and_survives_its_absence(fake_gh):
    """`headRefOid` is the one field this repository's landing sweep added to PR_FIELDS"""
    sha = "9f1c2ab4d5e6f708192a3b4c5d6e7f8091a2b3c4"
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z", head_oid=sha)

    assert github.pr_view(PR).head_oid == sha

    # And the fleet that predates the field: GitHub answers, the sha is simply absent
    fake_gh.set_pr(OTHER_PR, "MERGED", merged_at="2026-08-02T10:00:00Z")
    assert github.pr_view(OTHER_PR).head_oid == ""


def test_pr_view_raises_rather_than_guessing(fake_gh):
    """An unreadable pull request must never look like an unmerged one: guessing OPEN parks a merged work..."""
    fake_gh.fail("HTTP 401: Bad credentials")

    with pytest.raises(github.GitHubError) as e:
        github.pr_view(PR)
    assert "Bad credentials" in str(e.value)


def test_the_isolation_gate_stops_a_test_reaching_real_github():
    """No `fake_gh` fixture: the blocked stub must answer, not the real `gh`"""
    with pytest.raises(github.GitHubError):
        github.pr_view(PR)


# -- the PR title prefix


@pytest.mark.parametrize("command", [
    'gh pr create --title "Add feature X" --body-file /tmp/b.md',
    "gh pr create --title='Add feature X'",
    'gh pr create -t "Add feature X"',
    'cd /tmp/wt && gh pr create --title "Add feature X"',
    # gh is often not on a worker's PATH, so an absolute path is the normal invocation
    '/snap/bin/gh pr create --title "Add feature X"',
])
def test_gh_pr_create_without_the_prefix_is_denied(command):
    out = hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-1234abcd"},
    )
    assert out is not None
    hook = out["hookSpecificOutput"]
    assert hook["permissionDecision"] == "deny"
    # the fix has to be one copy-paste, so the exact required title is in the reason
    assert '--title "[wo-1234abcd] Add feature X"' in hook["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    'gh pr create --title "[wo-1234abcd] Add feature X" --body-file /tmp/b.md',
    "gh pr create --title='[wo-1234abcd] Add feature X'",
])
def test_a_prefixed_title_passes(command):
    assert hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-1234abcd"},
    ) is None


@pytest.mark.parametrize("command", [
    "gh pr create --fill",                      # no title of ours to judge
    "gh pr list --state open",                  # not a create
    "gh pr merge 7",                            # a gate's business, not this hook's
    'git commit -m "gh pr create --title x"',   # the words, not the command
    'echo "unbalanced',                         # unparseable: never our call
])
def test_the_hook_keeps_out_of_everything_else(command):
    assert hooks.preflight_decision(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"JARVIS_WO_ID": "wo-1234abcd"},
    ) is None


def test_interactive_sessions_are_untouched():
    """No JARVIS_WO_ID: a human in a managed repo has no work order to name"""
    assert hooks.preflight_decision(
        {"tool_name": "Bash",
         "tool_input": {"command": 'gh pr create --title "Add feature X"'}},
        {},
    ) is None


def test_the_worker_briefing_names_the_required_prefix(catalog_file):
    """A rule enforced only by a hook is a trap; the contract has to state it first"""
    from jarvis.dispatch import build_worker_prompt

    prompt = build_worker_prompt(
        {"id": "wo-test", "title": "add feature X", "description": "d"},
        load_catalog(catalog_file).projects[0], knowledge=[])

    assert "[wo-test] " in prompt
    assert "--pr <url>" in prompt


def test_the_operation_contract_names_it_too(started, project):
    """OPERATION.md is what a worker reads when the briefing is not in front of it"""
    contract = (project / "OPERATION.md").read_text()

    assert "[$JARVIS_WO_ID]" in contract
    assert "--pr <url>" in contract


# -- a repair attempt the worker was never allowed to make


def gate(store, wo_id: str, kind: str = "pr_merge", status: str = "pending") -> dict:
    """A privileged-action request sitting in front of a reviewer (#469)"""
    return store.add_approval(wo_id, kind, f"<gated command for {PR}>", status=status)


def argue(store, request: dict) -> dict:
    """The worker making its case — the one door out of `awaiting_case`"""
    neo = NeoStore()
    try:
        gates.queue_for_review(
            store, neo, "proj_a", store.get_work_order(request["wo_id"]),
            gates.GatedAction(kind=request["kind"], summary="<privileged action>",
                              command=request["command"],
                              matched=request["command"]),
            request)
    finally:
        neo.close()
    return store.get_approval(request["id"])


def test_a_pending_gate_defers_the_nudge_instead_of_spending_an_attempt(
        started, project, fake_gh, parked_worker):
    """THE defect"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    gate(store, parked_worker["id"])

    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)

    assert not store.queued_messages(parked_worker["id"])
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 0
    row = store.get_work_order(parked_worker["id"])
    assert not row["needs_attention"]          # the GATE is the item, not the conflict
    assert true_blockers(store, row) == []


def test_the_deferral_is_recorded_once_however_long_the_review_takes(
        started, project, fake_gh, parked_worker):
    """Six hours of two-minute polls is 180 rows if this is not deduped"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"])

    for _ in range(5):
        poll(started, store)

    deferred = [e for e in store.list_events(parked_worker["id"])
                if e["kind"] == "pr_conflict_deferred"]
    assert len(deferred) == 1
    assert json.loads(deferred[0]["payload"])["approval_id"] == request["id"]
    entries = build_timeline(store.get_work_order(parked_worker["id"]),
                             store.list_events(parked_worker["id"]), [])
    assert [e["label"] for e in entries
            if e["kind"] == "pr_conflict_deferred"] == [
        f"Merge conflict — waiting for gate {request['id']} before asking the worker"]


def test_deciding_the_gate_lets_the_repair_start(started, project, fake_gh,
                                                 parked_worker):
    """Nothing was spent, so resuming is just the next tick doing its job"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"])
    poll(started, store)
    store.decide_approval(request["id"], "approved", "go ahead", "user")

    poll(started, store)

    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1
    assert store.queued_messages(parked_worker["id"])[0]["source"] == "pr-conflict"


def test_a_gate_the_worker_must_still_argue_does_not_defer_anything(
        started, project, fake_gh, parked_worker):
    """`awaiting_case` blocks the END of a turn, not the work in it — so it is not an excuse"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    gate(store, parked_worker["id"], status="awaiting_case")

    poll(started, store)

    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1


def test_a_budget_spent_entirely_behind_a_gate_is_given_back_when_it_is_decided(
        started, project, fake_gh, parked_worker):
    """The other half: an order already stranded when this shipped"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"])
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):        # as the poller did before #469
        ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                            ops.PR_CONFLICT, base="main")
        delivered(store, parked_worker["id"])
    assert store.get_work_order(parked_worker["id"])["attention_reason"] == \
        PR_CONFLICT_BLOCKER

    store.decide_approval(request["id"], "approved", "go ahead", "user")
    poll(started, store)

    events = [e["kind"] for e in store.list_events(parked_worker["id"])]
    assert "pr_conflict_rearmed" in events
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1   # of 3
    assert store.queued_messages(parked_worker["id"])[0]["source"] == "pr-conflict"
    row = store.get_work_order(parked_worker["id"])
    assert not row["needs_attention"]
    assert true_blockers(store, row) == []


def test_a_budget_the_worker_actually_spent_is_not_given_back(started, project,
                                                              fake_gh, parked_worker):
    """The negative control, and the one that keeps the give-up meaning something: the worker was let at..."""
    conflicting(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, parked_worker["id"])
    request = gate(store, parked_worker["id"])          # filed AFTER the attempts
    store.decide_approval(request["id"], "approved", "go ahead", "user")

    poll(started, store)

    events = [e["kind"] for e in store.list_events(parked_worker["id"])]
    assert "pr_conflict_rearmed" not in events
    assert store.get_work_order(parked_worker["id"])["attention_reason"] == \
        PR_CONFLICT_BLOCKER


def test_one_genuine_attempt_among_the_refusals_keeps_the_give_up(
        started, project, fake_gh, parked_worker):
    """EVERY nudge, not some of them: a partial refund would need the episode boundary to be a number..."""
    conflicting(fake_gh)
    store = ProjectStore(project)
    poll(started, store)                                # attempt 1: nothing in the way
    delivered(store, parked_worker["id"])
    request = gate(store, parked_worker["id"])
    for _ in range(PR_REPAIR_MAX_ATTEMPTS):
        ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                            ops.PR_CONFLICT, base="main")
        delivered(store, parked_worker["id"])
    store.decide_approval(request["id"], "approved", "go ahead", "user")

    poll(started, store)

    assert "pr_conflict_rearmed" not in [e["kind"] for e in
                                         store.list_events(parked_worker["id"])]


def test_the_refund_happens_once_and_the_loop_terminates(started, project, fake_gh,
                                                         parked_worker):
    """A re-armed episode gives up again for real"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"])
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                            ops.PR_CONFLICT, base="main")
        delivered(store, parked_worker["id"])
    store.decide_approval(request["id"], "approved", "go ahead", "user")

    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 2):
        poll(started, store)
        delivered(store, parked_worker["id"])

    events = [e["kind"] for e in store.list_events(parked_worker["id"])]
    assert events.count("pr_conflict_rearmed") == 1
    row = store.get_work_order(parked_worker["id"])
    assert row["attention_reason"] == PR_CONFLICT_BLOCKER      # and it means it now


def test_a_request_nobody_ever_argued_never_blocked_anything(started, project, fake_gh,
                                                             parked_worker):
    """`gate_open_at` excludes a request abandoned unargued: it never reached the status that refuses a..."""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"], status="awaiting_case")
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, parked_worker["id"])
    store.conn.execute(
        "UPDATE approvals SET status='expired', closed_as='abandoned', decided_at=? "
        "WHERE id=?", (db.now(), request["id"]))

    poll(started, store)

    assert "pr_conflict_rearmed" not in [e["kind"] for e in
                                         store.list_events(parked_worker["id"])]


def test_a_held_request_nobody_argues_never_refunds_anything(started, project, fake_gh,
                                                             parked_worker):
    """The loop this change nearly shipped, pinned shut (#469)"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    gate(store, parked_worker["id"], status="awaiting_case")

    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 2):
        poll(started, store)
        delivered(store, parked_worker["id"])

    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == \
        PR_REPAIR_MAX_ATTEMPTS
    assert store.get_work_order(parked_worker["id"])["attention_reason"] == \
        PR_CONFLICT_BLOCKER
    assert "pr_conflict_rearmed" not in [e["kind"] for e in
                                         store.list_events(parked_worker["id"])]

    fake_gh.set_pr(PR, "OPEN")                    # the user resolved it by hand
    poll(started, store)
    conflicting(fake_gh)                          # and the base moved again
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 2):
        poll(started, store)
        delivered(store, parked_worker["id"])

    events = [e["kind"] for e in store.list_events(parked_worker["id"])]
    assert "pr_conflict_rearmed" not in events
    assert events.count("pr_conflict_unresolved") == 2
    assert store.get_work_order(parked_worker["id"])["attention_reason"] == \
        PR_CONFLICT_BLOCKER


def test_the_refund_is_capped_at_one_per_work_order(started, project, fake_gh,
                                                    parked_worker):
    """The braces, tested apart from the belt: a second episode that satisfies every refund condition is..."""
    conflicting(fake_gh)
    store = ProjectStore(project)
    for episode in range(2):
        request = gate(store, parked_worker["id"])
        for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):   # as the poller did before #469
            ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                                ops.PR_CONFLICT, base="main")
            delivered(store, parked_worker["id"])
        store.decide_approval(request["id"], "approved", "go ahead", "user")
        poll(started, store)
        delivered(store, parked_worker["id"])
        if episode == 0:                              # clear it, so the next is fresh
            fake_gh.set_pr(PR, "OPEN")
            poll(started, store)
            conflicting(fake_gh)

    events = [e["kind"] for e in store.list_events(parked_worker["id"])]
    assert events.count("pr_conflict_rearmed") == 1


def test_the_migration_refunds_an_order_stranded_before_this_shipped(
        started, project, fake_gh, parked_worker):
    """`pending_at` is a NEW column, so every row that was already stranded has none (#469)"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"])
    store.conn.execute("UPDATE approvals SET pending_at=NULL WHERE id=?",
                       (request["id"],))          # exactly how the row stood on disk
    store.conn.commit()
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):        # as the poller did before #469
        ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                            ops.PR_CONFLICT, base="main")
        delivered(store, parked_worker["id"])
    store.decide_approval(request["id"], "approved", "go ahead", "user")
    assert store.get_approval(request["id"])["pending_at"] is None
    store.close()

    store = ProjectStore(project)                      # `_migrate` runs here
    row = store.get_approval(request["id"])
    assert row["pending_at"] == row["ts"]              # the filing time, exact for this
    poll(started, store)                               # ...and the refund it unlocks

    assert "pr_conflict_rearmed" in [e["kind"] for e in
                                     store.list_events(parked_worker["id"])]
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1   # of 3
    assert not store.get_work_order(parked_worker["id"])["needs_attention"]


def test_a_nudge_sent_before_the_case_was_made_is_a_real_attempt(
        started, project, fake_gh, parked_worker):
    """The near side of the `[pending_at, decided_at)` boundary"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"], status="awaiting_case")

    poll(started, store)                               # attempt 1: held, not pending
    delivered(store, parked_worker["id"])
    argue(store, request)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS):            # the rest, behind a shut gate
        ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                            ops.PR_CONFLICT, base="main")
        delivered(store, parked_worker["id"])
    store.decide_approval(request["id"], "approved", "go ahead", "user")

    poll(started, store)

    assert "pr_conflict_rearmed" not in [e["kind"] for e in
                                         store.list_events(parked_worker["id"])]
    assert store.get_work_order(parked_worker["id"])["attention_reason"] == \
        PR_CONFLICT_BLOCKER


def test_a_held_request_starts_blocking_the_moment_the_case_is_made(
        started, project, fake_gh, parked_worker):
    """The far side of the same boundary"""
    conflicting(fake_gh)
    store = ProjectStore(project)
    request = gate(store, parked_worker["id"], status="awaiting_case")

    argued = argue(store, request)
    assert argued["status"] == "pending"
    assert argued["pending_at"] >= argued["ts"]
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        ops.nudge_pr_repair(store, store.get_work_order(parked_worker["id"]),
                            ops.PR_CONFLICT, base="main")
        delivered(store, parked_worker["id"])
    store.decide_approval(request["id"], "approved", "go ahead", "user")

    poll(started, store)

    assert "pr_conflict_rearmed" in [e["kind"] for e in
                                     store.list_events(parked_worker["id"])]
    assert store.pr_repair_attempts(parked_worker["id"], "conflict") == 1   # of 3


# -- `jarvis wo pr`: the pull request the worker could not open (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md)

BRANCH = "worktree-wo-pushed"


def pr_pending(store, wo_id: str, *, compare_url: str = "",
               branch: str = BRANCH) -> None:
    store.add_event(wo_id, "pr_pending",
                    {"branch": branch, "base": "origin/main", "commits": 2,
                     "dirty": [], "pushed": True, "compare_url": compare_url,
                     "was": "running"})
    store.set_status(wo_id, "needs_review")


@pytest.fixture()
def pushed(started, project):
    """An order parked on a pushed branch with no pull request."""
    wo = ops.create_work_order("proj_a", "add feature X")
    store = ProjectStore(project)
    try:
        store.add_event(wo["id"], "finished", {"summary": "pushed the branch"})
        pr_pending(store, wo["id"])
    finally:
        store.close()
    return wo


def test_the_recorded_url_routes_and_the_merge_poll_picks_the_order_up(
        started, project, fake_gh, pushed):
    """§8.1, AND IT IS THE FIRST TEST FOR A REASON"""
    out = ops.record_pull_request(pushed["id"], PR)

    assert out["status"] == "waiting_pr_merge"
    store = ProjectStore(project)
    row = store.get_work_order(pushed["id"])
    assert row["pr_url"] == PR
    assert ops.routes_on_pull_request(store, row) is True
    assert store.pr_pending_open(pushed["id"]) is None      # the episode is closed
    assert not row["needs_attention"]

    fake_gh.set_pr(PR, "MERGED", merged_at="2026-10-12T10:00:00Z")
    poll(started, store)

    assert store.get_work_order(pushed["id"])["status"] == "completed"


def test_a_gate_recorded_url_still_routes_nothing(started, project):
    """The asymmetry §8.1 keeps"""
    wo = ops.create_work_order("proj_a", "a planner")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url=PR)
        store.add_event(wo["id"], "pr_url_recorded", {"pr_url": PR, "source": "gate"})
        row = store.get_work_order(wo["id"])

        assert ops.declared_pull_request(store, row) == ""
        assert ops.routes_on_pull_request(store, row) is False
    finally:
        store.close()


@pytest.mark.parametrize("source", ["user", "auto"])
def test_a_recorded_url_the_user_or_the_os_asserted_is_a_declaration(started, project,
                                                                     source):
    """§8.2: the auto-attach routes too, and that is decided rather than inherited."""
    wo = ops.create_work_order("proj_a", "add feature X")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url=PR)
        store.add_event(wo["id"], "pr_url_recorded", {"pr_url": PR, "source": source})
        row = store.get_work_order(wo["id"])

        assert ops.declared_pull_request(store, row) == PR
        assert ops.routes_on_pull_request(store, row) is True
    finally:
        store.close()


def test_it_refuses_an_order_that_already_declared_one(started, project, pushed):
    ops.record_pull_request(pushed["id"], PR)

    with pytest.raises(ops.OpsError, match="already"):
        ops.record_pull_request(pushed["id"], OTHER_PR)


def test_it_refuses_an_order_that_is_not_waiting_on_a_pull_request(started, project):
    """And says what the order IS waiting on, so the refusal is not a dead end."""
    wo = ops.create_work_order("proj_a", "add feature X")
    store = ProjectStore(project)
    try:
        ops.assume(wo["id"], "polled every 30s")
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    with pytest.raises(ops.OpsError, match="assumption"):
        ops.record_pull_request(wo["id"], PR)


@pytest.mark.parametrize("bad", ["not a url", "-repo=someone/else",
                                 "https://github.com/acme/proj/compare/a...b"])
def test_it_refuses_a_url_that_is_not_a_pull_request_on_this_project(started, project,
                                                                     pushed, bad):
    """`github.checked_pr_url` already does exactly this, including the leading-`-`-is- a-flag case and the compare URL §10 must never let into the column"""
    with pytest.raises(ops.OpsError):
        ops.record_pull_request(pushed["id"], bad)

    store = ProjectStore(project)
    assert not store.get_work_order(pushed["id"])["pr_url"]


def test_it_refuses_a_work_order_that_does_not_exist(started, project):
    with pytest.raises(ops.OpsError):
        ops.record_pull_request("wo-deadbeef", PR)


def test_a_pending_assumption_keeps_the_status_and_the_accept_lands_it(started, project,
                                                                       pushed):
    """§8's status rule"""
    ops.assume(pushed["id"], "polled every 30s")

    out = ops.record_pull_request(pushed["id"], PR)

    assert out["status"] == "needs_review"
    store = ProjectStore(project)
    assert store.get_work_order(pushed["id"])["pr_url"] == PR
    assert store.pr_pending_open(pushed["id"]) is None

    assert ops.review_work_order(pushed["id"], accept=True)["status"] == \
        "waiting_pr_merge"


def test_cli_wo_pr_records_it(started, project, pushed, capsys):
    cli.main(["wo", "pr", pushed["id"], PR])

    store = ProjectStore(project)
    assert store.get_work_order(pushed["id"])["pr_url"] == PR
    assert PR in capsys.readouterr().out


# -- the daemon's auto-attach (§9)


def test_the_poll_attaches_an_open_pull_request_found_on_the_parked_branch(
        started, project, fake_gh, pushed):
    """§9"""
    fake_gh.set_open_pr(BRANCH, PR)
    store = ProjectStore(project)

    started.attach_pull_requests(started.catalog.project("proj_a"), store)

    row = store.get_work_order(pushed["id"])
    assert row["pr_url"] == PR
    assert row["status"] == "waiting_pr_merge"
    assert ops.routes_on_pull_request(store, row) is True
    assert json.loads(store.events_of_kind(pushed["id"], "pr_url_recorded")[-1]
                      ["payload"])["source"] == "auto"


def test_no_open_pull_request_on_the_branch_changes_nothing(started, project, fake_gh,
                                                            pushed):
    store = ProjectStore(project)

    started.attach_pull_requests(started.catalog.project("proj_a"), store)

    assert not store.get_work_order(pushed["id"])["pr_url"]
    assert store.pr_pending_open(pushed["id"]) is not None


def test_the_auto_attach_degrades_silently_when_gh_raises(started, project, fake_gh,
                                                          pushed):
    """`poll_pull_requests`' own treatment of `GhUnavailable` and `GitHubError`"""
    fake_gh.fail("gh: not authenticated")
    store = ProjectStore(project)

    started.attach_pull_requests(started.catalog.project("proj_a"), store)

    assert not store.get_work_order(pushed["id"])["pr_url"]
    assert store.get_work_order(pushed["id"])["status"] == "needs_review"


def test_nothing_parked_means_no_subprocess_for_the_attach(started, project, fake_gh):
    ops.create_work_order("proj_a", "add feature X")
    store = ProjectStore(project)

    started.attach_pull_requests(started.catalog.project("proj_a"), store)

    assert fake_gh.calls == []
