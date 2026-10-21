"""A work order must never sit on a red pull request — issue #224"""

from __future__ import annotations

import json

import pytest

from jarvis import db, github, ops
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
#: A second parked pull request, for the per-step half of the read budget
OTHER_PR = "https://github.com/acme/proj/pull/8"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def check(name: str, conclusion: str, status: str = "COMPLETED") -> dict:
    """One `statusCheckRollup` entry in GitHub's CheckRun shape"""
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


#: The live case, from run 34741340610 on wo-a6af01f0's branch: one job failed and fail-fast CANCELLED...
RED = [check("unit (3.11)", "FAILURE"), check("unit (3.12)", "CANCELLED"),
       check("unit (3.13)", "CANCELLED"), check("evals", "SUCCESS"),
       check("browser", "SUCCESS")]
GREEN = [check("unit (3.11)", "SUCCESS"), check("evals", "SUCCESS")]


def poll(daemon, store):
    """The poll step alone, without the rest of the tick around it"""
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


@pytest.fixture()
def parked(started, project, fake_gh):
    """Finished behind a pull request, with the session every real one has"""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    ProjectStore(project).update_work_order(wo["id"], session_id="sess-1")
    return wo


@pytest.fixture()
def reviewing(started, project, parked):
    """`parked`, then escalated into `needs_review` — the live case's shape"""
    store = ProjectStore(project)
    store.set_status(parked["id"], "needs_review")
    store.flag_attention(parked["id"], IDLE_NO_FINISH_BLOCKER)
    return parked


def red(fake_gh, checks=None, merge_state: str | None = None) -> None:
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main",
                   checks=RED if checks is None else checks, merge_state=merge_state)


#: The head every real pull request has, and every counting test below records on the row as well
COUNTED_HEAD = "a" * 40


def head_is_known(store, wo_id: str) -> None:
    """Cache `COUNTED_HEAD` on the row: the same sha GitHub reports"""
    store.update_work_order(wo_id, pr_head_oid=COUNTED_HEAD)


def delivered(store, wo_id: str) -> list[dict]:
    """Pretend the daemon delivered whatever is queued, and hand it back"""
    msgs = store.queued_messages(wo_id)
    for m in msgs:
        store.mark_message(m["id"], "delivered")
    return msgs


# -- the regression


def test_a_needs_review_work_order_with_a_red_pr_is_nudged(started, project, fake_gh,
                                                           reviewing):
    """THE defect. wo-a6af01f0 sat here with three failing unit jobs and the OS said nothing: the poll..."""
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
    """THE TRAP"""
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
    """The other half of the trap, and Neo's ruling on question 275: `settle_work_order` parks a done turn..."""
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

    # The flag follows the status rather than being carried through the repair
    started.tick_count = 6               # a tick that reconciles but does not poll
    started.tick()
    row = store.get_work_order(wo["id"])
    assert row["status"] == "needs_review"
    assert row["needs_attention"]
    assert row["attention_reason"] == IDLE_NO_FINISH_BLOCKER


def test_a_parked_work_order_still_goes_back_to_the_merge_queue(
        started, project, fake_gh, fake_claude, settle_turns):
    """The negative half of the test above: restoring the ORIGIN must not change the ordinary case"""
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


# -- the negative controls


def test_a_pending_check_nudges_nobody(started, project, fake_gh, reviewing):
    """A run in flight is not a failure"""
    red(fake_gh, [check("unit (3.11)", "", status="IN_PROGRESS"),
                  check("evals", "", status="QUEUED")])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])
    assert store.pr_repair_attempts(reviewing["id"], "checks") == 0
    assert store.get_work_order(reviewing["id"])["status"] == "needs_review"


def test_a_failure_beside_a_pending_check_still_nudges(started, project, fake_gh,
                                                       reviewing):
    """...but a failure that has already landed is a fact"""
    red(fake_gh, [check("unit (3.11)", "FAILURE"),
                  check("unit (3.12)", "", status="IN_PROGRESS")])
    store = ProjectStore(project)

    poll(started, store)

    assert len(store.queued_messages(reviewing["id"])) == 1


def test_a_cancelled_check_is_not_a_failure(started, project, fake_gh, reviewing):
    """CANCELLED is what fail-fast does to the siblings of a job that failed"""
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
    """An empty rollup is a repository that runs no CI"""
    red(fake_gh, [])
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(reviewing["id"])


def test_a_green_pull_request_costs_one_call_six_reads_and_no_write(
        started, project, fake_gh, reviewing):
    """The overwhelmingly common case stays the cheap one — and the budget is COUNTED, not described"""
    red(fake_gh, GREEN)
    store = ProjectStore(project)
    head_is_known(store, reviewing["id"])
    before = store.list_events(reviewing["id"])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]]) == 1
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["run", "list"]]
    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []
    # 7 -> 8 DELIBERATELY (wo-7ec57748
    assert len([s for s in sql if "wo_events" in s]) == 8
    # ...and the work-order query is the step's one
    assert len([s for s in sql if "wo_events" not in s]) == 1
    assert not store.queued_messages(reviewing["id"])
    assert store.list_events(reviewing["id"]) == before
    assert store.get_work_order(reviewing["id"])["status"] == "needs_review"


def test_two_parked_pull_requests_pay_the_per_request_price_twice(
        started, project, fake_gh, reviewing):
    """What "per pull request" and "per step" mean, counted on a step that has two"""
    second = ops.create_work_order("proj_a", "add feature Y")
    ops.finish(second["id"], "opened a second PR", pr_url=OTHER_PR)
    red(fake_gh, GREEN)
    fake_gh.set_pr(OTHER_PR, "OPEN", mergeable="MERGEABLE", base_ref="main",
                   checks=GREEN)
    store = ProjectStore(project)
    head_is_known(store, reviewing["id"])
    head_is_known(store, second["id"])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]]) == 2
    # `6 * n + 1` -> `7 * n + 1`: the resync-window read above is per pull request
    assert len([s for s in sql if "wo_events" in s]) == 7 * 2 + 1
    assert len([s for s in sql if "wo_events" not in s]) == 1


def test_the_automatic_merge_costs_a_project_that_has_not_opted_in_nothing(
        started, project, fake_gh, parked):
    """The budget above is the SHIPPED one, and it must stay shipped"""
    red(fake_gh, GREEN)
    store = ProjectStore(project)
    head_is_known(store, parked["id"])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    # 7 -> 8 for the resync-window read, as in the green budget above
    assert len([s for s in sql if "wo_events" in s]) == 8
    assert len([s for s in sql if "wo_events" not in s]) == 1
    # ...and no round read, which is what proves the undeclared detector's cheap short-circuit ordering:...
    assert not [s for s in sql if "validation_rounds" in s]


def test_an_opted_in_project_declares_what_the_automatic_merge_costs_it(
        started, project, fake_gh, parked):
    """And the other half: a project that HAS opted in pays, and the price is counted"""
    red(fake_gh, GREEN)
    spec = started.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True
    store = ProjectStore(project)
    head_is_known(store, parked["id"])
    poll(started, store)                     # the hold is recorded, once
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []
    # `validation_rounds` and `assumptions` are the auto-merge's own
    assert len([s for s in sql if "validation_rounds" in s]) == 1
    assert len([s for s in sql if "assumptions" in s]) == 1
    # 8 -> 9 for the resync-window read, as in the green budget above
    assert len([s for s in sql if "wo_events" in s]) == 9
    assert not [s for s in sql if "approvals" in s]


def test_the_carry_costs_nothing_until_the_head_has_actually_moved(
        started, project, fake_gh, parked, monkeypatch, local_base):
    """SPEC 2026-09-27 §7 TEST 19: the carry's extra reads happen on a `sha_moved` tick and on no other"""
    from jarvis import branchproof

    git: list[str] = []
    real_git = branchproof._git
    monkeypatch.setattr(branchproof, "_git",
                        lambda repo, *a, **kw: (git.append(a[0]), real_git(repo, *a,
                                                                          **kw))[1])
    spec = started.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True
    store = ProjectStore(project)
    judged = "709582ae53000000000000000000000000000aaa"
    row = store.open_validation_round(wo_id=parked["id"], fingerprint="fp1")
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main", checks=GREEN,
                   merge_state="CLEAN", head_oid=judged, base_oid=judged)
    # Up to date: the checkout agrees the head carries the base (spec 2026-09-28 §3.2)
    local_base.update({"tip": judged, "contains": {judged}})
    poll(started, store)                 # the merge is proposed on this one
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)                 # ...and this is the steady state being priced

    store.conn.set_trace_callback(None)
    # The verdict already covers the head, so nothing is carried and nothing is refused
    assert store.events_of_kind(parked["id"], ops.HEAD_CARRIED_EVENT) == []
    assert store.events_of_kind(parked["id"], ops.CARRY_REFUSED_EVENT) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["api", "--method"]]
    # No local proof either: the diff the branch adds is computed for a MOVED head only
    assert "diff" not in git
    assert len([s for s in sql if "validation_rounds" in s]) == 1
    assert len([s for s in sql if "assumptions" in s]) == 1


def test_a_sha_moved_tick_is_the_one_that_pays_for_the_walk(
        started, project, fake_gh, parked, monkeypatch):
    """The other side of test 19, so "only on a `sha_moved` tick" is an assertion about both ticks"""
    from jarvis import branchproof

    monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: True)
    monkeypatch.setattr(branchproof, "diff_fingerprint", lambda repo, base_ref, sha: "beef")
    monkeypatch.setattr(branchproof, "is_ancestor",
                        lambda repo, ancestor, descendant: True)
    spec = started.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True
    store = ProjectStore(project)
    judged = "709582ae53000000000000000000000000000aaa"
    head = "c2120424ba000000000000000000000000000bbb"
    base = "3c4d5e6f7a00000000000000000000000000f003"
    row = store.open_validation_round(wo_id=parked["id"], fingerprint="fp1")
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main", checks=GREEN,
                   merge_state="CLEAN", head_oid=head, base_oid=base)
    fake_gh.set_parents(head, [judged, base])

    poll(started, store)

    assert len(store.events_of_kind(parked["id"], ops.HEAD_CARRIED_EVENT)) == 1
    # ONE commit walked, one API read for it — the walk is the length of the chain
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["api", "--method"]
                and "/commits/" in c["argv"][3]]) == 1


def test_an_opted_in_project_pays_nothing_for_an_order_awaiting_a_person(
        started, project, fake_gh, reviewing):
    """The status guard is a cost guard too, and on the commonest non-parked status"""
    red(fake_gh, GREEN)
    spec = started.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True
    store = ProjectStore(project)
    head_is_known(store, reviewing["id"])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    poll(started, store)

    store.conn.set_trace_callback(None)
    # 7 -> 8 for the resync-window read, as in the green budget above
    assert len([s for s in sql if "wo_events" in s]) == 8
    assert not [s for s in sql if "validation_rounds" in s or "assumptions" in s]


def test_a_work_order_with_no_pr_url_is_never_polled(started, project, fake_gh):
    """The skip-the-whole-step optimisation"""
    wo = ops.create_work_order("proj_a", "no pull request here")
    store = ProjectStore(project)
    store.set_status(wo["id"], "needs_review")

    poll(started, store)

    assert fake_gh.calls == []


def test_a_gate_recorded_pull_request_is_never_polled_and_never_settles_the_order(
        started, project, fake_gh):
    """A merge a GATE recorded routes nothing — 2026-09-25 spec §4"""
    from jarvis import gates

    wo = ops.create_work_order("proj_a", "plan the feature")
    store = ProjectStore(project)
    approval = store.add_approval(wo["id"], gates.PR_MERGE, f"gh pr merge {PR} --squash")
    gates.apply_decision(store, approval["id"], verdict="approved",
                         reason="the plan's own docs PR", decided_by="neo")
    assert store.get_work_order(wo["id"])["pr_url"] == PR
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-25T10:00:00Z", head_oid="abc1234")

    for status in ("waiting_input", "failed"):
        store.set_status(wo["id"], status)

        poll(started, store)

        assert [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]] == []
        row = store.get_work_order(wo["id"])
        assert row["status"] == status
        assert row["pr_state"] is None
        assert store.events_of_kind(wo["id"], "pr_merged") == []


def test_a_declaration_after_a_gate_recorded_the_same_url_is_still_polled(
        started, project, fake_gh):
    """The pairing: the exclusion is keyed on a gate-ONLY column, not on the EVENT"""
    from jarvis import gates

    wo = ops.create_work_order("proj_a", "merge it then declare it")
    store = ProjectStore(project)
    approval = store.add_approval(wo["id"], gates.PR_MERGE, f"gh pr merge {PR} --squash")
    gates.apply_decision(store, approval["id"], verdict="approved", reason="green",
                         decided_by="neo")
    assert store.events_of_kind(wo["id"], "pr_url_recorded")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-25T10:00:00Z", head_oid="abc1234")

    poll(started, store)

    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]]) == 1
    assert store.get_work_order(wo["id"])["status"] == "completed"


# -- giving up


def test_three_attempts_then_the_red_ci_asks_the_user(started, project, fake_gh,
                                                      reviewing):
    """The same cap as the conflict repair, and the same shape of give-up"""
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
    """INV-ATTENTION-REASON rewrites any flag `true_blockers` cannot re-derive"""
    red(fake_gh)
    store = ProjectStore(project)
    for _ in range(PR_REPAIR_MAX_ATTEMPTS + 1):
        poll(started, store)
        delivered(store, reviewing["id"])

    assert [v.invariant for v in check_project(store)] == []
    assert store.get_work_order(reviewing["id"])["attention_reason"] == PR_CHECKS_BLOCKER


def test_a_parked_work_order_can_also_give_up(started, project, fake_gh, parked):
    """`waiting_pr_merge` is silent in the ordinary case"""
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
    """The budget is per episode, not per work order lifetime (spec §4)"""
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
    """The tick right after a worker pushes its fix has every check QUEUED"""
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
    """The review item outlives the repair: going green answers the build"""
    red(fake_gh, GREEN)
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert row["needs_attention"]
    assert row["attention_reason"] == IDLE_NO_FINISH_BLOCKER


def test_a_merged_work_order_stops_saying_do_not_merge_it(started, project, fake_gh,
                                                          reviewing):
    """An episode is only ever closed by a poll, and a terminal work order is not polled"""
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
    """Widening the poll made a co-occurrence reachable that `true_blockers` had a guard against precisely..."""
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
    """The conflict blocker's twin obligation"""
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
    """DERIVED IS NOT READ (#224)"""
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
    """`PR_REPAIR_BLOCKERS` is ORDERED, and one site deriving from it is only half the answer"""
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
    """The closed-unmerged twin of the merged case above"""
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
    """The other half of closing the episode on a refusal: the fix the worker never landed is three..."""
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


# -- the guards, and BEHIND


def test_a_work_order_with_no_session_is_left_alone(started, project, fake_gh):
    """Same guard as the conflict repair: a nudge queued for a conversation that does not exist would..."""
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    red(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    assert not store.queued_messages(wo["id"])
    assert store.pr_repair_attempts(wo["id"], "checks") == 0


def test_an_open_validation_round_owns_the_worker(started, project, fake_gh, reviewing):
    """THE FOURTH GUARD (Neo question 283)"""
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
    """The boundary of that guard, written down rather than left to the constant"""
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
    """Spec §5: the OS reports BEHIND and lets the worker act on it"""
    red(fake_gh, merge_state="BEHIND")
    store = ProjectStore(project)

    poll(started, store)

    msg = store.queued_messages(reviewing["id"])[0]["content"]
    assert "behind" in msg.lower() and "origin/main" in msg
    assert [c["argv"][:2] for c in fake_gh.calls] == [["pr", "view"]]


def test_a_merely_behind_pull_request_is_reported_to_nobody(started, project, fake_gh,
                                                            reviewing):
    """THE DOCUMENTED SILENCE, pinned so nobody has to take spec §5's word for it"""
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
    """The other half of §5's scope: only the checks nudge carries it"""
    fake_gh.set_pr(PR, "OPEN", mergeable="CONFLICTING", base_ref="main", checks=GREEN,
                   merge_state="BEHIND")
    store = ProjectStore(project)

    poll(started, store)

    msg = store.queued_messages(reviewing["id"])[0]
    assert msg["source"] == "pr-conflict"
    assert "behind" not in msg["content"].lower()


# -- one reader, two field sets


def test_the_poll_and_the_panel_read_checks_through_the_same_code(fake_gh):
    """Spec §2: the FIELD SETS stay separate"""
    from jarvis import github

    fake_gh.set_pr_artifact(PR, checks=RED)
    fake_gh.set_pr(PR, "OPEN", checks=RED)

    assert github.pr_view(PR).checks == github.pr_artifact(PR).checks
    assert "body" not in github.PR_FIELDS and "files" not in github.PR_FIELDS


def test_a_legacy_commit_status_is_read_too(started, project, fake_gh, reviewing):
    """A repository can carry both shapes at once"""
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


# -- §2b: the poll records the head it already read

HEAD = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
MOVED = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def test_the_poll_records_the_head_it_already_read(started, project, fake_gh, reviewing):
    """Spec §2b"""
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=HEAD)
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(reviewing["id"])
    assert row["pr_head_oid"] == HEAD
    assert float(row["pr_head_seen_at"]) > 0

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=MOVED)
    poll(started, store)

    assert store.get_work_order(reviewing["id"])["pr_head_oid"] == MOVED


def test_an_unchanged_head_is_not_rewritten_every_tick(started, project, fake_gh,
                                                       reviewing):
    """The budget of the poll's common case is NO WRITE"""
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=HEAD)
    store = ProjectStore(project)
    poll(started, store)
    seen = float(store.get_work_order(reviewing["id"])["pr_head_seen_at"])

    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)
    poll(started, store)
    store.conn.set_trace_callback(None)

    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []
    assert float(store.get_work_order(reviewing["id"])["pr_head_seen_at"]) == seen


# -- GitHub stopped recomputing this pull request (wo-d3a70c29, PR #995)

#: #995's branch tip, which the pull request's `head.sha` never caught up to
TIP = "cccccccccccccccccccccccccccccccccccccccc"
BRANCH = "worktree-wo-cdee6f9b"


def unknown(fake_gh, head_oid: str = HEAD, pr_url: str = PR) -> None:
    """The one answer the poll could do nothing with: OPEN, UNKNOWN, no checks state"""
    fake_gh.set_pr(pr_url, "OPEN", mergeable="UNKNOWN", base_ref="main", checks=GREEN,
                   head_oid=head_oid, head_ref=BRANCH)


@pytest.fixture()
def tips(monkeypatch):
    """`tips(mapping)` registers what `git ls-remote --heads origin` answers"""
    from jarvis import github

    calls: list = []

    def install(mapping: dict[str, str] | None = None, fail: bool = False):
        def fake(cwd=None, remote="origin"):
            calls.append(cwd)
            if fail:
                raise github.GitHubError("`git ls-remote` failed", "boom")
            return dict(mapping or {})

        monkeypatch.setattr(github, "ls_remote", fake)
        return calls

    return install


def test_the_remote_tip_is_not_read_while_github_is_still_answering(
        started, project, fake_gh, parked, tips):
    """The read is paid by an UNKNOWN pull request and by nothing else: the green common case must not..."""
    calls = tips({BRANCH: TIP})
    red(fake_gh, GREEN)
    store = ProjectStore(project)

    poll(started, store)

    assert calls == []
    row = store.get_work_order(parked["id"])
    assert (row["pr_ref_oid"], row["pr_mergeable_unknown_since"]) == (None, None)


def test_the_remote_tip_is_read_once_per_project_per_tick(started, project, fake_gh,
                                                          parked, tips):
    """`base_ci`'s shape: filled lazily, shared across the loop"""
    calls = tips({BRANCH: TIP})
    second = ops.create_work_order("proj_a", "add feature Y")
    ops.finish(second["id"], "opened a second PR", pr_url=OTHER_PR)
    unknown(fake_gh)
    unknown(fake_gh, pr_url=OTHER_PR)
    store = ProjectStore(project)

    poll(started, store)

    assert len(calls) == 1


def test_an_unknown_mergeability_is_clocked_once_and_only_once(started, project,
                                                               fake_gh, parked, tips):
    """The clock is what tells a fresh UNKNOWN from one GitHub has abandoned"""
    tips({BRANCH: HEAD})
    unknown(fake_gh)
    store = ProjectStore(project)

    poll(started, store)
    since = float(store.get_work_order(parked["id"])["pr_mergeable_unknown_since"])
    poll(started, store)

    assert since > 0
    assert float(store.get_work_order(parked["id"])["pr_mergeable_unknown_since"]) \
        == since


def test_mergeability_coming_back_clears_the_clock(started, project, fake_gh, parked,
                                                   tips):
    """And clears it only when it is set: an answer GitHub never stopped giving writes nothing at all"""
    tips({BRANCH: HEAD})
    unknown(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    assert store.get_work_order(parked["id"])["pr_mergeable_unknown_since"]

    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main", checks=GREEN,
                   head_oid=HEAD, head_ref=BRANCH)
    poll(started, store)

    assert store.get_work_order(parked["id"])["pr_mergeable_unknown_since"] is None


def test_a_tip_the_pull_request_never_caught_up_to_is_recorded_and_told_once(
        started, project, fake_gh, parked, tips):
    """#995 itself: `pr_ref_oid` carries the branch tip GitHub reports"""
    tips({BRANCH: TIP})
    unknown(fake_gh)
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert (row["pr_ref_oid"], row["pr_head_oid"]) == (TIP, HEAD)
    assert ops.scheduled_actor(store, row) is None
    assert len([e for e in store.list_events(parked["id"])
                if e["kind"] == "pr_recompute_stalled"]) == 1


def test_an_unchanged_tip_is_not_rewritten_every_tick(started, project, fake_gh,
                                                      parked, tips):
    """The head cache's discipline, applied to the second column"""
    tips({BRANCH: TIP})
    unknown(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    before = float(store.get_work_order(parked["id"])["updated_at"])

    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)
    poll(started, store)
    store.conn.set_trace_callback(None)

    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []
    assert float(store.get_work_order(parked["id"])["updated_at"]) == before


def test_a_later_healthy_push_voids_the_tip_rather_than_outliving_it(
        started, project, fake_gh, parked, tips):
    """The tip is only re-read while mergeability is UNKNOWN"""
    tips({BRANCH: TIP})
    unknown(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    assert store.get_work_order(parked["id"])["pr_ref_oid"] == TIP

    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main", checks=GREEN,
                   head_oid=MOVED, head_ref=BRANCH)
    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert (row["pr_head_oid"], row["pr_ref_oid"]) == (MOVED, "")
    assert ops.scheduled_actor(store, row) is not None


def test_a_merged_pull_request_with_a_stale_cached_tip_writes_no_stall_event(
        started, project, fake_gh, parked, tips):
    """There is nothing left for GitHub to recompute"""
    tips({BRANCH: TIP})
    store = ProjectStore(project)
    store.update_work_order(parked["id"], pr_head_oid=HEAD, pr_ref_oid=TIP,
                            pr_mergeable_unknown_since=db.now())
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-10-09T10:00:00Z", head_oid=HEAD,
                   head_ref=BRANCH)

    poll(started, store)

    assert [e for e in store.list_events(parked["id"])
            if e["kind"] == "pr_recompute_stalled"] == []
    assert store.get_work_order(parked["id"])["status"] == "completed"


def test_an_unreadable_ls_remote_leaves_the_cache_standing(started, project, fake_gh,
                                                           parked, tips):
    """github.py's error discipline: stale rather than wrong"""
    tips(fail=True)
    unknown(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    row = store.get_work_order(parked["id"])
    assert row["pr_ref_oid"] in (None, "")
    assert row["pr_mergeable_unknown_since"]          # the poll still did its work


# -- and the OS closes and reopens it ONCE to force the resync (wo-7ec57748)

#: How long #995 sat with mergeability unanswered
STALLED_FOR = 7 * 3600

#: Where the branch is after a LATER push, for the episode that re-arms
RETIP = "d" * 40


def resync_calls(fake_gh) -> list[list[str]]:
    """Every `gh pr close` / `gh pr reopen` the fake was asked for, in order"""
    return [c["argv"][:3] for c in fake_gh.calls
            if c["argv"][:2] in (["pr", "close"], ["pr", "reopen"])]


def kinds_of(store, wo_id: str, kind: str) -> list[dict]:
    return [e for e in store.list_events(wo_id) if e["kind"] == kind]


@pytest.fixture()
def stalled(started, project, fake_gh, parked, tips):
    """#995's recorded state: OPEN, head at 0488753"""
    tips({BRANCH: TIP})
    unknown(fake_gh)
    store = ProjectStore(project)
    store.update_work_order(parked["id"],
                            pr_mergeable_unknown_since=db.now() - STALLED_FOR)
    return store


def test_a_stalled_pull_request_is_closed_and_reopened_exactly_once(
        started, project, fake_gh, parked, stalled):
    """THE REMEDY, and it is one attempt per episode"""
    store = stalled

    poll(started, store)
    first = resync_calls(fake_gh)
    poll(started, store)

    assert first == [["pr", "close", PR], ["pr", "reopen", PR]]
    assert resync_calls(fake_gh) == first          # a second tick asks for neither again
    assert len(kinds_of(store, parked["id"], "pr_resync_started")) == 1
    assert len(kinds_of(store, parked["id"], "pr_resync_done")) == 1
    payload = json.loads(kinds_of(store, parked["id"], "pr_resync_started")[0]["payload"])
    assert (payload["pr_url"], payload["head_oid"], payload["ref_oid"]) == (PR, HEAD, TIP)


def test_the_poll_after_the_resync_closes_the_episode_and_flags_nobody(
        started, project, fake_gh, parked, stalled):
    """GitHub recomputing again IS the clearing"""
    store = stalled
    poll(started, store)

    # what the hand fix produced on #995: the pull request catches up to the branch
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main", checks=GREEN,
                   head_oid=TIP, head_ref=BRANCH)
    poll(started, store)

    assert len(kinds_of(store, parked["id"], "pr_resync_cleared")) == 1
    cleared = json.loads(kinds_of(store, parked["id"], "pr_resync_cleared")[0]["payload"])
    assert (cleared["pr_url"], cleared["head_oid"]) == (PR, TIP)
    row = store.get_work_order(parked["id"])
    assert row["status"] == "waiting_pr_merge"
    assert not row["needs_attention"]


def test_a_reopen_that_fails_says_so_on_the_timeline(started, project, fake_gh, parked,
                                                     stalled):
    """`fail_merge`'s rationale: the likeliest real failure is credentials that read and cannot write"""
    store = stalled
    fake_gh.fail_pr_reopen("gh: HTTP 403 — resource not accessible by integration")

    poll(started, store)

    failed = kinds_of(store, parked["id"], "pr_resync_failed")
    assert len(failed) == 1
    payload = json.loads(failed[0]["payload"])
    assert payload["stage"] == "reopen"
    assert (payload["pr_url"], payload["head_oid"], payload["ref_oid"]) == (PR, HEAD, TIP)
    assert "403" in payload["error"]
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"


def test_a_resync_that_could_not_finish_is_flagged_for_the_user(
        started, project, fake_gh, parked, stalled):
    """The other half of the one above: a reopen that failed is the one outcome NOBODY else notices"""
    store = stalled
    fake_gh.fail_pr_reopen("gh: HTTP 403")

    poll(started, store)

    failed = kinds_of(store, parked["id"], "pr_resync_failed")
    assert len(failed) == 1
    assert json.loads(failed[0]["payload"])["stage"] == "reopen"
    row = store.get_work_order(parked["id"])
    assert bool(row["needs_attention"])
    assert PR in row["attention_reason"]
    assert HEAD[:7] in row["attention_reason"]
    assert row["status"] == "waiting_pr_merge"
    assert row["status"] != "needs_review"
    assert kinds_of(store, parked["id"], "pr_closed") == []


def test_a_hand_reopened_pull_request_closed_for_real_still_reaches_the_user(
        started, project, fake_gh, parked, stalled):
    """THE WINDOW ENDS WHEN THE PULL REQUEST IS OPEN AGAIN, however that happened"""
    store = stalled
    fake_gh.fail_pr_reopen("gh: HTTP 403")
    poll(started, store)                     # the OS closes it; the reopen fails

    fake_gh.set_pr_state(PR, "OPEN")         # a person repairs it by hand
    poll(started, store)
    fake_gh.set_pr_state(PR, "CLOSED")       # ...and later refuses the work for real
    poll(started, store)

    assert len(kinds_of(store, parked["id"], "pr_closed")) == 1
    assert store.pr_closure_told(parked["id"])
    assert store.get_work_order(parked["id"])["status"] == "needs_review"
    observed = [e for e in kinds_of(store, parked["id"], "pr_resync_done")
                if json.loads(e["payload"]).get("observed")]
    assert len(observed) == 1


def test_the_os_own_closure_is_never_read_as_refused_work(started, project, fake_gh,
                                                          parked, stalled):
    """THE HAZARD `resync_pull_request` writes its event before the close for"""
    store = stalled
    fake_gh.fail_pr_reopen("gh: HTTP 403")
    poll(started, store)                     # closes it, and cannot get it open again

    poll(started, store)                     # the next tick observes CLOSED, unmerged

    assert kinds_of(store, parked["id"], "pr_closed") == []
    assert not store.pr_closure_told(parked["id"])
    assert store.get_work_order(parked["id"])["status"] == "waiting_pr_merge"


def test_the_suppression_releases_the_moment_the_reopen_succeeds(
        started, project, fake_gh, parked, stalled):
    """THE SUPPRESSION IS NOT A MUTE BUTTON"""
    store = stalled
    poll(started, store)
    assert len(kinds_of(store, parked["id"], "pr_resync_done")) == 1

    fake_gh.set_pr_state(PR, "CLOSED")       # a person shuts it, after the OS's episode
    poll(started, store)

    assert len(kinds_of(store, parked["id"], "pr_closed")) == 1
    assert store.pr_closure_told(parked["id"])
    assert store.get_work_order(parked["id"])["status"] == "needs_review"


def test_a_close_that_failed_suppresses_nothing(started, project, fake_gh, parked,
                                                stalled):
    """THE GUARD IS ON THE OS'S OWN WINDOW, NOT ON THE WORD "started\""""
    store = stalled
    fake_gh.fail_pr_close("gh: HTTP 403 — resource not accessible by integration")

    poll(started, store)

    failed = kinds_of(store, parked["id"], "pr_resync_failed")
    assert len(failed) == 1
    assert json.loads(failed[0]["payload"])["stage"] == "close"
    assert resync_calls(fake_gh) == [["pr", "close", PR]]      # the reopen never ran
    assert fake_gh.pr_state(PR) == "OPEN"
    assert kinds_of(store, parked["id"], "pr_closed") == []

    fake_gh.set_pr_state(PR, "CLOSED")       # a person shuts it; the OS never did
    poll(started, store)

    assert len(kinds_of(store, parked["id"], "pr_closed")) == 1
    assert store.pr_closure_told(parked["id"])
    assert store.get_work_order(parked["id"])["status"] == "needs_review"


def test_a_stall_that_re_arms_buys_one_more_close_and_reopen(
        started, project, fake_gh, parked, stalled, tips):
    """ONE PAIR PER EPISODE, NOT PER PULL REQUEST FOR EVER"""
    store = stalled
    poll(started, store)
    fake_gh.set_pr(PR, "OPEN", mergeable="MERGEABLE", base_ref="main", checks=GREEN,
                   head_oid=TIP, head_ref=BRANCH)
    poll(started, store)                     # GitHub answers again: episode cleared
    assert len(kinds_of(store, parked["id"], "pr_resync_cleared")) == 1

    # A LATER PUSH STALLS THE SAME WAY: the head moved
    tips({BRANCH: RETIP})
    unknown(fake_gh, head_oid=MOVED)
    poll(started, store)
    store.update_work_order(parked["id"],
                            pr_mergeable_unknown_since=db.now() - STALLED_FOR)
    poll(started, store)

    assert len(kinds_of(store, parked["id"], "pr_recompute_stalled")) == 2
    assert resync_calls(fake_gh) == [["pr", "close", PR], ["pr", "reopen", PR]] * 2
    assert len(kinds_of(store, parked["id"], "pr_resync_started")) == 2

    poll(started, store)                     # ...and the second episode is spent too
    assert len(kinds_of(store, parked["id"], "pr_resync_started")) == 2


def test_a_pull_request_a_human_closed_still_reaches_the_user(started, project, fake_gh,
                                                              parked, tips):
    """The negative control beside it"""
    tips({BRANCH: TIP})
    fake_gh.set_pr(PR, "CLOSED", head_oid=HEAD, head_ref=BRANCH)
    store = ProjectStore(project)

    poll(started, store)

    assert len(kinds_of(store, parked["id"], "pr_closed")) == 1
    assert store.get_work_order(parked["id"])["status"] == "needs_review"


def test_a_validating_work_order_is_never_resynced(started, project, fake_gh, parked,
                                                   stalled):
    """`PR_REPAIR_STATUSES` bounds this like every other repair: an open validation round owns the session..."""
    store = stalled
    store.set_status(parked["id"], "validating")

    poll(started, store)

    assert resync_calls(fake_gh) == []
    assert kinds_of(store, parked["id"], "pr_resync_started") == []


def test_the_resync_module_builds_only_the_two_commands_it_declares():
    """`tests/test_base_heal.py`'s AST test"""
    import ast
    import inspect

    from jarvis import pr_resync

    tree = ast.parse(inspect.getsource(pr_resync))
    verbs = {tuple(e.value for e in node.elts[:2])
             for node in ast.walk(tree)
             if isinstance(node, ast.List) and len(node.elts) >= 2
             and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                     for e in node.elts[:2])}
    assert verbs <= set(pr_resync.VERBS), \
        f"undeclared `gh` command: {sorted(verbs - set(pr_resync.VERBS))}"
    assert set(pr_resync.WRITE_VERBS) == {("pr", "close"), ("pr", "reopen")}
    assert ("pr", "close") not in set(github.READ_ONLY_VERBS)
    # NO FLAG REACHES EITHER ARGUMENT LIST
    flags = {e.value for node in ast.walk(tree) if isinstance(node, ast.List)
             for e in node.elts
             if isinstance(e, ast.Constant) and isinstance(e.value, str)
             and e.value.startswith("--")}
    assert flags == set()
