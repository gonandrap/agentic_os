"""A work order must never sit on a red pull request — issue #224.

docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md.

The conflict half of this machinery is tested beside the merge poll in
`test_wo_pr_merge.py` and is NOT re-tested here: both repairs run through the same
`ops.PrRepair`, so duplicating the guard tests would only pin the copy. What is new is
the vocabulary — a check that is not green is not the same as a red one — and the trap
the live case (wo-a6af01f0) walked into: a nudge on a `needs_review` work order must not
take the user's review item off their list.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import (
    IDLE_NO_FINISH_BLOCKER,
    PR_CHECKS_BLOCKER,
    PR_CLOSED_BLOCKER,
    PR_CONFLICT_BLOCKER,
    PR_REPAIR_MAX_ATTEMPTS,
    VALIDATION_STUCK_BLOCKER,
    check_project,
    true_blockers,
)
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def check(name: str, conclusion: str, status: str = "COMPLETED") -> dict:
    """One `statusCheckRollup` entry in GitHub's CheckRun shape."""
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


#: The live case, from run 34741340610 on wo-a6af01f0's branch: one job failed and
#: fail-fast CANCELLED its two siblings. Both halves matter — the failure is what must
#: nudge, and the cancellations are what must not be read as three separate failures.
RED = [check("unit (3.11)", "FAILURE"), check("unit (3.12)", "CANCELLED"),
       check("unit (3.13)", "CANCELLED"), check("evals", "SUCCESS"),
       check("browser", "SUCCESS")]
GREEN = [check("unit (3.11)", "SUCCESS"), check("evals", "SUCCESS")]


def poll(daemon, store):
    """The poll step alone, without the rest of the tick around it."""
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


@pytest.fixture()
def parked(started, project, fake_gh):
    """Finished behind a pull request, with the session every real one has."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    ProjectStore(project).update_work_order(wo["id"], session_id="sess-1")
    return wo


@pytest.fixture()
def reviewing(started, project, parked):
    """`parked`, then escalated into `needs_review` — the live case's shape.

    This is the status the defect was invisible in: the user is about to look at the
    pull request and decide whether to merge it, and it is exactly the status the poll
    did not select.
    """
    store = ProjectStore(project)
    store.set_status(parked["id"], "needs_review")
    store.flag_attention(parked["id"], IDLE_NO_FINISH_BLOCKER)
    return parked


def red(fake_gh, checks=None, merge_state: str | None = None) -> None:
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main",
                   checks=RED if checks is None else checks, merge_state=merge_state)


def delivered(store, wo_id: str) -> list[dict]:
    """Pretend the daemon delivered whatever is queued, and hand it back."""
    msgs = store.queued_messages(wo_id)
    for m in msgs:
        store.mark_message(m["id"], "delivered")
    return msgs


# -- the regression -----------------------------------------------------------------


def test_a_needs_review_work_order_with_a_red_pr_is_nudged(started, project, fake_gh,
                                                           reviewing):
    """THE defect. wo-a6af01f0 sat here with three failing unit jobs and the OS said
    nothing: the poll selected `waiting_pr_merge` alone and never asked about CI."""
    red(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    msgs = store.queued_messages(reviewing["id"])
    assert len(msgs) == 1
    assert msgs[0]["source"] == "pr-checks"
    assert PR in msgs[0]["content"]
    assert "unit (3.11)" in msgs[0]["content"]      # names what is red
    assert "do NOT call `jarvis wo finish` again" in msgs[0]["content"]
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 1


def test_the_red_ci_nudge_leaves_the_review_item_on_the_users_list(
        started, project, fake_gh, reviewing):
    """THE TRAP. A red pull request in `needs_review` means the USER is the one being
    asked to merge something broken, so the nudge must reach the worker WITHOUT the OS
    quietly taking the item off their list."""
    red(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert row["status"] == "needs_review"
    assert row["needs_attention"]
    assert true_blockers(store, row) == [IDLE_NO_FINISH_BLOCKER]
    assert [v.invariant for v in check_project(store)] == []


def test_the_repair_turn_gives_the_work_order_back_to_needs_review(
        started, project, fake_gh, fake_claude, settle_turns):
    """The other half of the trap, and Neo's ruling on question 275: `settle_work_order`
    parks a done turn carrying a summary and a PR into `waiting_pr_merge`, which would
    end the repair by silently downgrading a review item into a merge queue entry."""
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
    red(fake_gh)

    started.tick_count = 0               # a tick that polls: queues the nudge
    started.tick()
    assert store.queued_messages(wo["id"])

    started.tick_count = 1               # a tick that delivers but does not poll
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "running"
    assert settle_turns(store)

    started.tick_count = 1               # ... and settlement puts it back where it was
    started.tick()
    assert store.get_work_order(wo["id"])["status"] == "needs_review"

    # The flag follows the status rather than being carried through the repair, which is
    # the whole of Neo's ruling: INV-ATTENTION-MISSING re-derives it, so there is no
    # second piece of state to keep in step. The blind window is the repair turn plus one
    # reconcile — bounded, and the alternative was teaching `true_blockers` what an
    # in-flight repair is.
    started.tick_count = 6               # a tick that reconciles but does not poll
    started.tick()
    row = store.get_work_order(wo["id"])
    assert row["status"] == "needs_review"
    assert row["needs_attention"]
    assert row["attention_reason"] == IDLE_NO_FINISH_BLOCKER


def test_a_parked_work_order_still_goes_back_to_the_merge_queue(
        started, project, fake_gh, fake_claude, settle_turns):
    """The negative half of the test above: restoring the ORIGIN must not change the
    ordinary case, where the origin is `waiting_pr_merge` and always was."""
    wo = ops.create_work_order("proj_a", "add feature X")
    started.tick()
    store = ProjectStore(project)
    assert settle_turns(store)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    red(fake_gh)

    started.tick_count = 0
    started.tick()
    started.tick_count = 1
    started.tick()
    assert settle_turns(store)
    started.tick_count = 1
    started.tick()

    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]


# -- the negative controls ----------------------------------------------------------
#
# kn-67364b3a: the half that rots. Every one of these is a state that is NOT green and
# must still nudge nobody.


def test_a_pending_check_nudges_nobody(started, project, fake_gh, reviewing):
    """A run in flight is not a failure. Nudging here would ask a worker to fix a test
    that has not finished telling anyone whether it passed."""
    red(fake_gh, [check("unit (3.11)", "", status="IN_PROGRESS"),
                  check("evals", "", status="QUEUED")])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 0
    assert store.get_work_order(reviewing["id"])["status"] == "needs_review"


def test_a_failure_beside_a_pending_check_still_nudges(started, project, fake_gh,
                                                       reviewing):
    """...but a failure that has already landed is a fact, and waiting for the rest of
    the matrix would just delay the same message by a tick."""
    red(fake_gh, [check("unit (3.11)", "FAILURE"),
                  check("unit (3.12)", "", status="IN_PROGRESS")])
    store = ProjectStore(project)

    poll(started, store)

    assert len(store.queued_messages(reviewing["id"])) == 1


def test_a_cancelled_check_is_not_a_failure(started, project, fake_gh, reviewing):
    """CANCELLED is what fail-fast does to the siblings of a job that failed, and what a
    human does to a run they no longer want. Neither is the code being wrong, and
    neither is something a worker can fix by editing it — it wants a re-run."""
    red(fake_gh, [check("unit (3.12)", "CANCELLED"), check("evals", "SUCCESS")])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])


def test_a_skipped_check_is_not_a_failure(started, project, fake_gh, reviewing):
    red(fake_gh, [check("browser", "SKIPPED"), check("evals", "SUCCESS")])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])


def test_a_pull_request_with_no_checks_is_not_red(started, project, fake_gh, reviewing):
    """An empty rollup is a repository that runs no CI, which is not the same as every
    check failing and must not be rendered as though it were."""
    red(fake_gh, [])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])


def test_a_green_pull_request_costs_one_call_three_reads_and_no_write(
        started, project, fake_gh, reviewing):
    """The overwhelmingly common case stays the cheap one — and the budget is COUNTED,
    not described.

    `poll_pull_requests` states this cost in its docstring, and the sentence had already
    drifted: it claimed one indexed read while the rewritten body performed several. A
    prose budget nobody executes is a comment, not a guarantee, so the statements are
    read off the connection here. The three per pull request are one question each —
    was this closure already reported, is a conflict episode open, is a checks episode
    open — and a fourth appearing means somebody put a query on the path every open
    pull request in the fleet pays for every two minutes.
    """
    red(fake_gh, GREEN)
    store = ProjectStore(project)
    before = store.list_events(reviewing["id"])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]]) == 1
    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []
    assert len([s for s in sql if "wo_events" in s]) == 3
    # ...and the work-order query is the step's one, for the whole project, not one per
    # pull request: the row is re-read only when a clear has just taken a flag down.
    assert len([s for s in sql if "wo_events" not in s]) == 1
    assert not store.queued_messages(reviewing["id"])
    assert store.list_events(reviewing["id"]) == before
    assert store.get_work_order(reviewing["id"])["status"] == "needs_review"


def test_the_automatic_merge_costs_a_project_that_has_not_opted_in_nothing(
        started, project, fake_gh, reviewing):
    """The budget above is the SHIPPED one, and it must stay shipped.

    `validation.auto_merge` is false on every project until someone names it true, so the
    common case has to be provably free — not "one cheap read", but the same three
    `wo_events` reads and the same one work-order query as before the feature existed.
    That is what `Daemon.auto_merge` returning on the config check buys, and asserting it
    here is what stops a later refactor moving the check below the query.
    """
    red(fake_gh, GREEN)
    store = ProjectStore(project)
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    assert len([s for s in sql if "wo_events" in s]) == 3
    assert len([s for s in sql if "wo_events" not in s]) == 1
    assert not [s for s in sql if "validation_rounds" in s]


def test_an_opted_in_project_declares_what_the_automatic_merge_costs_it(
        started, project, fake_gh, reviewing):
    """And the other half: a project that HAS opted in pays, and the price is counted.

    Four more indexed reads per parked pull request per poll — the latest validation
    round TWICE, the pending assumptions, and the `automerge_held` events the dedupe keys
    on — and that is the whole standing cost of holding merge authority. A fifth
    appearing means somebody put a query on a path every open pull request of an opted-in
    project pays every two minutes; this is the test that says so rather than a sentence
    in a docstring nobody executes.

    The round is read twice on purpose: `ProjectStore.validated_head` answers the
    predicate and the raw row supplies only the wording, so the rule has one home. The
    price of that is one repeated indexed read of one row, and it is named here so the
    trade is visible rather than mistaken for a fifth query later.

    Counted on a HELD pull request (this one's round never passed) and on the SECOND
    poll, because held is the state an opted-in project spends almost all of its time in
    and the first poll of any hold writes its one event. That write not repeating is the
    other half of what is asserted here.
    """
    red(fake_gh, GREEN)
    spec = started.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True
    store = ProjectStore(project)
    poll(started, store)                     # the hold is recorded, once
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []
    assert len([s for s in sql if "validation_rounds" in s]) == 2
    assert len([s for s in sql if "assumptions" in s]) == 1
    assert len([s for s in sql if "wo_events" in s]) == 4
    assert not [s for s in sql if "approvals" in s]


def test_a_work_order_with_no_pr_url_is_never_polled(started, project, fake_gh):
    """The skip-the-whole-step optimisation, which widening the status set is the
    obvious way to lose: a fleet with no open pull requests spawns no subprocess."""
    wo = ops.create_work_order("proj_a", "no pull request here")
    store = ProjectStore(project)
    store.set_status(wo["id"], "needs_review")

    poll(started, store)

    assert fake_gh.calls == []


# -- giving up ----------------------------------------------------------------------


def test_three_attempts_then_the_red_ci_asks_the_user(started, project, fake_gh,
                                                      reviewing):
    """The same cap as the conflict repair, and the same shape of give-up."""
    red(fake_gh)
    store = ProjectStore(project)

    for _ in range(PR_REPAIR_MAX_ATTEMPTS):
        poll(started, store)
        assert delivered(store, reviewing["id"])
    assert store.get_work_order(reviewing["id"])["attention_reason"] == \
        IDLE_NO_FINISH_BLOCKER          # still trying; the review item is untouched

    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == PR_CHECKS_BLOCKER
    assert true_blockers(store, row)[0] == PR_CHECKS_BLOCKER
    assert not store.queued_messages(reviewing["id"])


def test_the_give_up_reason_survives_the_reconciler(started, project, fake_gh,
                                                    reviewing):
    """INV-ATTENTION-REASON rewrites any flag `true_blockers` cannot re-derive, so the
    blocker has to be derivable from the work order's own timeline."""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])

    assert [v.invariant for v in check_project(store)] == []
    assert store.get_work_order(reviewing["id"])["attention_reason"] == PR_CHECKS_BLOCKER


def test_a_parked_work_order_can_also_give_up(started, project, fake_gh, parked):
    """`waiting_pr_merge` is silent in the ordinary case; a red build nobody could fix
    is the second thing that makes it speak."""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, parked["id"])

    row = store.get_work_order(parked["id"])
    assert row["attention_reason"] == PR_CHECKS_BLOCKER
    assert [v.invariant for v in check_project(store)] == []


def test_green_again_closes_the_episode_and_resets_the_budget(started, project,
                                                              fake_gh, reviewing):
    """The budget is per episode, not per work order lifetime (spec §4)."""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])
    assert store.get_work_order(reviewing["id"])["attention_reason"] == PR_CHECKS_BLOCKER

    red(fake_gh, GREEN)
    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 0
    assert row["attention_reason"] != PR_CHECKS_BLOCKER

    red(fake_gh)
    poll(started, store)
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 1


def test_a_re_run_in_flight_does_not_reset_the_budget(started, project, fake_gh,
                                                      reviewing):
    """The tick right after a worker pushes its fix has every check QUEUED. Reading that
    as "nothing is failing" would close the episode, reset the count, and hand a fix
    that does not work three fresh attempts every round — a cap that caps nothing."""
    red(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    delivered(store, reviewing["id"])
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 1

    red(fake_gh, [check("unit (3.11)", "", status="QUEUED")])
    poll(started, store)
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 1

    red(fake_gh)                          # ... and it came back red
    poll(started, store)
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 2


def test_clearing_the_ci_flag_never_takes_down_a_different_one(started, project,
                                                               fake_gh, reviewing):
    """The review item outlives the repair: going green answers the build, not the
    question the user was asked."""
    red(fake_gh, GREEN)
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == IDLE_NO_FINISH_BLOCKER


def test_a_merged_work_order_stops_saying_do_not_merge_it(started, project, fake_gh,
                                                          reviewing):
    """An episode is only ever closed by a poll, and a terminal work order is not polled.
    So a user who reads the red build and merges anyway ends in `complete_merged` with
    the episode still open — and an ungated derivation would leave a finished work order
    telling them not to merge something they already merged, for ever."""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])
    assert store.get_work_order(reviewing["id"])["attention_reason"] == PR_CHECKS_BLOCKER

    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-13T10:00:00Z", checks=RED)
    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert row["status"] == "completed"
    assert true_blockers(store, row) == []
    assert not row["needs_attention"]
    assert [v.invariant for v in check_project(store)] == []


def test_a_refusal_is_not_hidden_behind_a_pending_assumption(started, project,
                                                             fake_gh):
    """Widening the poll made a co-occurrence reachable that `true_blockers` had a guard
    against precisely because it could not happen: a work order holding an undecided
    assumption sits in `needs_review`, which is polled now, so its pull request CAN be
    closed under it. The refusal must still be said — under the assumptions line, never
    instead of it."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.assume(wo["id"], "used tabs, not spaces")
    assert ops.finish(wo["id"], "opened a PR", pr_url=PR)["status"] == "needs_review"
    fake_gh.set_pr(PR, "CLOSED")
    store = ProjectStore(project)

    poll(started, store)

    blockers = true_blockers(store, store.get_work_order(wo["id"]))
    assert "assumption" in blockers[0]        # the decision still ranks first
    assert PR_CLOSED_BLOCKER in blockers      # ... and the refusal is not lost
    assert [v.invariant for v in check_project(store)] == []


def test_a_conflict_give_up_outside_the_merge_queue_is_re_derivable(
        started, project, fake_gh, reviewing):
    """The conflict blocker's twin obligation. Widening the poll means conflicts are now
    nudged in `needs_review` too, so a give-up there is a flag `true_blockers` must be
    able to re-derive — or INV-ATTENTION-REASON relabels it on the next tick and the
    user is sent to read a session whose story is a merge conflict."""
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="main", checks=GREEN)
    store = ProjectStore(project)

    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])

    row = store.get_work_order(reviewing["id"])
    assert row["status"] == "needs_review"
    assert PR_CONFLICT_BLOCKER in true_blockers(store, row)
    assert [v.invariant for v in check_project(store)] == []


def test_a_conflict_give_up_is_read_and_not_merely_derived(started, project, fake_gh,
                                                           reviewing):
    """DERIVED IS NOT READ. `attention_reason` is one column fed from `blockers[0]`
    (kn-d4d5a967), so a blocker appended below another is invisible to the user — and
    the two give-ups were derived at two different sites, the red build above the
    `needs_review` triage and the conflict below it. In the statuses issue #224 added,
    that put a conflict give-up under the panel's, where nobody would ever read it.

    The panel half is what makes this a test rather than an ordering preference: both
    lines are true at once here, and only one of them can be the reason."""
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="main", checks=GREEN)
    store = ProjectStore(project)
    rnd = store.open_validation_round(wo_id=reviewing["id"], fingerprint="abc")
    store.close_validation_round(rnd["id"], "escalated", reason="three rounds, no deal")
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])

    row = store.get_work_order(reviewing["id"])
    blockers = true_blockers(store, row)
    assert blockers[0] == PR_CONFLICT_BLOCKER
    assert VALIDATION_STUCK_BLOCKER in blockers      # ...and the panel is not dropped
    assert row["attention_reason"] == PR_CONFLICT_BLOCKER
    assert [v.invariant for v in check_project(store)] == []


def test_a_conflict_outranks_a_red_build_when_both_budgets_are_spent(
        started, project, fake_gh, reviewing):
    """`PR_REPAIR_BLOCKERS` is ORDERED, and one site deriving from it is only half the
    answer — the order it derives in is the other half. A pull request that will not
    merge at all is not waiting on its checks, so the conflict is the one to act on."""
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="main", checks=RED)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])
    # the conflict is gone, so the poll can finally see the red build underneath it
    red(fake_gh)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])

    blockers = true_blockers(store, store.get_work_order(reviewing["id"]))
    assert blockers[:2] == [PR_CONFLICT_BLOCKER, PR_CHECKS_BLOCKER]
    assert [v.invariant for v in check_project(store)] == []


def test_a_refused_pull_request_stops_saying_do_not_merge_it(started, project, fake_gh,
                                                             reviewing):
    """The closed-unmerged twin of the merged case above, and the one that had no
    answer: only the open-and-mergeable branch closed an episode, so a red pull request
    later shut without merging went on saying "do not merge it as it stands" — ABOVE
    the news that nobody is going to. A true line hiding a truer one is the shape
    kn-b6977de3 is about, and it is this work order's own bug wearing a different hat.

    `record_pr_closed` closes both episodes, which is why the ranking in `true_blockers`
    never has to decide between a refusal and a give-up: the co-occurrence cannot
    happen."""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])
    assert store.get_work_order(reviewing["id"])["attention_reason"] == PR_CHECKS_BLOCKER

    fake_gh.set_pr(PR, "CLOSED", checks=RED)
    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    blockers = true_blockers(store, row)
    assert blockers == [PR_CLOSED_BLOCKER]
    assert row["attention_reason"] == PR_CLOSED_BLOCKER
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 0
    assert [v.invariant for v in check_project(store)] == []


def test_a_reopened_pull_request_gets_a_fresh_repair_budget(started, project, fake_gh,
                                                            reviewing):
    """The other half of closing the episode on a refusal: the fix the worker never
    landed is three attempts away again, not zero. Reopening a pull request that was
    shut mid-repair is somebody saying "actually, let's have this" — and a budget still
    reading as spent would hand it straight back to the user."""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])
    fake_gh.set_pr(PR, "CLOSED", checks=RED)
    poll(started, store)

    red(fake_gh)                       # reopened, still red
    poll(started, store)               # ...notices the reopen
    poll(started, store)               # ...and starts again from one

    assert store.pr_repair_attempts(reviewing["id"], "checks") == 1
    assert delivered(store, reviewing["id"])
    assert [v.invariant for v in check_project(store)] == []


# -- the guards, and BEHIND ---------------------------------------------------------


def test_a_work_order_with_no_session_is_left_alone(started, project, fake_gh):
    """Same guard as the conflict repair: a nudge queued for a conversation that does
    not exist would never go out and would block every later one behind it."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    red(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(wo["id"])
    assert store.pr_repair_attempts(wo["id"], "checks") == 0


def test_an_open_validation_round_owns_the_worker(started, project, fake_gh, reviewing):
    """THE FOURTH GUARD (Neo question 283). Since issue 212 a round runs while the user
    decides, so a work order can sit in `needs_review` — a status this poll now selects
    — with its round still open. Both loops would then claim the same worker in the same
    moment, and `busy` cannot see it because the session is idle while the panel thinks.

    The pairing is the test: asserting only the silence would pass against an
    implementation that had simply stopped nudging. The nudge has to arrive once the
    round settles, because the pull request is still red."""
    red(fake_gh)
    store = ProjectStore(project)
    rnd = store.open_validation_round(wo_id=reviewing["id"], fingerprint="abc")
    assert store.validation_round_open(reviewing["id"])

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 0   # not spent either

    store.close_validation_round(rnd["id"], "escalated", reason="three rounds, no deal")
    poll(started, store)

    assert store.queued_messages(reviewing["id"])
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 1


def test_a_rejected_round_does_not_defer_the_repair(started, project, fake_gh,
                                                    reviewing):
    """The boundary of that guard, written down rather than left to the constant.
    `rejected` means the panel is waiting for the SUBMITTER, not deliberating — the
    worker is the one who acts next, and a red build it is about to push over is
    exactly what it needs told. That window belongs to the turn-in-flight and
    nudge-queued guards above, which is why `RUNNABLE_VALIDATION_OUTCOMES` and not the
    wider `OPEN_VALIDATION_OUTCOMES` is the set this keys off."""
    red(fake_gh)
    store = ProjectStore(project)
    rnd = store.open_validation_round(wo_id=reviewing["id"], fingerprint="abc")
    store.close_validation_round(rnd["id"], "rejected", reason="needs a test")

    poll(started, store)

    assert not store.validation_round_open(reviewing["id"])
    assert store.queued_messages(reviewing["id"])


def test_a_second_poll_does_not_nudge_twice(started, project, fake_gh, reviewing):
    red(fake_gh)
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    assert len(store.queued_messages(reviewing["id"])) == 1


def test_a_behind_branch_is_reported_and_never_rebased(started, project, fake_gh,
                                                       reviewing):
    """Spec §5: the OS reports BEHIND and lets the worker act on it. It runs no git and
    writes nothing to GitHub — `github.READ_ONLY_VERBS` is load-bearing for the panel's
    blind review, and a branch update is a history rewrite on a branch the worker may
    still be sitting on."""
    red(fake_gh, merge_state="BEHIND")
    store = ProjectStore(project)

    poll(started, store)

    msg = store.queued_messages(reviewing["id"])[0]["content"]
    assert "behind" in msg.lower() and "origin/main" in msg
    assert [c["argv"][:2] for c in fake_gh.calls] == [["pr", "view"]]


def test_a_merely_behind_pull_request_is_reported_to_nobody(started, project, fake_gh,
                                                            reviewing):
    """THE DOCUMENTED SILENCE, pinned so nobody has to take spec §5's word for it.

    A green, non-conflicting, behind pull request cannot merge under this repository's
    strict ruleset, and Jarvis says nothing at all about it: no message, no event, no
    attention. GitHub already says so on the merge page with the "Update branch" button
    beside it, `main` moves under every open pull request in the fleet, and an attention
    item per movement is how the attention strip stops being read.
    """
    red(fake_gh, GREEN, merge_state="BEHIND")
    store = ProjectStore(project)
    before = store.list_events(reviewing["id"])

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])
    assert store.list_events(reviewing["id"]) == before
    assert store.get_work_order(reviewing["id"])["attention_reason"] == \
        IDLE_NO_FINISH_BLOCKER          # what it already said, and nothing added


def test_the_conflict_nudge_carries_no_behind_note(started, project, fake_gh,
                                                   reviewing):
    """The other half of §5's scope: only the checks nudge carries it. A conflicting
    branch is about to have its base merged in anyway, which cures BEHIND too, so
    repeating it there would be an instruction the worker is already following."""
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="main", checks=GREEN,
                   merge_state="BEHIND")
    store = ProjectStore(project)

    poll(started, store)

    msg = store.queued_messages(reviewing["id"])[0]
    assert msg["source"] == "pr-conflict"
    assert "behind" not in msg["content"].lower()


# -- one reader, two field sets -----------------------------------------------------


def test_the_poll_and_the_panel_read_checks_through_the_same_code(fake_gh):
    """Spec §2: the FIELD SETS stay separate — the poll must not pay for the body, the
    file list and the diff every tick — but there is one reader of a check run, so the
    poll and the panel cannot disagree about what green means."""
    from jarvis import github

    fake_gh.set_pr_artifact(PR, checks=RED)
    fake_gh.set_pr(PR, "OPEN", checks=RED)

    assert github.pr_view(PR).checks == github.pr_artifact(PR).checks
    assert "body" not in github.PR_FIELDS and "files" not in github.PR_FIELDS


def test_a_legacy_commit_status_is_read_too(started, project, fake_gh, reviewing):
    """A repository can carry both shapes at once; `pr_artifact` already read the legacy
    one, and reading it in only one of the two places is how the OS ends up judging a
    pull request by a standard it does not police while waiting."""
    red(fake_gh, [{"__typename": "StatusContext", "context": "ci/legacy",
                   "state": "FAILURE"}])
    store = ProjectStore(project)

    poll(started, store)

    msg = store.queued_messages(reviewing["id"])[0]["content"]
    assert "ci/legacy" in msg


def test_a_pending_legacy_status_is_not_red(started, project, fake_gh, reviewing):
    red(fake_gh, [{"__typename": "StatusContext", "context": "ci/legacy",
                   "state": "PENDING"}])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])
