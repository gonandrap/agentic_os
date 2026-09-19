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
from pathlib import Path

import pytest

from jarvis import automerge, ci, db, gates, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import BASE_RED_NOTE, status_label
from jarvis.project_store import ProjectStore

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
