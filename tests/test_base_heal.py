"""A pull request red only because its base was red heals itself.

docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md.

THE TWO PRODUCTION SHAPES, measured on the fleet 2026-09-18, are named in the docstrings
below and drive the tests rather than being quoted beside them:

* **Shape 1** — wo-43c4c665, PR #280, check `unit (3.12)`, run 35302313324, started
  03:12:17Z, built at merge commit 07a05eb.
* **Shape 2** — wo-b2f88616, PR #298, check `unit (3.11)`, run 35315816246, started
  06:39:22Z. Note the SHARD DIFFERS from shape 1's: fail-fast cancels siblings, which is
  why the recogniser pairs on the WORKFLOW and not on the job name (Neo question 435).

Both were byte-identical to `main`'s own failure at the time: run 35298519923 at 9dd7bcc,
started 02:13:51Z. `main` recovered at run 35361364013, commit e080156, 15:16:03Z, and
neither pull request healed — GitHub never rebuilds a pull request's merge ref when its
base moves. Each burned three worker turns, about USD 5.22, on three identical answers.

**THE TWO WRONG IMPLEMENTATIONS THIS FILE EXISTS TO CATCH**, because both look like
success from the outside:

1. **Re-running the build.** A re-run replays the same merge commit, so it reproduces an
   inherited failure for ever at full CI cost; both re-runs tried on production came back
   red. The fake models the merge ref rather than the outcome — `pr update-branch` moves
   `headRefOid` and leaves the checks alone — so an implementation that re-runs never gets
   a green pull request here either.
2. **Stopping when CI goes green.** Updating the branch MOVES THE HEAD, and auto-merge
   refuses a commit no round judged, so the healed pull request is held `sha_moved` and
   the stall is the same one wearing a better label. Both production pull requests did
   exactly this and needed `jarvis validation force` by hand. So the acceptance here is
   that the pull request MERGES, not that it goes green.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jarvis import automerge, ci, db, gates, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import BASE_RED_NOTE, status_label
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

PR = "https://github.com/acme/proj/pull/7"
PR2 = "https://github.com/acme/proj/pull/8"

#: Shape 1's commits, at their production lengths — the judged one and the one the
#: branch update produced. `jarvis wo show` reported exactly this pair on wo-43c4c665.
JUDGED = "709582ae53000000000000000000000000000aaa"
UPDATED = "c2120424ba000000000000000000000000000bbb"
#: Shape 2's, on the second pull request of the fleet-wide test.
JUDGED2 = "a650fd2c98000000000000000000000000000ccc"
UPDATED2 = "85dbac55fb000000000000000000000000000ddd"


def check(name: str, conclusion: str, started: str = "2026-09-18T03:12:17Z",
          workflow: str = "ci", status: str = "COMPLETED") -> dict:
    """One `statusCheckRollup` entry, with the two fields the recogniser reads.

    `startedAt` and `workflowName` cost nothing — `gh` answers `statusCheckRollup` whole
    — so they are in the payload both readers already parse.
    """
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "startedAt": started, "workflowName": workflow}


def run(run_id: int, sha: str, conclusion: str, started: str,
        workflow: str = "ci", status: str = "completed") -> dict:
    """One `gh run list` row. LOWER CASE conclusions, as the real CLI answers them."""
    return {"databaseId": run_id, "headSha": sha, "conclusion": conclusion,
            "status": status, "startedAt": started, "workflowName": workflow}


#: `main`'s own failure, and its recovery. The real run ids and the real instants.
MAIN_RED = run(35298519923, "9dd7bcc", "failure", "2026-09-18T02:13:51Z")
MAIN_GREEN = run(35361364013, "e080156", "success", "2026-09-18T15:16:03Z")
#: A green `main` from BEFORE the pull request's check ran — the control.
MAIN_WAS_GREEN = run(35290000000, "1111111", "success", "2026-09-18T01:00:00Z")

#: Shape 1: the branch is red on one shard, its siblings cancelled by fail-fast.
RED_312 = [check("unit (3.12)", "FAILURE"), check("unit (3.11)", "CANCELLED"),
           check("evals", "SUCCESS")]
#: Shape 2: the SAME inherited failure surfacing on a different shard.
RED_311 = [check("unit (3.11)", "FAILURE", started="2026-09-18T06:39:22Z"),
           check("evals", "SUCCESS", started="2026-09-18T06:39:22Z")]
GREEN = [check("unit (3.12)", "SUCCESS", started="2026-09-18T16:00:00Z"),
         check("evals", "SUCCESS", started="2026-09-18T16:00:00Z")]


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def parked(project_path, *, pr_url: str = PR, judged: str = JUDGED,
           title: str = "add feature X"):
    """A work order finished behind a pull request, with a passed round on `judged`.

    Everything the heal and the merge both need, so each test moves exactly one fact.
    """
    store = ProjectStore(project_path)
    wo = ops.create_work_order("proj_a", title)
    ops.finish(wo["id"], "opened a PR", pr_url=pr_url)
    store.update_work_order(wo["id"], session_id="sess-" + wo["id"])
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-" + wo["id"])
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    return store, store.get_work_order(wo["id"])


def poll(daemon, store):
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def opt_in(daemon):
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True


# -- recognising it -------------------------------------------------------------------


def test_a_check_that_failed_on_a_red_base_is_rebuilt_and_the_worker_is_not_nudged(
        started, project, fake_gh):
    """SHAPE 1 — wo-43c4c665, PR #280, `unit (3.12)`, run 35302313324 started 03:12:17Z
    against a `main` whose head was 9dd7bcc (run 35298519923, red, 02:13:51Z), with
    `main` green again at e080156 (run 35361364013, 15:16:03Z).

    The whole point in one assertion pair: the branch is rebuilt, and the worker — who
    answered this correctly three times and could do nothing — is never asked.
    """
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)

    poll(started, store)

    assert fake_gh.updates == [PR]
    assert store.queued_messages(wo["id"]) == []
    assert store.pr_repair_attempts(wo["id"], "checks") == 0


def test_the_os_updates_the_branch_and_never_re_runs_the_build(started, project,
                                                               fake_gh):
    """THE PLAUSIBLE WRONG IMPLEMENTATION, and it fails silently in production: a re-run
    replays the same merge commit (run 35302313324 is fixed at 07a05eb, the merge with a
    red `main`), so it comes back red having spent a full build. Both re-runs the user
    tried on 2026-09-18 did exactly that."""
    store, _ = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])

    poll(started, store)

    assert fake_gh.reruns == []
    assert fake_gh.updates == [PR]


def test_a_branch_that_broke_itself_is_still_the_workers_problem(started, project,
                                                                 fake_gh):
    """THE CONTROL, and the one that must not regress: `main` was green when this check
    ran and is green now, so the failure is the branch's own. Unchanged behaviour — the
    worker is nudged exactly as it is today, and nothing is pushed anywhere."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_WAS_GREEN])

    poll(started, store)

    assert fake_gh.updates == []
    msgs = store.queued_messages(wo["id"])
    assert len(msgs) == 1 and msgs[0]["source"] == "pr-checks"
    assert "unit (3.12)" in msgs[0]["content"]
    assert store.pr_repair_attempts(wo["id"], "checks") == 1


def test_a_legacy_commit_status_is_never_judged_inherited(started, project, fake_gh):
    """A commit status carries no workflow and no start time, so the recogniser cannot
    place it against the base's history. It falls through to the worker — today's
    behaviour, and the safe direction for anything this heal cannot reason about."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", head_oid=JUDGED,
                   checks=[{"__typename": "StatusContext", "context": "buildkite",
                            "state": "FAILURE"}])
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])

    poll(started, store)

    assert fake_gh.updates == []
    assert store.pr_repair_attempts(wo["id"], "checks") == 1


# -- not spending a worker turn on the unfixable case ---------------------------------


def test_while_the_base_is_red_no_attempt_is_spent_and_the_status_line_says_why(
        started, project, fake_gh):
    """THE WASTE THIS ORDER WAS FILED OVER. While `main` is red nothing the worker pushes
    can pass, so each nudge is a whole conversation re-sent for an answer that cannot
    change. No attempt is spent, nothing is pushed — and the work order SAYS what it is
    waiting for, because a parked order that is silent reads as one the OS forgot."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_RED])

    poll(started, store)
    poll(started, store)
    poll(started, store)

    assert store.pr_repair_attempts(wo["id"], "checks") == 0
    assert store.queued_messages(wo["id"]) == []
    assert fake_gh.updates == []
    row = store.get_work_order(wo["id"])
    assert status_label(store, row) == (
        "waiting_pr_merge — " + BASE_RED_NOTE.format(base="main"))
    # Said ONCE however long the base stays broken, and it asks nobody for anything:
    # the OS is waiting for a build that is already running, not for a decision.
    assert len(store.events_of_kind(wo["id"], "pr_base_health")) == 1
    assert not row["needs_attention"]


def test_the_waiting_note_comes_down_when_the_pull_request_goes_green(
        started, project, fake_gh):
    """The other end of the note, and the reason the poll pays a fourth indexed read: a
    pull request whose checks predate the breakage can go green while `main` is still
    red, and a status line left saying "waiting for main" would send the user looking
    for something that no longer blocks them."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_RED])
    poll(started, store)
    assert BASE_RED_NOTE.format(base="main") in status_label(
        store, store.get_work_order(wo["id"]))

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)

    assert status_label(store, store.get_work_order(wo["id"])) == "waiting_pr_merge"


# -- bounding it ----------------------------------------------------------------------


def test_a_rebuilt_branch_that_is_still_red_falls_through_to_the_worker_once(
        started, project, fake_gh):
    """SHAPE 2 — wo-b2f88616, PR #298, `unit (3.11)`, run 35315816246 started 06:39:22Z.

    Note the shard: `main`'s red run failed a DIFFERENT job of the same matrix, so a
    recogniser that paired on the job name would refuse to heal this one. It pairs on
    the workflow instead (Neo question 435).

    And the bound: once the merge ref has been rebuilt on a green base and CI is STILL
    red, the failure is the branch's own. The worker is nudged and the branch is never
    updated a second time for that base commit — an update would only reproduce the same
    commit, which is the re-run trap one level along.
    """
    store, wo = parked(project, judged=JUDGED2)
    fake_gh.set_pr(PR, "OPEN", checks=RED_311, head_oid=JUDGED2)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED2)

    poll(started, store)
    assert fake_gh.updates == [PR]
    assert store.pr_repair_attempts(wo["id"], "checks") == 0

    # Rebuilt, and red again on the new head: the branch really is broken.
    fake_gh.set_pr(PR, "OPEN", checks=RED_311, head_oid=UPDATED2)
    poll(started, store)

    assert fake_gh.updates == [PR]
    msgs = store.queued_messages(wo["id"])
    assert len(msgs) == 1 and msgs[0]["source"] == "pr-checks"
    assert store.pr_repair_attempts(wo["id"], "checks") == 1


def test_an_update_github_refuses_spends_the_attempt_and_reaches_the_worker(
        started, project, fake_gh):
    """A refusal counts the same as a success, or a pull request GitHub will not update
    is retried every two minutes for ever and never reaches the worker who could fix it.
    The recorded reason is this OS's own phrase, never `gh`'s stderr."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.refuse_update("merge conflict between base and head")

    poll(started, store)
    poll(started, store)

    assert len(fake_gh.updates) == 1
    failed = store.events_of_kind(wo["id"], "pr_base_update_failed")
    assert len(failed) == 1
    payload = db.from_json(failed[0]["payload"], {})
    assert payload["base_sha"] == "e080156"
    assert "merge conflict" not in payload["reason"]     # our vocabulary, not gh's
    # Nudged on the first tick, when the refusal fell through. The second tick neither
    # re-updates (the attempt is spent) nor re-nudges — `heal_pull_request`'s existing
    # guard sees the message still queued, which is the behaviour that was already right.
    assert store.pr_repair_attempts(wo["id"], "checks") == 1
    assert len(store.queued_messages(wo["id"])) == 1


def test_a_conflicting_pull_request_is_the_conflict_paths_and_never_this_ones(
        started, project, fake_gh):
    """Where a conflicting update goes, and it is answered STRUCTURALLY rather than by
    policy: `elif pr.conflicting` precedes `elif pr.failing`, so a pull request GitHub
    says will not merge never reaches this heal at all. The existing conflict episode
    owns it, which is the right owner — a worker resolving the conflict merges its base
    in anyway, and that cures the inherited failure too."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, mergeable="CONFLICTING",
                   merge_state="DIRTY", head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])

    poll(started, store)

    assert fake_gh.updates == []
    assert store.pr_repair_attempts(wo["id"], "conflict") == 1
    assert store.pr_repair_attempts(wo["id"], "checks") == 0


def test_the_heal_waits_while_the_panel_is_reading_the_branch(started, project,
                                                              fake_gh):
    """Neo question 283's hazard, one mechanism along and stronger: this MOVES THE HEAD,
    so running it under an open round is the branch moving beneath the seats
    mid-judgement. Deferred, never dropped — nothing is nudged either, so the attempt
    budget is not spent while the OS waits."""
    store, wo = parked(project)
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp2")
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])

    poll(started, store)

    assert fake_gh.updates == []
    assert store.pr_repair_attempts(wo["id"], "checks") == 0


# -- the fleet ------------------------------------------------------------------------


def test_the_base_going_green_clears_stale_red_checks_across_the_whole_fleet(
        started, project, fake_gh):
    """THE GAP ONE LEVEL UP. When `main` recovers, EVERY open pull request is carrying a
    stale red check — not just the ones a worker happened to be nudged about. A heal
    driven per pull request by something that was already looking at it would leave the
    rest sitting red until somebody noticed.

    So the base's CI is read ONCE for the project and shared across the loop: one
    `gh run list` for the whole fleet, and every parked pull request rebuilt on the same
    tick. Shapes 1 and 2 are the two pull requests here, as they were on production.
    """
    store, one = parked(project, pr_url=PR, judged=JUDGED, title="shape one")
    _, two = parked(project, pr_url=PR2, judged=JUDGED2, title="shape two")
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_pr(PR2, "OPEN", checks=RED_311, head_oid=JUDGED2)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])

    poll(started, store)

    assert sorted(fake_gh.updates) == sorted([PR, PR2])
    for wo_id in (one["id"], two["id"]):
        assert store.queued_messages(wo_id) == []
        assert len(store.events_of_kind(wo_id, "pr_base_updated")) == 1
    # ONE base read for the whole project, however many pull requests it has.
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["run", "list"]]) == 1


def test_a_pull_request_with_no_session_is_healed_too(started, project, fake_gh):
    """`heal_pull_request` returns early with no session to resume, so before this a
    pull request whose worker was gone could never be repaired at all. Nothing here
    talks to a worker, so that guard does not apply and must not be copied."""
    store, wo = parked(project)
    store.update_work_order(wo["id"], session_id=None)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])

    poll(started, store)

    assert fake_gh.updates == [PR]


# -- the heal is done when it MERGES --------------------------------------------------


def test_the_heal_carries_the_verdict_and_the_pull_request_actually_merges(
        started, project, fake_gh):
    """END TO END, AND THIS IS THE ACCEPTANCE THAT MATTERS. A test stopping at "CI went
    green" passes on an implementation that leaves the pull request held `sha_moved`,
    which is what happened by hand on production: PR #280 judged 709582ae53, head moved
    to c2120424ba, every check green, `CLEAN`, and it did not merge.

    Three ticks, which is the state machine: update the branch, watch CI, merge.
    """
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)

    poll(started, store)                                    # 1. rebuild the merge ref
    assert fake_gh.updates == [PR]
    carried = store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)
    assert len(carried) == 1
    payload = db.from_json(carried[0]["payload"], {})
    assert payload["judged_sha"] == JUDGED
    assert payload["carried_head_sha"] == UPDATED
    # `head_sha` still says what the SEATS read. Only `carried_head_sha` moved.
    row = store.latest_validation_round(wo_id=wo["id"])
    assert row["head_sha"] == JUDGED and row["carried_head_sha"] == UPDATED
    assert ProjectStore.validated_head(row) == UPDATED

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=UPDATED)
    poll(started, store)                                    # 2. green on the new head
    approvals = store.list_approvals(wo["id"])
    assert len(approvals) == 1 and approvals[0]["kind"] == "auto_merge"
    # THE MERGE IS STILL GATED. The carry changes what this request says, never whether
    # one is filed — nothing about the heal lets the OS merge unreviewed.
    gates.apply_decision(store, approvals[0]["id"], "approved", "ok", "neo",
                         project="proj_a")

    poll(started, store)                                    # 3. it merges

    assert [c["argv"][2] for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]] \
        == [PR]
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_a_worker_push_after_the_heal_is_not_carried(started, project, fake_gh):
    """THE SCOPE OF THE WEAKENING. The carry is licensed by "this commit differs from
    the judged one by a merge of the base and nothing else". A worker that pushes
    afterwards breaks that, the head leaves `carried_head_sha`, and `decide` holds
    `sha_moved` again — correctly, because now there IS new authored content."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)
    poll(started, store)

    pushed = "f00dfeed00000000000000000000000000000eee"
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=pushed)
    poll(started, store)

    assert store.list_approvals(wo["id"]) == []
    held = store.events_of_kind(wo["id"], "automerge_held")
    # The first tick held on CI — the rebuild was still running, which is true. The
    # SECOND is the one this test is about.
    assert [db.from_json(e["payload"], {})["code"] for e in held] == [
        automerge.HELD_CHECKS_NOT_GREEN, automerge.HELD_SHA_MOVED]
    assert db.from_json(held[-1]["payload"], {})["judged_sha"] == UPDATED
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_the_head_the_carry_rests_on_is_re_read_immediately_before_the_update(
        started, project, fake_gh):
    """REVIEW ROUND 1. The `pr.head_oid` taken at the top of the poll loop is a stale
    snapshot by the time the update runs: the `busy` guard and a `gh run list` with a
    30s timeout both sit in between, and a worker turn can end and push inside that
    window. So the head is read again AFTER the guards and immediately before the
    update, and the call order is what says so."""
    store, _ = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)

    poll(started, store)

    order = [" ".join(c["argv"][:2]) for c in fake_gh.calls]
    assert order[:4] == ["pr view", "run list", "pr view", "pr update-branch"]


def test_a_push_that_beat_the_update_is_caught_by_the_commits_parents(
        started, project, fake_gh):
    """REVIEW ROUND 1, and the half that cannot be raced. `gh pr update-branch` has no
    `--match-head-commit`, so narrowing the window is not closing it: a worker can still
    push between the re-read and the update landing. The carry is therefore proved from
    what the resulting commit ACTUALLY merged — a head built on a worker's push has that
    push as its first parent, not the judged commit.

    Without this the OS would bind the panel's verdict to code the seats never read and
    record "no authored content changed" beside it, which `automerge.decide` and the
    gate request would both then repeat to Neo. Silent, and it falls open.
    """
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)
    # The worker's push landed in the window, so the update merged the base into THAT.
    raced = "0ddba11000000000000000000000000000000111"
    fake_gh.set_parents(UPDATED, [raced, "basecommit0"])

    poll(started, store)

    assert fake_gh.updates == [PR]                    # healed: that part is right
    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    row = store.latest_validation_round(wo_id=wo["id"])
    assert row["carried_head_sha"] == ""
    assert ProjectStore.validated_head(row) == JUDGED

    # ...and the pull request is HELD rather than merged, which is the direction this
    # failure has to fall.
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=UPDATED)
    poll(started, store)
    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_a_carry_is_refused_when_the_parents_cannot_be_read_at_all(started, project,
                                                                   fake_gh):
    """The proof is a network call and it can fail. An unprovable carry is not a carry:
    the pull request stays bound to the commit the seats read and waits for a person,
    which is worse than a carry and much better than one nobody can justify."""
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=JUDGED)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)
    fake_gh.set_parents(UPDATED, [])          # GitHub answered, and said nothing usable

    poll(started, store)

    assert fake_gh.updates == [PR]
    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []


@pytest.mark.parametrize("parents, carried", [
    # The shape a rebuilt merge ref has: judged first, the base second.
    ((JUDGED, "basecommit0"), True),
    # Judged as the SECOND parent. The first is the side the merge was made ONTO, so
    # this is a different history with somebody else's work at its root and
    # `git log --first-parent` would never show the reviewed branch at all.
    (("basecommit0", JUDGED), False),
    # Not a merge: one parent means the branch was rebased or rewritten, which
    # `update_branch` never does and nothing else may be carried over.
    ((JUDGED,), False),
    # An octopus merge brought in something nobody accounted for.
    ((JUDGED, "basecommit0", "somethingelse"), False),
    # Unreadable.
    ((), False),
])
def test_only_a_two_parent_merge_onto_the_judged_commit_may_be_carried(
        started, project, fake_gh, parents, carried):
    """The predicate itself, driven directly rather than through the poll — the fixture
    cannot produce most of these through `update-branch`, because GitHub never builds
    one that way, and "the fake cannot express it" is not a reason to leave the rule
    untested."""
    store, wo = parked(project)

    out = ops.carry_validated_head(store, wo, judged=JUDGED, head_after=UPDATED,
                                   base="main", base_sha="e080156", parents=parents)

    assert (out is not None) is carried
    row = store.latest_validation_round(wo_id=wo["id"])
    assert row["carried_head_sha"] == (UPDATED if carried else "")


def test_nothing_is_carried_when_the_head_had_already_moved_off_the_verdict(
        started, project, fake_gh):
    """Fact 2 of the carry: the commit the panel accepted must be the one this update
    replaced. A worker that pushed before the heal means the pull request was already
    going to be held `sha_moved`, and carrying a verdict onto a commit built from that
    push would bind the panel to work it never read."""
    store, wo = parked(project)
    ahead = "beefcafe00000000000000000000000000000fff"
    fake_gh.set_pr(PR, "OPEN", checks=RED_312, head_oid=ahead)
    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    fake_gh.updated_head(UPDATED)

    poll(started, store)

    assert fake_gh.updates == [PR]                     # healed: that part is unchanged
    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    row = store.latest_validation_round(wo_id=wo["id"])
    assert row["carried_head_sha"] == ""
    assert ProjectStore.validated_head(row) == JUDGED


def test_a_carry_can_never_manufacture_a_binding_the_seats_never_made():
    """A round that recorded no judged commit can never auto-merge, and a carry must not
    be the way round that. Both halves are asserted here rather than driven, because the
    population this protects is every round judged before `head_sha` shipped."""
    assert ProjectStore.validated_head(
        {"outcome": "passed", "head_sha": "", "carried_head_sha": UPDATED}) is None
    assert ProjectStore.validated_head(
        {"outcome": "rejected", "head_sha": JUDGED, "carried_head_sha": UPDATED}) is None
    assert ProjectStore.validated_head(
        {"outcome": "passed", "head_sha": JUDGED, "carried_head_sha": ""}) == JUDGED


# -- the recogniser, without a network ------------------------------------------------


BASE = (ci.Run(35361364013, "e080156", "success", "completed",
               ci.parse_ts("2026-09-18T15:16:03Z"), "ci"),
        ci.Run(35298519923, "9dd7bcc", "failure", "completed",
               ci.parse_ts("2026-09-18T02:13:51Z"), "ci"))


@pytest.mark.parametrize("started_at, workflow, verdict", [
    # Shape 1 and shape 2: ran while 9dd7bcc was head, and `main` is green now.
    ("2026-09-18T03:12:17Z", "ci", True),
    ("2026-09-18T06:39:22Z", "ci", True),
    # Ran BEFORE the base broke: whatever is wrong is the branch's own.
    ("2026-09-18T01:00:00Z", "ci", False),
    # Ran after the recovery: it was built against a base that works.
    ("2026-09-18T16:00:00Z", "ci", False),
    # A workflow the base does not run cannot be paired with any base verdict.
    ("2026-09-18T03:12:17Z", "release", False),
    # No workflow at all — a legacy commit status.
    ("2026-09-18T03:12:17Z", "", False),
])
def test_the_recogniser_is_temporal_and_workflow_scoped(started_at, workflow, verdict):
    """The table, both directions, without a daemon. A false positive costs one bounded
    branch update; a false negative costs exactly today's behaviour."""
    assert ci.inherited({"started_at": started_at, "workflow": workflow}, BASE) is verdict


def test_a_base_still_building_does_not_read_as_recovered():
    """`main` is red and a fix is IN PROGRESS. The heal must wait rather than rebuild
    against a base nobody has judged yet — `latest` takes the newest COMPLETED run."""
    building = (ci.Run(35399655231, "abc1234", "", "in_progress",
                       ci.parse_ts("2026-09-18T15:00:00Z"), "ci"), BASE[1])
    assert ci.base_is_red(building, ("ci",))
    assert not ci.inherited({"started_at": "2026-09-18T03:12:17Z", "workflow": "ci"},
                            building)


def test_a_cancelled_base_run_says_nothing_about_the_code():
    """A human stopping a run on `main` is not `main` being broken, and reading it as
    one would rebuild every pull request in the fleet for nothing."""
    cancelled = (ci.Run(1, "abc1234", "cancelled", "completed",
                        ci.parse_ts("2026-09-18T02:13:51Z"), "ci"),)
    assert not ci.base_is_red(cancelled, ("ci",))
    assert not ci.inherited({"started_at": "2026-09-18T03:12:17Z", "workflow": "ci"},
                            cancelled)


# -- the write surface ----------------------------------------------------------------


def test_this_module_writes_only_the_branch_update():
    """`tests/test_github_artifact.py`'s guard, for the module that is ALLOWED to write.

    `github.py` proves it runs no write verb at all, which is what keeps a judging seat
    unable to talk to the implementor it is reviewing. That property is why the write
    lives here instead, and it is worth nothing unless this module's own surface is
    pinned too: the OS may update a branch and do nothing else from this file.
    """
    tree = ast.parse(Path(ci.__file__).read_text())
    verbs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        words = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
        if isinstance(words[0], str) and isinstance(words[1], str):
            verbs.add((words[0], words[1]))
    assert verbs <= set(ci.VERBS), f"undeclared `gh` command: {sorted(verbs - set(ci.VERBS))}"
    assert set(ci.WRITE_VERBS) == {("pr", "update-branch")}
    assert ("run", "rerun") not in verbs, (
        "a re-run replays the same merge commit and cannot clear an inherited failure")


def test_a_branch_name_github_answered_is_still_checked_before_it_is_an_argument():
    """`baseRefName` comes back from GitHub about a pull request whose URL a WORKER
    wrote, and a name opening with `-` sits where `gh` would read a flag."""
    from jarvis.github import GitHubError

    for bad in ("--repo=someone/else", "-x", "main;rm -rf /", ""):
        with pytest.raises(GitHubError):
            ci.base_runs(bad)


# -- a catch-up with `main` costs no round ---------------------------------------------
# docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md, issue #806:
# the carry above, generalised from ONE merge the OS made to a CHAIN of base merges
# anyone made — and proved twice, because parentage alone would carry an evil merge.

#: The chain: JUDGED, then `main` merged in twice. Production lengths, as above.
MID = "1a2b3c4d5e00000000000000000000000000f001"
CAUGHT_UP = "2b3c4d5e6f00000000000000000000000000f002"
BASE1 = "3c4d5e6f7a00000000000000000000000000f003"
BASE2 = "4d5e6f7a8b00000000000000000000000000f004"
WORKER_PUSH = "5e6f7a8b9c00000000000000000000000000f005"
#: The divergent pull-merge shape of wo-00bd1096 / PR #779: a merge of two lineages of the
#: BRANCH itself, its second parent already contained in JUDGED (§3.2 item 6, Neo 806).
PULL_MERGE = "6f7a8b9c0d00000000000000000000000000f006"
BRANCH_SIDE = "7a8b9c0d1e00000000000000000000000000f007"


@pytest.fixture()
def local_proof(monkeypatch):
    """Proof (b) and the ancestry half of proof (a), answered without a checkout. §3.3.

    The `project` fixture is a repository with no `origin`, so a real fetch resolves
    nothing here. The git itself is proved against a real clone below, and the refusal a
    failed fetch produces is driven through the real code in
    `test_a_fetch_that_fails_refuses_the_carry_and_merges_nothing`.
    """
    from jarvis import branchproof

    # THE REAL ONES, kept reachable: the two whitespace tests below drive the daemon with
    # ids a real repository produced, so they need the git behind the fake (review round 1).
    state = {"id": "b7f0deadbeef", "ids": {}, "ancestors": None, "contained": set(),
             "git_fetch": branchproof.fetch, "git_fingerprint": branchproof.diff_fingerprint}

    def fetch(repo, *refs):
        return True

    def diff_fingerprint(repo, base_ref, sha):
        return state["ids"].get(sha, state["id"])

    def is_ancestor(repo, ancestor, descendant):
        # TWO DESCENDANTS, and telling them apart is the whole of Neo question 806's
        # widening: `origin/<base>` (a base merge) and the JUDGED commit (a pull merge of
        # the branch's own lineage). `contained` is the second set.
        if descendant.startswith("origin/"):
            return state["ancestors"] is None or ancestor in state["ancestors"]
        return ancestor in state["contained"]

    monkeypatch.setattr(branchproof, "fetch", fetch)
    monkeypatch.setattr(branchproof, "diff_fingerprint", diff_fingerprint)
    monkeypatch.setattr(branchproof, "is_ancestor", is_ancestor)
    return state


def api_reads(fake_gh) -> list[str]:
    """Every commit the walk asked GitHub about — the bound is a length of this list."""
    return [c["argv"][3].rsplit("/", 1)[-1] for c in fake_gh.calls
            if c["argv"][:2] == ["api", "--method"] and "/commits/" in c["argv"][3]]


def refusals(store, wo) -> list[str]:
    return [db.from_json(e["payload"], {}).get("proof")
            for e in store.events_of_kind(wo["id"], ops.CARRY_REFUSED_EVENT)]


def test_two_base_merges_in_a_row_carry_the_verdict_and_cost_no_round(
        started, project, fake_gh, local_proof):
    """THE WHOLE FEATURE. `main` was merged in twice — by a worker clearing a conflict, by
    the user pressing "Update branch", it does not matter — so the head is two commits
    past the one round 1 read. Today that costs a round, and on the last one it strands
    the order in front of the user (wo-00bd1096, wo-8736a5c5).

    The acceptance is that it MERGES on no new round: the carry, the same-tick re-decide
    (§3.5 step 3), the gate, the merge."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP)
    fake_gh.set_parents(CAUGHT_UP, [MID, BASE2])
    fake_gh.set_parents(MID, [JUDGED, BASE1])

    poll(started, store)

    carried = store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)
    assert len(carried) == 1
    payload = db.from_json(carried[0]["payload"], {})
    assert payload["cause"] == "base_merge_chain"
    assert payload["chain"] == [MID, CAUGHT_UP]
    assert payload["merged_base_shas"] == [BASE1, BASE2]
    assert payload["patch_id"] == local_proof["id"]
    row = store.latest_validation_round(wo_id=wo["id"])
    assert row["head_sha"] == JUDGED and row["carried_head_sha"] == CAUGHT_UP
    assert ProjectStore.validated_head(row) == CAUGHT_UP
    # NO ROUND, which is the point: one settled round before and after.
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    # ...and the merge is proposed on the SAME tick, not two minutes later.
    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    assert CAUGHT_UP in approvals[0]["command"]

    gates.apply_decision(store, approvals[0]["id"], "approved", "ok", "neo",
                         project="proj_a")
    poll(started, store)

    assert [c["argv"][2] for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]] \
        == [PR]
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_a_chain_whose_first_parent_is_a_worker_push_is_refused(started, project,
                                                               fake_gh, local_proof):
    """PROOF (a), §3.2. First parents only: a commit on the way back to the judged one
    that is not a two-parent merge is authored content, and the verdict may not cross it.
    Said once per (head, proof) however long the pull request stays parked."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP)
    fake_gh.set_parents(CAUGHT_UP, [WORKER_PUSH, BASE2])
    fake_gh.set_parents(WORKER_PUSH, [JUDGED])

    poll(started, store)
    poll(started, store)

    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert refusals(store, wo) == ["chain"]
    assert store.list_approvals(wo["id"]) == []


def test_a_merge_of_something_other_than_the_base_is_refused(started, project, fake_gh,
                                                            local_proof):
    """§3.2 item 6, and it is what makes the chain a chain of BASE merges: a two-parent
    merge whose second parent is not an ancestor of `origin/main` merged somebody else's
    branch in — authored content arriving in a shape that looks identical."""
    opt_in(started)
    store, wo = parked(project)
    local_proof["ancestors"] = {BASE1}
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP)
    fake_gh.set_parents(CAUGHT_UP, [MID, BASE2])
    fake_gh.set_parents(MID, [JUDGED, BASE1])

    poll(started, store)

    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert refusals(store, wo) == ["chain"]


def test_a_pull_merge_of_the_branchs_own_lineage_carries_the_verdict(
        started, project, fake_gh, local_proof):
    """§3.2 item 6 as Neo question 806 widened it — the shape of wo-00bd1096 / PR #779.

    A worker ran `git pull --no-rebase` before pushing, so the head is a merge of two
    lineages of the BRANCH: its second parent is already contained in the judged commit,
    so it brings in no commit that was not judged. Proof (b) is still required and still
    equal, so the verdict carries and no round is spent.
    """
    opt_in(started)
    store, wo = parked(project)
    local_proof["ancestors"] = {BASE1}
    local_proof["contained"] = {BRANCH_SIDE}
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=PULL_MERGE)
    fake_gh.set_parents(PULL_MERGE, [MID, BRANCH_SIDE])
    fake_gh.set_parents(MID, [JUDGED, BASE1])

    poll(started, store)

    carried = store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)
    assert len(carried) == 1
    payload = db.from_json(carried[0]["payload"], {})
    assert payload["chain"] == [MID, PULL_MERGE]
    # `merged_base_shas` MUST NOT LIE: the pull merge's second parent is no base commit.
    assert payload["merged_base_shas"] == [BASE1]
    assert payload["merged_branch_shas"] == [BRANCH_SIDE]
    assert payload["base_sha"] == BASE1
    assert ProjectStore.validated_head(store.latest_validation_round(wo_id=wo["id"])) \
        == PULL_MERGE
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1


def test_a_merge_bringing_in_an_unjudged_commit_is_still_refused(
        started, project, fake_gh, local_proof):
    """Neo question 806's condition 3: the same shape with one fact moved. The second
    parent is an ancestor of NEITHER `origin/main` NOR the judged commit, so it brings in
    authored content no seat read — refused as `chain`, and the round machine gets its
    turn on that head."""
    opt_in(started)
    store, wo = parked(project)
    local_proof["ancestors"] = {BASE1}
    local_proof["contained"] = set()
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=PULL_MERGE)
    fake_gh.set_parents(PULL_MERGE, [MID, BRANCH_SIDE])
    fake_gh.set_parents(MID, [JUDGED, BASE1])

    poll(started, store)

    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert refusals(store, wo) == ["chain"]
    assert store.list_approvals(wo["id"]) == []
    assert ops.rejudged_heads(store, wo["id"]) == {PULL_MERGE}


def test_the_walk_is_bounded_and_spends_no_more_api_calls_than_the_limit(
        started, project, fake_gh, local_proof):
    """THE BOUND, §3.2 item 5. Ten catch-ups on one parked pull request is not a case
    worth an unbounded walk, and the cost of the bound is the assertion: one `gh api` per
    commit, at most `ci.CHAIN_LIMIT` of them, then fall through."""
    opt_in(started)
    store, wo = parked(project)
    chain = [f"{i:02d}" + "0" * 34 + "aa11" for i in range(ci.CHAIN_LIMIT + 2)]
    for i, sha in enumerate(chain):
        fake_gh.set_parents(sha, [chain[i + 1] if i + 1 < len(chain) else JUDGED, BASE1])
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=chain[0])

    poll(started, store)

    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert refusals(store, wo) == ["chain"]
    assert len(api_reads(fake_gh)) == ci.CHAIN_LIMIT


# -- a rebind: the round the OS's own merge costs nobody -------------------------------
# spec docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.2


def _resolved(local_proof, fake_gh, head=None):
    """A merge of the base whose CONFLICT RESOLUTION changed the branch's own diff —
    proof (a) holds, proof (b) does not. wo-8736a5c5 / PR #794."""
    head = head or CAUGHT_UP
    local_proof["ids"][head] = "a-resolution-nobody-judged"
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=head)
    fake_gh.set_parents(head, [JUDGED, BASE1])


def test_a_changed_diff_walks_the_chain_only_when_a_rebind_could_open(
        started, project, fake_gh, local_proof):
    """THE READ BUDGET. A parked pull request is polled every couple of minutes and the
    walk is up to `ci.CHAIN_LIMIT` `gh api` calls; paying that on every tick of every
    pull request whose diff genuinely changed buys nothing when no rebind could open."""
    opt_in(started)
    store, wo = parked(project)
    _resolved(local_proof, fake_gh)
    store.create_turn(wo["id"], "message", "resolve the conflict")

    poll(started, store)
    poll(started, store)
    poll(started, store)

    assert api_reads(fake_gh) == []
    assert refusals(store, wo) == ["patch_id"]          # the (head, proof) dedupe
    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []


def test_a_changed_diff_on_a_proved_chain_reports_a_rebind(started, project, fake_gh,
                                                           local_proof):
    """The carry is still REFUSED — that resolution is content no seat has read — and
    the caller is told a rebind may be opened on it."""
    opt_in(started)
    store, wo = parked(project)
    _resolved(local_proof, fake_gh)

    out = started._carry_catch_up(
        started.catalog.project("proj_a"), store, store.get_work_order(wo["id"]),
        SimpleNamespace(base_ref="main"),
        automerge._held(automerge.HELD_SHA_MOVED, "the head moved", judged_sha=JUDGED,
                        head_sha=CAUGHT_UP, round_n=1))

    assert out.carried is False and out.rebind is True
    assert refusals(store, wo) == ["patch_id"]
    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []


def test_a_fetch_that_fails_refuses_the_carry_and_merges_nothing(started, project,
                                                                fake_gh):
    """§3.4, and the open question the spec logs: the `project` fixture has no `origin`,
    so proof (b) cannot be computed at all — the shape of a checkout whose origin refuses
    `refs/pull/N/head`. Refused, named `fetch` so a reader can tell it from a conflict
    resolution, and the pull request falls through untouched."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE1])

    poll(started, store)

    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert refusals(store, wo) == ["fetch"]
    assert api_reads(fake_gh) == []            # refused before a single API call
    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


# -- proof (b), against a real repository ---------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _a_branch_that_merged_main(tmp_path) -> tuple[Path, str, str, str]:
    """A real clone whose feature branch merged a moved `main`, cleanly and evilly.

    Returns (repo, judged, caught_up, evil). `caught_up` is an ordinary merge of
    `origin/main`; `evil` merges the same commit and edits the branch's own file while
    resolving — the case GitHub reports with the parentage of a clean one.
    """
    up = make_git_project(tmp_path, "upstream")
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(up), str(repo)], check=True)
    _git(repo, "checkout", "-qb", "feature")
    (repo / "a.txt").write_text("the branch's own line\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "the branch's contribution")
    judged = _git(repo, "rev-parse", "HEAD")
    (up / "b.txt").write_text("a line main added\n")
    _git(up, "add", "b.txt")
    _git(up, "commit", "-qm", "main moves")
    _git(repo, "fetch", "-q", "origin", "main")
    _git(repo, "merge", "-q", "--no-edit", "origin/main")
    caught_up = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", judged)
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
    (repo / "a.txt").write_text("the branch's own line\nresolved differently\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "a merge that resolved content")
    return repo, judged, caught_up, _git(repo, "rev-parse", "HEAD")


def test_identical_patch_ids_carry_and_an_edited_branch_file_does_not(tmp_path):
    """PROOF (b), §3.3, and the whole reason it exists. An evil merge has the parentage of
    a clean one, so proof (a) alone would carry it and bind the panel's verdict to code no
    seat read. What the branch adds ON TOP OF ITS MERGE BASE catches it."""
    from jarvis import branchproof

    repo, judged, caught_up, evil = _a_branch_that_merged_main(tmp_path)

    assert branchproof.fetch(repo, "main")
    clean = branchproof.diff_fingerprint(repo, "main", caught_up)
    assert clean and clean == branchproof.diff_fingerprint(repo, "main", judged)
    assert branchproof.diff_fingerprint(repo, "main", evil) != clean


def _a_branch_that_added_a_binary_file(tmp_path) -> tuple[Path, str, str]:
    """A real clone whose feature branch ADDED a binary file, then a merge swapped it.

    Returns (repo, judged, swapped). `bin.dat` holds a NUL byte, so `git diff` prints only
    `Binary files ... differ` for it and the `index` line's new-side blob id is the only
    fingerprint of its content. `swapped` merges `origin/main` cleanly and rewrites those
    bytes while resolving.
    """
    up = make_git_project(tmp_path, "upstream")
    repo = tmp_path / "binwork"
    subprocess.run(["git", "clone", "-q", str(up), str(repo)], check=True)
    _git(repo, "checkout", "-qb", "feature")
    (repo / "bin.dat").write_bytes(b"\x00\x01judged payload\x00\xff")
    _git(repo, "add", "bin.dat")
    _git(repo, "commit", "-qm", "the branch adds a binary file")
    judged = _git(repo, "rev-parse", "HEAD")
    (up / "b.txt").write_text("a line main added\n")
    _git(up, "add", "b.txt")
    _git(up, "commit", "-qm", "main moves")
    _git(repo, "fetch", "-q", "origin", "main")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
    (repo / "bin.dat").write_bytes(b"\x00\x01swapped payload\x00\xff")
    _git(repo, "add", "bin.dat")
    _git(repo, "commit", "-qm", "a merge that swapped the binary file's bytes")
    return repo, judged, _git(repo, "rev-parse", "HEAD")


def test_a_merge_that_swaps_a_binary_files_bytes_is_not_the_judged_diff(tmp_path):
    """REVIEW ROUND 3's BLOCKER. `git diff` prints no content for a binary file, only
    `Binary files ... differ`, so dropping the `index` blob ids dropped the ONLY
    fingerprint of it: a resolution that swapped the bytes of a file the branch added took
    the judged commit's id and carried the verdict onto content no seat read."""
    from jarvis import branchproof

    repo, judged, swapped = _a_branch_that_added_a_binary_file(tmp_path)

    before = branchproof.diff_fingerprint(repo, "main", judged)
    assert before and before != branchproof.diff_fingerprint(repo, "main", swapped)


def _a_branch_that_added_a_latin1_text_file(tmp_path) -> tuple[Path, str, str]:
    r"""A real clone whose branch added a TEXT file that is not valid UTF-8.

    Returns (repo, judged, rewritten). `accents.txt` holds b"caf\xe9\n" — Latin-1, and no
    NUL byte, so `git diff` prints it as content rather than as `Binary files ... differ`.
    `rewritten` merges `origin/main` and changes that byte to `\xe8` while resolving.
    """
    up = make_git_project(tmp_path, "upstream")
    repo = tmp_path / "latin1work"
    subprocess.run(["git", "clone", "-q", str(up), str(repo)], check=True)
    _git(repo, "checkout", "-qb", "feature")
    (repo / "accents.txt").write_bytes(b"caf\xe9\n")
    _git(repo, "add", "accents.txt")
    _git(repo, "commit", "-qm", "the branch adds a latin-1 text file")
    judged = _git(repo, "rev-parse", "HEAD")
    (up / "b.txt").write_text("a line main added\n")
    _git(up, "add", "b.txt")
    _git(up, "commit", "-qm", "main moves")
    _git(repo, "fetch", "-q", "origin", "main")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
    (repo / "accents.txt").write_bytes(b"caf\xe8\n")
    _git(repo, "add", "accents.txt")
    _git(repo, "commit", "-qm", "a merge that rewrote an undecodable byte")
    return repo, judged, _git(repo, "rev-parse", "HEAD")


def test_a_merge_that_rewrites_an_undecodable_byte_is_not_the_judged_diff(tmp_path):
    r"""REVIEW ROUND 5's BLOCKER. The fingerprint is over the diff's BYTES. A text file's
    diff carries its content, so the binary rule does not cover it, and hashing that text
    as UTF-8 with `errors="replace"` mapped every undecodable byte to one U+FFFD:
    b"caf\xe9\n" and b"caf\xe8\n" hashed alike and a resolution that swapped them carried
    the verdict onto bytes no seat read."""
    from jarvis import branchproof

    repo, judged, rewritten = _a_branch_that_added_a_latin1_text_file(tmp_path)

    before = branchproof.diff_fingerprint(repo, "main", judged)
    assert before and before != branchproof.diff_fingerprint(repo, "main", rewritten)


def test_a_base_merge_that_moves_the_hunks_section_heading_still_matches(tmp_path):
    """The `@@`'s trailing SECTION HEADING is in the hash, unlike the ranges beside it, so
    the tolerance of §3.3 only holds while a base merge MOVES that line without changing
    which heading the branch's hunk sits under. `main` adds a function above `alpha`, so
    the heading `def alpha():` slides down four lines and the id must not move. A base
    change that lands BETWEEN the heading and the hunk renames it and does move the id:
    that is the conservative false refusal `diff_fingerprint` documents and accepts."""
    from jarvis import branchproof

    up = make_git_project(tmp_path, "upstream")
    (up / "mod.py").write_text(
        "def alpha():\n    return 1\n\n\ndef omega():\n    return 9\n")
    _git(up, "add", "mod.py")
    _git(up, "commit", "-qm", "the module")
    repo = tmp_path / "headwork"
    subprocess.run(["git", "clone", "-q", str(up), str(repo)], check=True)
    _git(repo, "checkout", "-qb", "feature")
    (repo / "mod.py").write_text(
        (repo / "mod.py").read_text().replace("return 9", "return 99"))
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-qm", "the branch's contribution")
    judged = _git(repo, "rev-parse", "HEAD")
    (up / "mod.py").write_text("def first():\n    return 0\n\n\n"
                               + (up / "mod.py").read_text())
    _git(up, "add", "mod.py")
    _git(up, "commit", "-qm", "main inserts a function above the whole module")
    _git(repo, "fetch", "-q", "origin", "main")
    _git(repo, "merge", "-q", "--no-edit", "origin/main")
    caught_up = _git(repo, "rev-parse", "HEAD")

    merge_base = _git(repo, "merge-base", "origin/main", caught_up)
    assert "@@ def alpha():" in _git(repo, "diff", f"{merge_base}..{caught_up}")
    before = branchproof.diff_fingerprint(repo, "main", judged)
    assert before and before == branchproof.diff_fingerprint(repo, "main", caught_up)


def _a_python_branch_that_merged_main(tmp_path) -> tuple[Path, str, str, str]:
    """A real clone where `main` moved INSIDE the file the branch also changed.

    Returns (repo, judged, caught_up, dedented). `caught_up` merges the moved `main`
    cleanly — the branch's own hunk is untouched and only its LINE NUMBERS shift, the
    tolerance §3.3 has to keep. `dedented` merges the same commit and pulls a `return` out
    of its `if` while resolving: whitespace only, a different program, and `git patch-id
    --stable` cannot see it (review round 1 of wo-659be188).
    """
    up = make_git_project(tmp_path, "upstream")
    (up / "mod.py").write_text("def f(x):\n    if x:\n        return 1\n    return 0\n")
    _git(up, "add", "mod.py")
    _git(up, "commit", "-qm", "the module")
    repo = tmp_path / "pywork"
    subprocess.run(["git", "clone", "-q", str(up), str(repo)], check=True)
    _git(repo, "checkout", "-qb", "feature")
    with open(repo / "mod.py", "a") as fh:
        fh.write("\n\ndef g(y):\n    if y:\n        return 2\n    return 0\n")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-qm", "the branch's contribution")
    judged = _git(repo, "rev-parse", "HEAD")
    (up / "mod.py").write_text("HEADER = 1\nHEADER2 = 2\n\n"
                               + (up / "mod.py").read_text())
    _git(up, "add", "mod.py")
    _git(up, "commit", "-qm", "main moves, above the branch's own lines")
    _git(repo, "fetch", "-q", "origin", "main")
    _git(repo, "merge", "-q", "--no-edit", "origin/main")
    caught_up = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", judged)
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "origin/main")
    (repo / "mod.py").write_text(
        (repo / "mod.py").read_text().replace("        return 2", "    return 2"))
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-qm", "a resolution that only re-indented")
    return repo, judged, caught_up, _git(repo, "rev-parse", "HEAD")


def test_a_resolution_that_only_re_indents_python_is_not_the_diff_that_was_judged(
        started, project, fake_gh, local_proof, tmp_path):
    """REVIEW ROUND 1's BLOCKER. `git patch-id --stable` strips whitespace before hashing,
    so a resolution that dedents a `return` out of its `if` — a different program, on the
    branch's own line — produced the SAME id and the verdict was carried onto it, with the
    record saying the diff was byte-identical.

    Both halves in one test: the ids from a real repository must differ, and the daemon
    driven with those ids must refuse the carry as `patch_id`."""
    git_fingerprint = local_proof["git_fingerprint"]
    repo, judged, _caught_up, dedented = _a_python_branch_that_merged_main(tmp_path)
    assert local_proof["git_fetch"](repo, "main")
    before = git_fingerprint(repo, "main", judged)
    after = git_fingerprint(repo, "main", dedented)
    assert before and after and before != after

    opt_in(started)
    store, wo = parked(project)
    local_proof["ids"] = {JUDGED: before, CAUGHT_UP: after}
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE1])

    poll(started, store)

    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert refusals(store, wo) == ["patch_id"]
    assert store.list_approvals(wo["id"]) == []


def test_a_base_merge_that_only_shifts_the_branchs_line_numbers_still_carries(
        started, project, fake_gh, local_proof, tmp_path):
    """THE TOLERANCE, and the reason the `@@` ranges are normalised rather than hashed.

    `main` added lines ABOVE the branch's own hunk in the same file, so the branch's
    contribution is unchanged and only the hunk header's numbers moved. A whitespace-exact
    hash that kept them would refuse this — the commonest catch-up on the fleet — so the
    ids must still be equal and the verdict must still carry with no round spent."""
    git_fingerprint = local_proof["git_fingerprint"]
    repo, judged, caught_up, _dedented = _a_python_branch_that_merged_main(tmp_path)
    assert local_proof["git_fetch"](repo, "main")
    before = git_fingerprint(repo, "main", judged)
    assert before and before == git_fingerprint(repo, "main", caught_up)

    opt_in(started)
    store, wo = parked(project)
    local_proof["ids"] = {JUDGED: before, CAUGHT_UP: before}
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE1])

    poll(started, store)

    assert refusals(store, wo) == []
    assert len(store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)) == 1
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1


def test_the_ancestry_question_is_asked_of_the_local_origin_ref(tmp_path):
    """§3.2 item 6 asked locally rather than with a `compare` call per commit: the fetch
    is already paid for, and the API cost of the walk would otherwise double."""
    from jarvis import branchproof

    repo, judged, caught_up, _evil = _a_branch_that_merged_main(tmp_path)
    merged_base = _git(repo, "rev-parse", caught_up + "^2")

    assert branchproof.is_ancestor(repo, merged_base, "origin/main")
    assert not branchproof.is_ancestor(repo, judged, "origin/main")
    assert not branchproof.is_ancestor(repo, "not-a-ref", "origin/main")


def test_a_fetch_of_a_ref_nobody_publishes_fails_rather_than_guessing(tmp_path):
    """The other open question: a `refs/pull/N/head` some origin refuses makes the feature
    inert there, so the failure is a False the caller has to handle — logged with the ref
    it asked for. And a ref is an argument, so a flag-shaped one never becomes one."""
    from jarvis import branchproof

    repo, _judged, _caught_up, _evil = _a_branch_that_merged_main(tmp_path)

    assert not branchproof.fetch(repo, "main", "refs/pull/7/head")
    assert not branchproof.fetch(repo, "--upload-pack=false")


def test_the_local_proof_module_runs_git_and_talks_to_nothing_else():
    """`test_this_module_writes_only_the_branch_update`'s guard, for the new module. `ci`
    is held against a `gh` allowlist and `github` against a read-only one; this module is
    the local-`git` home, so what it is held against is that it shells out to `git` alone
    and imports neither of them (§3.1)."""
    from jarvis import branchproof

    tree = ast.parse(Path(branchproof.__file__).read_text())
    argv0 = {node.elts[0].value for node in ast.walk(tree)
             if isinstance(node, ast.List) and node.elts
             and isinstance(node.elts[0], ast.Constant)
             and isinstance(node.elts[0].value, str)}
    assert argv0 == {"git"}, f"this module runs more than git: {sorted(argv0)}"
    imported = {(n.module or "") for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)}
    assert not imported & {"ci", "github", "bugreport", "project_store", "ops"}
    assert set(ci.WRITE_VERBS) == {("pr", "update-branch")}
