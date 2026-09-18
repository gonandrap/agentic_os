"""Settling a work order asserts the deliverable (GitHub issue #232).

`tests/test_landing.py` proves the two predicates in isolation. This file proves what the
OS does with them: the two landings that refuse, the escape that records an abandonment,
and the standing sweep that answers "what has this fleet delivered that nobody merged".

THE SWEEP IS TWO STEPS NOW, and they are separate on purpose. `Daemon.discover_pull_requests`
is the half with a network: it asks GitHub which pull requests a completed order's
BRANCHES ever had and writes the answer to the timeline. `invariants.check_work_lands` is
the half that judges, and it reads that event and nothing else. Every sweep test below
drives both, in that order, through the fake `gh` — because the joint between them is a
dict key, and a key that did not match would leave the check permanently, invisibly
quiet.

EVERY REFUSAL IS PAIRED WITH THE CASE THAT MUST STILL PASS, because a check that blocks
a worker from finishing is worse than no check at all — it gets switched off within a
day. The pairings that matter most are the two ways an order legitimately settles with
no pull request of its own: it produced no code (the 60-of-89 case), or its code went in
through a pull request that merged.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from jarvis import daemon as daemon_mod
from jarvis import invariants, landing, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"
OTHER_PR = "https://github.com/acme/proj/pull/8"
THIRD_PR = "https://github.com/acme/proj/pull/9"
#: A HIGHER number than `PR`, and that is the whole of it: `wo-cd73c537` had #116 opened
#: on the branch whose #81 had already merged, and the report has to name the later one.
LATER_PR = "https://github.com/acme/proj/pull/116"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _feature(salt: str, n: int = 30) -> str:
    return "".join(f"def {salt}_helper_{i}(argument):  # {salt} feature line {i}\n"
                   f"    return compute_{salt}_result({i}, argument)\n"
                   for i in range(n))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """The shipped `project` fixture, upgraded into a repository with a real history.

    `make_git_project` leaves an EMPTY repository — no commit, no remote — which is the
    right default for every other suite and useless here: with no default branch there is
    no "ahead of the default branch" to measure, so `landing.authored` answers
    `unreadable` and nothing is ever refused. The branch is named explicitly and is
    `trunk` (kn-4b6f18f5).
    """
    _git(project, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (project / "app.py").write_text(_feature("base", 10))
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    # Named `acme/proj` so `github.checked_pr_url` accepts the `PR` constant below: it
    # refuses a pull request whose owner/repo does not match this checkout's `origin`,
    # and the poll is part of the seam these tests exercise.
    origin = project.parent / "acme" / "proj.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(project.parent, "init", "--bare", "-q", str(origin))
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "origin", "trunk")
    _git(project, "remote", "set-head", "origin", "trunk")
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _order(project: Path, title: str = "add feature X", *, code: str | None = None,
           dirty: str | None = None) -> dict:
    """A work order with a worktree, carrying `code` as a commit and `dirty` uncommitted.

    `worktree` is set by hand because dispatch is what normally sets it and these tests
    never dispatch: the column is the only thing tying a work order to a directory.
    """
    wo = ops.create_work_order("proj_a", title)
    wt = project / ".claude" / "worktrees" / wo["id"]
    _git(project, "worktree", "add", "-q", "-b", f"worktree-{wo['id']}", str(wt),
         "trunk")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], worktree=wo["id"])
    finally:
        store.close()
    if code:
        (wt / f"{code}.py").write_text(_feature(code))
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"add {code}")
    if dirty:
        (wt / f"{dirty}.py").write_text(_feature(dirty))
    return {**wo, "worktree_path": wt}


def _settle(project: Path, *wo_ids: str) -> None:
    """Put work orders into `completed` the way the six stranded ones got there.

    They completed through Mode A, before any of this existed. Reaching that state via
    `ops.finish` is impossible now by construction, which is the point of the change —
    so the fixture writes the status the pre-fix OS would have written.
    """
    store = ProjectStore(project)
    try:
        for wo_id in wo_ids:
            store.set_status(wo_id, "completed")
    finally:
        store.close()


def _events(project: Path, wo_id: str, kind: str) -> list[dict]:
    store = ProjectStore(project)
    try:
        return store.events_of_kind(wo_id, kind)
    finally:
        store.close()


def _row(project: Path, wo_id: str) -> dict:
    store = ProjectStore(project)
    try:
        return store.get_work_order(wo_id)
    finally:
        store.close()


# -- Mode A: the pull request named in prose, and the --pr that was not passed ----------


def test_finish_over_commits_with_no_pr_is_refused_and_names_the_url_in_the_summary(
        started, project):
    """Four of the six stranded orders finished with a summary naming a draft PR.

    The OS does not ADOPT that URL — it has verified nothing about it — but it stops
    pretending it did not see it, which is the difference between this refusal and a
    generic one.
    """
    wo = _order(project, code="launcher")

    with pytest.raises(ops.OpsError) as e:
        ops.finish(wo["id"], f"Opened a draft PR at {PR} for review.")

    assert PR in str(e.value)
    assert "--pr" in str(e.value)
    # The record is untouched: a summary saved beside a refusal reads afterwards like a
    # work order that finished.
    assert _row(project, wo["id"])["result_summary"] in (None, "")
    assert _row(project, wo["id"])["status"] != "completed"


def test_the_same_finish_with_pr_parks_it_and_never_reads_the_worktree(started, project):
    """The pairing. Identical worktree, identical commits — only `--pr` differs."""
    wo = _order(project, code="launcher")

    out = ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert out["status"] == "waiting_pr_merge"
    assert _row(project, wo["id"])["pr_url"] == PR


def test_an_order_that_produced_no_code_finishes_exactly_as_before(started, project):
    """THE NEGATIVE CONTROL THAT DECIDES WHETHER THIS SHIPS.

    A planner, a knowledge-base write, an investigation, an answered question: 60 of the
    89 candidates in the audit. Its worktree exists and is clean, so there is nothing to
    land and nothing to refuse.
    """
    wo = _order(project, "answer a question")

    assert ops.finish(wo["id"], "no code needed")["status"] == "completed"


def test_uncommitted_work_alone_is_enough_to_refuse(started, project):
    """A worker that never committed has still produced the thing that gets lost."""
    wo = _order(project, dirty="scratch")

    with pytest.raises(ops.OpsError) as e:
        ops.finish(wo["id"], "done")

    assert "uncommitted" in str(e.value)


# -- the escape: an abandonment is a legitimate outcome, cheaply recorded --------------


def test_abandon_completes_the_order_and_writes_down_what_was_dropped(started, project):
    """The goal is that the decision is WRITTEN DOWN, not that it is prevented."""
    wo = _order(project, code="spike")

    out = ops.finish(wo["id"], "spiked it; not worth landing",
                     abandon="the approach does not work, see the summary")

    assert out["status"] == "completed"
    [event] = _events(project, wo["id"], "abandoned")
    payload = json.loads(event["payload"])
    assert payload["reason"] == "the approach does not work, see the summary"
    assert payload["commits"] == 1
    assert payload["branch"] == f"worktree-{wo['id']}"


def test_the_remedy_the_sweep_prints_works_on_an_order_already_completed(started,
                                                                          project,
                                                                          fake_gh):
    """INV-WORK-LANDED tells the user to run `wo finish --abandon` on a COMPLETED order.

    Every order the sweep names is already settled — that is what the sweep looks at — so
    the remedy it prints is only a remedy if `finish` accepts one. A violation whose fix
    is refused by the command it names is a report that cannot be answered, and it would
    go on naming the same order every hour until somebody switched the check off.
    """
    wo = _order(project, "launcher contract", code="launcher")
    _settle(project, wo["id"])
    fake_gh.set_pr(PR, "OPEN", head_ref=f"worktree-{wo['id']}")
    _discover(started, project)
    [violation] = _violations(project)
    assert f"finish {wo['id']}" in violation.detail and "--abandon" in violation.detail

    ops.finish(wo["id"], "dropping it", abandon="superseded by the rewrite")

    assert _row(project, wo["id"])["status"] == "completed"
    assert _violations(project) == []


def test_an_abandonment_is_superseded_by_a_later_ordinary_finish(started, project):
    """The newest of the two events wins, or an order excused once is excused for ever."""
    wo = _order(project, code="spike")
    ops.finish(wo["id"], "dropping it", abandon="not worth landing")

    with pytest.raises(ops.OpsError):
        ops.finish(wo["id"], "actually I did land it")


# -- Mode B: accepting assumptions is not a closure verb --------------------------------


def test_accepting_assumptions_will_not_complete_an_order_holding_unlanded_commits(
        started, project):
    """THE SHARP EDGE. wo-69a06ff4 and wo-01d30340 both died here.

    The worker went idle without ever calling `jarvis wo finish`, the order parked at
    `needs_review`, and the user closed it by accepting its assumptions — which completed
    it and wrote `result_summary` NULL and `pr_url` NULL over the fact that a commit
    existed. The mirror of the rule `wo ack` and `wo done` already follow.
    """
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s rather than every tick")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
        store.flag_attention(wo["id"], "worker idle without jarvis wo finish")
    finally:
        store.close()

    out = ops.review_work_order(wo["id"], accept=True)

    assert out["status"] == "needs_review"
    assert out["reviewed"] == 1          # the assumptions WERE decided: that is its job
    row = _row(project, wo["id"])
    assert row["status"] == "needs_review"
    assert row["attention_reason"] == invariants.UNLANDED_BLOCKER
    # The evidence is preserved rather than written over, which is the whole of Mode B.
    import json
    payload = json.loads(_events(project, wo["id"], "work_unlanded")[-1]["payload"])
    assert payload["commits"] == 1
    assert payload["branch"] == f"worktree-{wo['id']}"


def test_accepting_assumptions_completes_an_order_that_produced_no_code(started, project):
    """The pairing: the review still does what it always did when nothing is stranded."""
    wo = _order(project, "decide something")
    ops.assume(wo["id"], "used the existing helper")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    assert ops.review_work_order(wo["id"], accept=True)["status"] == "completed"


def test_a_merged_pull_request_is_not_re_flagged_by_its_own_commits(started, project):
    """THE FALSE POSITIVE THAT WOULD ARRIVE BY THE BACK DOOR.

    `review_work_order` blanks `pr_url` before landing when the poll has already settled
    that pull request. For a MERGED one the code IS on the default branch — so a landing
    that read the caller's blanked dict instead of the record would refuse to complete a
    work order over the very commits that landed it.
    """
    wo = _order(project, code="merged_feature")
    ops.assume(wo["id"], "named it merged_feature")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url=PR, pr_state="MERGED")
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    assert ops.review_work_order(wo["id"], accept=True)["status"] == "completed"


def test_the_parked_flag_survives_a_reconcile_tick(started, project):
    """kn-eafe383a's rule, one level over: a flag `true_blockers` cannot re-derive is
    relabelled by INV-ATTENTION-REASON on the next tick, and this one would become the
    generic "worker stopped without finishing" — the opposite of what is true here.

    The pairing is the ANSWER: re-finishing behind a pull request ends the episode, so
    the derivation stops and the work order leaves the attention list of its own accord.
    """
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()
    ops.review_work_order(wo["id"], accept=True)

    store = ProjectStore(project)
    try:
        invariants.check_project(store, repair=True)
        assert store.get_work_order(wo["id"])["attention_reason"] == \
            invariants.UNLANDED_BLOCKER
    finally:
        store.close()

    ops.finish(wo["id"], "opened the PR after all", pr_url=PR)

    store = ProjectStore(project)
    try:
        assert store.work_unlanded_open(wo["id"]) is False
        assert invariants.UNLANDED_BLOCKER not in invariants.true_blockers(
            store, store.get_work_order(wo["id"]))
    finally:
        store.close()


def test_wo_done_records_the_unlanded_work_and_still_closes_it(started, project):
    """The one landing that does NOT refuse, and the reason is that it is not silence.

    `jarvis wo done` is the user saying so — the documented exit for a pull request that
    will never merge — and refusing it would leave them no way to close the order at all.
    What issue #232 asks for is that the evidence stops being written over, so the event
    goes on the record and the closure proceeds.
    """
    wo = _order(project, code="stranded")

    assert ops.mark_done(wo["id"])["status"] == "completed"

    import json
    payload = json.loads(_events(project, wo["id"], "work_unlanded")[-1]["payload"])
    assert payload["commits"] == 1
    assert payload["closed_by"] == "marked_done"


# -- the standing report: jarvis doctor ------------------------------------------------



def _violations(project: Path) -> list[invariants.Violation]:
    store = ProjectStore(project)
    try:
        return [v for v in invariants.check_project(store, repair=True, slow=True)
                if v.invariant == "INV-WORK-LANDED"]
    finally:
        store.close()


def _discover(daemon: Daemon, project: Path) -> None:
    """One landing sweep's discovery pass — `Daemon.discover_pull_requests`.

    The step the invariant reads. Nothing here calls it implicitly: the two are joined by
    a timeline event on purpose, and a test that could not run one without the other
    could not prove the check is silent before discovery has happened.
    """
    store = ProjectStore(project)
    try:
        daemon.discover_pull_requests(daemon.catalog.project("proj_a"), store)
    finally:
        store.close()


def _pr_calls(fake_gh) -> list[list[str]]:
    """Every `gh pr list` this test has caused, as argv. The cost, counted."""
    return [c["argv"] for c in fake_gh.calls if c["argv"][:2] == ["pr", "list"]]


def test_a_merged_pull_request_is_the_answer_and_an_open_one_is_a_violation(
        started, project, fake_gh):
    """THE NARROWED PREDICATE, all three reportable states in one sweep.

    Merged is satisfied with no arithmetic: the content test scored three orders that had
    merged months earlier and been refactored since at 44%, 31% and 72%, which is what
    the user's ruling of 2026-09-18 struck out.

    The open one is a violation that says something TRUE — the work is delivered and
    waiting on a merge — and its remedy is merge-shaped. "Its code is not on main" sent
    the reader hunting for a branch that was sitting in a pull request all along.
    """
    merged = _order(project, "the cap", code="cap")
    waiting = _order(project, "launcher contract", code="launcher")
    refused = _order(project, "superseded", code="superseded")
    _settle(project, merged["id"], waiting["id"], refused["id"])
    fake_gh.set_pr(PR, "MERGED", head_ref=f"worktree-{merged['id']}")
    fake_gh.set_pr(OTHER_PR, "OPEN", head_ref=f"worktree-{waiting['id']}")
    fake_gh.set_pr(THIRD_PR, "CLOSED", head_ref=f"worktree-{refused['id']}")

    _discover(started, project)
    found = {v.wo_id: v for v in _violations(project)}

    assert set(found) == {waiting["id"], refused["id"]}
    assert found[waiting["id"]].context["verdict"] == landing.AWAITING_MERGE
    assert "waiting on a merge" in found[waiting["id"]].detail
    assert "Merge it, or record the decision to drop it" in found[waiting["id"]].detail
    assert found[refused["id"]].context["verdict"] == landing.REFUSED
    assert "delivered and refused" in found[refused["id"]].detail
    assert "Re-open and merge it" in found[refused["id"]].detail
    assert not found[waiting["id"]].repaired    # what to do with it is the user's call


def test_an_order_with_no_pull_request_is_silent_and_costs_no_round_trip(
        started, project, fake_gh):
    """THE NARROWING, stated where it takes effect. wo-5a6b2d6d is the case it gives up:
    a completed planner whose only product is a WIP commit on `rescue/wo-5a6b2d6d` that
    no pull request was ever opened for. Nothing reports it now, by the user's ruling —
    validation is where an order that settles with nothing delivered belongs.

    And the order with no BRANCH at all never reaches GitHub, which is what makes the
    sweep affordable: 60 of the audit's 89 candidates were planners, investigations and
    knowledge-base writes, and each of them used to cost a per-file git walk.
    """
    committed = _order(project, "never opened a pull request", code="orphan")
    planner = ops.create_work_order("proj_a", "wrote a plan")
    _settle(project, committed["id"], planner["id"])

    _discover(started, project)

    assert _violations(project) == []
    # The one with a branch was asked about; the one without was not.
    assert _pr_calls(fake_gh) == [["pr", "list", "--head", f"worktree-{committed['id']}",
                                  "--state", "all", "--limit", "20",
                                  "--json", "number,url,state"]]


def test_a_null_pr_url_does_not_exempt_an_order_from_the_sweep(
        started, project, fake_gh):
    """wo-5eedc84d: `pr_url` NULL in the production record, pull request #42 all along.

    The trap the whole discovery step exists to avoid. `if not wo["pr_url"]: skip` is the
    obvious way to scope this invariant to orders that have a pull request, and it would
    silently exempt exactly the orders it exists for — a worse failure than the noise it
    replaced, because nothing shows it happening.

    Asserted in BOTH directions: the merged one is silent because GitHub says merged, not
    because the column is empty, and the open one proves an empty column is no excuse.
    """
    landed = _order(project, "the cap", code="cap")
    open_pr = _order(project, "launcher contract", code="launcher")
    _settle(project, landed["id"], open_pr["id"])
    assert _row(project, landed["id"])["pr_url"] is None
    assert _row(project, open_pr["id"])["pr_url"] is None
    fake_gh.set_pr(PR, "MERGED", head_ref=f"worktree-{landed['id']}")
    fake_gh.set_pr(OTHER_PR, "OPEN", head_ref=f"worktree-{open_pr['id']}")

    _discover(started, project)
    found = _violations(project)

    assert [v.wo_id for v in found] == [open_pr["id"]]
    assert found[0].context["pr_url"] == OTHER_PR


def test_a_newer_pull_request_on_the_same_branch_outranks_the_recorded_one(
        started, project, fake_gh):
    """wo-cd73c537: the record names #81, which merged; #116 is open and carries the rest.

    The old check read `pr_url`, saw a merged pull request, fell through to the content
    test and reported 2% — "stranded" — about work that was in an open pull request the
    whole time. Asking by BRANCH sees both, and the OPEN one is what the report is about:
    an earlier merge does not deliver what is still unmerged.
    """
    wo = _order(project, "the validation layer", code="validation")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    try:
        ops.complete_merged(store, store.get_work_order(wo["id"]),
                            merged_at="2026-08-02T10:00:00Z")
    finally:
        store.close()
    assert _row(project, wo["id"])["status"] == "completed"
    fake_gh.set_pr(PR, "MERGED", head_ref=f"worktree-{wo['id']}")
    fake_gh.set_pr(LATER_PR, "OPEN", head_ref=f"worktree-{wo['id']}")

    _discover(started, project)
    found = _violations(project)

    assert [v.wo_id for v in found] == [wo["id"]]
    assert found[0].context["verdict"] == landing.AWAITING_MERGE
    assert found[0].context["pr_url"] == LATER_PR          # not the recorded one
    assert found[0].context["pull_requests"] == ["#116 OPEN", "#7 MERGED"]


def test_a_merged_pull_request_with_a_tail_on_its_branch_is_no_longer_reported(
        started, project, fake_gh):
    """THE SECOND THING THE NARROWING GIVES UP, and it is worth stating as a test.

    Issue #232's Mode C — a first pull request merges and the worker keeps going — used
    to be caught by the `merged-tail` rung, which measured the branch against the sha
    GitHub merged. That rung went with the rest of the content machinery: the pull
    request merged, so the invariant is satisfied, and what was pushed to the branch
    afterwards is not this check's business. wo-752eced8 is the live example, where the
    only finding was one uncommitted file in a leftover worktree.
    """
    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z",
                   head_oid=_git(wo["worktree_path"], "rev-parse", "HEAD").strip(),
                   head_ref=f"worktree-{wo['id']}")
    store = ProjectStore(project)
    try:
        started.poll_pull_requests(started.catalog.project("proj_a"), store)
    finally:
        store.close()
    assert _row(project, wo["id"])["status"] == "completed"
    (wo["worktree_path"] / "onboarding.py").write_text(_feature("onboarding"))
    _git(wo["worktree_path"], "add", "-A")
    _git(wo["worktree_path"], "commit", "-qm", "the onboarding half")

    _discover(started, project)

    assert _violations(project) == []


def test_complete_merged_writes_the_sha_that_merged_onto_the_event(started, project):
    """WHICH COMMIT merged, on the record, because the event IS the record.

    No longer an input to any check — the landing sweep judges the pull request and never
    measures a branch against this sha — but it is the only place the merged commit is
    written down at all, and `ops.complete_merged`'s contract is that its event says what
    happened. Asserted on the event rather than inferred from a verdict: a producer whose
    consumer has gone is exactly the thing that rots in silence.
    """
    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    sha = "9f1c2ab4d5e6f708192a3b4c5d6e7f8091a2b3c4"

    store = ProjectStore(project)
    try:
        ops.complete_merged(store, store.get_work_order(wo["id"]),
                            merged_at="2026-08-02T10:00:00Z", head_oid=sha)
    finally:
        store.close()

    [event] = _events(project, wo["id"], "pr_merged")
    assert json.loads(event["payload"])["head_oid"] == sha


def test_the_check_is_silent_until_discovery_has_run_and_never_guesses(
        started, project, fake_gh):
    """The invariant is a pure timeline read, so an order nothing has looked at yet is
    silent — the same answer as one with no pull request.

    Deliberate, and it is the cost of keeping `gh` out of the invariant. A `jarvis doctor`
    on a project the daemon has never swept reports nothing here rather than a guess, and
    the daemon's hourly sweep is what fills it in. The pairing is the second half: once
    discovery HAS run, the same order is reported.
    """
    wo = _order(project, "launcher contract", code="launcher")
    _settle(project, wo["id"])
    fake_gh.set_pr(PR, "OPEN", head_ref=f"worktree-{wo['id']}")

    assert _violations(project) == []
    assert _events(project, wo["id"], "pr_discovered") == []

    _discover(started, project)

    assert [v.wo_id for v in _violations(project)] == [wo["id"]]


def test_the_sweep_is_silent_on_abandoned_orders_and_never_asks_about_them(
        started, project, fake_gh):
    """An abandonment is a decision that was taken, and asking GitHub about it would be
    spending a round trip an hour to re-report something the user already closed."""
    dropped = _order(project, "spiked it", code="spike")
    ops.finish(dropped["id"], "not landing this", abandon="the approach does not work")
    fake_gh.set_pr(PR, "OPEN", head_ref=f"worktree-{dropped['id']}")

    _discover(started, project)

    assert _violations(project) == []
    assert _pr_calls(fake_gh) == []


def test_an_order_the_user_marked_done_is_not_reported_by_the_sweep_for_ever(
        started, project, fake_gh):
    """`jarvis wo done` is a decision, and the sweep has to read it as one.

    `mark_done` is the one landing that records instead of refusing: the user closing an
    order over a pull request that will never merge is the documented exit, and it writes
    `work_unlanded` with `closed_by: marked_done`. But the sweep only ever excused
    `abandoned` — so the order it just closed came straight back with a remedy telling it
    to run `jarvis wo finish --abandon` on a work order that is already `completed`.
    Every hour. For ever. That is the hourly nag spec §3 is written against, and it gets
    the checker switched off within a day.

    The pairing is the order beside it, which was NOT closed and must still be named.
    """
    closed = _order(project, "will never merge", code="orphaned")
    still_open = _order(project, "launcher contract", code="launcher")
    _settle(project, still_open["id"])
    fake_gh.set_pr(PR, "OPEN", head_ref=f"worktree-{closed['id']}")
    fake_gh.set_pr(OTHER_PR, "OPEN", head_ref=f"worktree-{still_open['id']}")

    assert ops.mark_done(closed["id"])["status"] == "completed"
    # The evidence is on the record either way — that is what issue #232 asked for.
    assert json.loads(_events(project, closed["id"], "work_unlanded")[-1]["payload"]
                      )["closed_by"] == "marked_done"

    _discover(started, project)
    found = _violations(project)

    assert [v.wo_id for v in found] == [still_open["id"]]
    assert _violations(project) == found     # and it stays silent on the next sweep too


def test_a_settled_discovery_is_not_re_asked_every_sweep_and_an_unsettled_one_is(
        started, project, fake_gh):
    """The TTL, which is what stops the sweep costing a round trip per completed order
    per hour for ever — and the exception that makes it correct.

    A `landed` answer is not asked again until `PR_DISCOVERY_TTL_SECONDS`, because a
    merged pull request stays merged. It is not kept FOR EVER, though, and wo-cd73c537 is
    why: #116 was opened on its branch after #81 merged, so an answer cached permanently
    would have hidden the exact shape this check was rebuilt for. An OPEN one is re-asked
    every sweep, because that is the one a merge resolves.
    """
    landed = _order(project, "the cap", code="cap")
    waiting = _order(project, "launcher contract", code="launcher")
    _settle(project, landed["id"], waiting["id"])
    fake_gh.set_pr(PR, "MERGED", head_ref=f"worktree-{landed['id']}")
    fake_gh.set_pr(OTHER_PR, "OPEN", head_ref=f"worktree-{waiting['id']}")

    _discover(started, project)
    assert len(_pr_calls(fake_gh)) == 2
    _discover(started, project)

    # The open one alone; the merged one is inside its TTL.
    assert [c[3] for c in _pr_calls(fake_gh)[2:]] == [f"worktree-{waiting['id']}"]

    # ...and past the TTL it is asked again, which is how a pull request opened AFTER a
    # merge is ever noticed at all.
    store = ProjectStore(project)
    try:
        store.conn.execute(
            "UPDATE wo_events SET ts = ts - ? WHERE wo_id = ? AND kind = 'pr_discovered'",
            (daemon_mod.PR_DISCOVERY_TTL_SECONDS + 1, landed["id"]))
        store.conn.commit()
    finally:
        store.close()
    fake_gh.set_pr(LATER_PR, "OPEN", head_ref=f"worktree-{landed['id']}")
    _discover(started, project)

    assert {v.wo_id for v in _violations(project)} == {landed["id"], waiting["id"]}


def test_one_sweep_asks_about_at_most_the_cap_and_the_rest_arrive_next_time(
        started, project, fake_gh, monkeypatch):
    """The cap is about the TICK. A project with two hundred cold completed orders would
    otherwise spend two hundred round trips inside one daemon tick, and the daemon has
    everything else to do — so it fills in over several sweeps instead.

    Counted rather than timed, for the obvious reason.
    """
    monkeypatch.setattr(daemon_mod, "PR_DISCOVERY_PER_SWEEP", 2)
    orders = [_order(project, f"order {n}", code=f"code{n}") for n in range(3)]
    _settle(project, *[o["id"] for o in orders])

    _discover(started, project)
    assert len(_pr_calls(fake_gh)) == 2

    _discover(started, project)

    # The third one, and no re-ask of the two already settled as `no-pull-request`.
    assert len(_pr_calls(fake_gh)) == 3
    assert {c[3] for c in _pr_calls(fake_gh)} == {f"worktree-{o['id']}" for o in orders}


def test_a_gh_that_cannot_answer_leaves_the_last_record_standing(
        started, project, fake_gh):
    """A broken poll must make the sweep STALE, never wrong.

    The failure here is `gh` itself — the same command against the same repository, once
    per work order — so the pass stops rather than recording N identical failures. What
    it guards against is the shape an empty answer would take: `no-pull-request`, which
    is SILENT, so a `gh` failure read as "no pull requests" would quietly exempt every
    completed order in the project.
    """
    wo = _order(project, "launcher contract", code="launcher")
    _settle(project, wo["id"])
    fake_gh.set_pr(PR, "OPEN", head_ref=f"worktree-{wo['id']}")
    _discover(started, project)
    assert [v.wo_id for v in _violations(project)] == [wo["id"]]

    fake_gh.fail("gh: could not read credentials")
    _discover(started, project)

    # Still reported, off the record discovery left behind — not dropped, not re-judged.
    assert [v.wo_id for v in _violations(project)] == [wo["id"]]
    assert len(_events(project, wo["id"], "pr_discovered")) == 1


def test_the_read_only_doctor_gives_the_same_answer_and_writes_nothing(
        started, project, fake_gh):
    """What the plain `jarvis doctor` a human types does — which is now exactly what the
    daemon's sweep does, and that is the change.

    The check this replaced kept a `landing_checked` cache only a repairing run could
    write, so `jarvis doctor` paid a full per-file git walk every time AND answered less:
    it could not refresh the default branch, so the content verdict was withheld as
    `unknown`. There is no write on this path now and no ref that can be stale, so
    `repair=False` changes nothing about the answer.
    """
    waiting = _order(project, "launcher contract", code="launcher")
    landed = _order(project, "the cap", code="cap")
    _settle(project, waiting["id"], landed["id"])
    fake_gh.set_pr(PR, "OPEN", head_ref=f"worktree-{waiting['id']}")
    fake_gh.set_pr(OTHER_PR, "MERGED", head_ref=f"worktree-{landed['id']}")
    _discover(started, project)

    store = ProjectStore(project)
    try:
        before = len(store.events_of_kind(waiting["id"], "pr_discovered"))
        found = [v for v in invariants.check_project(store, repair=False, slow=True)
                 if v.invariant == "INV-WORK-LANDED"]
        after = len(store.events_of_kind(waiting["id"], "pr_discovered"))
    finally:
        store.close()

    assert [v.wo_id for v in found] == [waiting["id"]]
    assert before == after == 1
    assert [v.wo_id for v in _violations(project)] == [waiting["id"]]
