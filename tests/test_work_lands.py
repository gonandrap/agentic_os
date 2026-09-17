"""Settling a work order asserts the deliverable (GitHub issue #232).

`tests/test_landing.py` proves the DETECTOR against real branches. This file proves what
the OS does with it: the two landings that refuse, the escape that records an
abandonment, and the standing sweep that answers "what has this fleet produced that is
not on the default branch".

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

from jarvis import invariants, landing, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"
OTHER_PR = "https://github.com/acme/proj/pull/8"


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
                                                                          project):
    """INV-WORK-LANDED tells the user to run `wo finish --abandon` on a COMPLETED order.

    Every order the sweep names is already settled — that is what the sweep looks at — so
    the remedy it prints is only a remedy if `finish` accepts one. A violation whose fix
    is refused by the command it names is a report that cannot be answered, and it would
    go on naming the same order every hour until somebody switched the check off.
    """
    wo = _order(project, "launcher contract", code="launcher")
    _settle(project, wo["id"])
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


def test_the_sweep_reports_a_completed_order_whose_code_is_not_on_the_branch(
        started, project):
    """The audit that found the six was a one-off; this is it, running on demand."""
    stranded = _order(project, "launcher contract", code="launcher")
    landed = _order(project, "the cap", code="cap")
    # Settled by hand, which is exactly how the six got there: they reached `completed`
    # through Mode A before any of this existed, and the sweep's job is that historical
    # backlog. Going through `finish` would be the wrong fixture — it now refuses.
    _settle(project, stranded["id"], landed["id"])
    _git(project, "merge", "--squash", "-q", f"worktree-{landed['id']}")
    _git(project, "commit", "-qm", f"[{landed['id']}] the cap (#9)")
    _git(project, "push", "-q", "origin", "trunk")

    found = _violations(project)

    assert [v.wo_id for v in found] == [stranded["id"]]
    assert found[0].context["verdict"] == landing.STRANDED
    assert "launcher.py" in found[0].context["missing_files"]
    assert not found[0].repaired      # what to do with it is the user's call


def test_complete_merged_writes_the_sha_that_merged_onto_the_event(started, project):
    """The one place that sha can live, and the sweep's only exact answer to Mode C.

    `pr_state` is stale by construction with one permitted reader (kn-dbc4971d) and
    re-asking GitHub months later is a round trip per settled work order, so
    `landing.assess` reads `head_oid` off this payload and nowhere else. Asserted on the
    event rather than inferred from a verdict: the producer and the consumer are joined
    by a dict key, and a key that did not match would make the exact rung silently
    dormant for ever — indistinguishable from a fleet with nothing stranded.
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


def test_a_tail_after_the_merged_sha_is_stranded_and_a_clean_merge_is_not(
        started, project, fake_gh):
    """MODE C END TO END, through every joint the sha crosses.

    `gh pr view` -> `github.pr_view` -> `Daemon.poll_pull_requests` -> `complete_merged`
    -> the `pr_merged` event -> `check_work_lands` -> `assess(pr_head_oid=...)`. Calling
    `assess` directly with a sha proves the rung and none of that carriage, and by
    assumption [6] the rung cannot fire for any order that merged before this shipped —
    so this test is the only thing standing between a mistyped key and a check that is
    permanently, invisibly quiet.

    Both work orders merged. The difference is the work that came after: three of the
    six stranded orders had a first pull request merge while the worker kept going.
    """
    tailed = _order(project, "launcher contract", code="launcher")
    clean = _order(project, "the cap", code="cap")
    for wo, pr in ((tailed, PR), (clean, OTHER_PR)):
        ops.finish(wo["id"], "opened a PR", pr_url=pr)
        # The sha GitHub reports as merged is the branch tip at the moment it merged.
        merged_sha = _git(wo["worktree_path"], "rev-parse", "HEAD").strip()
        _git(project, "merge", "--squash", "-q", f"worktree-{wo['id']}")
        _git(project, "commit", "-qm", f"[{wo['id']}] {wo['title']} (#9)")
        fake_gh.set_pr(pr, "MERGED", merged_at="2026-08-02T10:00:00Z",
                       head_oid=merged_sha)
    _git(project, "push", "-q", "origin", "trunk")

    store = ProjectStore(project)
    try:
        started.poll_pull_requests(started.catalog.project("proj_a"), store)
    finally:
        store.close()
    assert _row(project, tailed["id"])["status"] == "completed"

    # ...and the worker kept going after the merge. This is the whole of Mode C.
    (tailed["worktree_path"] / "onboarding.py").write_text(_feature("onboarding"))
    _git(tailed["worktree_path"], "add", "-A")
    _git(tailed["worktree_path"], "commit", "-qm", "the onboarding half")

    found = _violations(project)

    assert [v.wo_id for v in found] == [tailed["id"]]
    assert found[0].context["rung"] == "merged-tail"   # the exact rung, not the heuristic
    assert found[0].context["verdict"] == landing.STRANDED


def test_the_sweep_is_silent_on_orders_that_produced_nothing_and_on_abandoned_ones(
        started, project):
    """The two exclusions, asserted together because either one alone leaves a report
    nobody reads: 67% of the audit's candidates produced no code, and an abandonment is
    a decision that was taken."""
    ops.finish(_order(project, "answered it")["id"], "no code needed")
    dropped = _order(project, "spiked it", code="spike")
    ops.finish(dropped["id"], "not landing this", abandon="the approach does not work")

    assert _violations(project) == []


def test_a_partly_landed_order_is_reported_and_its_verdict_is_never_cached(
        started, project):
    """THE ONLY RUNG THAT CAN SEE THE FLEET THAT ALREADY EXISTS, at the sweep's level.

    `tests/test_landing.py` proves the detector returns `PARTIAL`; this proves the sweep
    does something with it. Both halves matter and the second one more: `PARTIAL` must
    NOT be written to `landing_checked`, because that cache is never recomputed and the
    order's tail is exactly the thing a later merge resolves. A sweep that cached this
    would answer "all clear" for ever on a work order that is half on the branch.

    The shape is issue #232's Mode C without a `head_oid` to key on (spec §7): the first
    pull request squash-merged, the worker kept going, and the second half never left
    the branch.
    """
    wo = _order(project, "launcher contract", code="first")
    _settle(project, wo["id"])
    _git(project, "merge", "--squash", "-q", f"worktree-{wo['id']}")
    _git(project, "commit", "-qm", f"[{wo['id']}] the first half (#9)")
    _git(project, "push", "-q", "origin", "trunk")
    (wo["worktree_path"] / "second.py").write_text(_feature("second"))
    _git(wo["worktree_path"], "add", "-A")
    _git(wo["worktree_path"], "commit", "-qm", "the tail nobody merged")

    found = _violations(project)

    assert [v.wo_id for v in found] == [wo["id"]]
    assert found[0].context["verdict"] == landing.PARTIAL
    assert found[0].context["rung"] == "coverage"
    assert "second.py" in found[0].context["missing_files"]
    # Re-derived, not remembered: the moment the tail merges, this stops being reported.
    assert _events(project, wo["id"], "landing_checked") == []
    assert len(_violations(project)) == 1      # ...and it is still reported until then


def test_an_order_the_user_marked_done_is_not_reported_by_the_sweep_for_ever(
        started, project):
    """`jarvis wo done` is a decision, and the sweep has to read it as one.

    `mark_done` is the one landing that records instead of refusing: the user closing an
    order over a pull request that will never merge is the documented exit, and it writes
    `work_unlanded` with `closed_by: marked_done`. But the sweep only ever excused
    `abandoned` — so the order it just closed came straight back, flagged `stranded`,
    with a remedy telling it to run `jarvis wo finish --abandon` on a work order that is
    already `completed`. Every hour. For ever. That is the hourly nag spec §3 is written
    against, and it gets the checker switched off within a day.

    The pairing is the order beside it, which was NOT closed and must still be named.
    """
    closed = _order(project, "will never merge", code="orphaned")
    still_open = _order(project, "launcher contract", code="launcher")
    _settle(project, still_open["id"])

    assert ops.mark_done(closed["id"])["status"] == "completed"
    # The evidence is on the record either way — that is what issue #232 asked for.
    assert json.loads(_events(project, closed["id"], "work_unlanded")[-1]["payload"]
                      )["closed_by"] == "marked_done"

    found = _violations(project)

    assert [v.wo_id for v in found] == [still_open["id"]]
    assert _violations(project) == found      # and it stays silent on the next sweep too


def test_the_read_only_doctor_withholds_the_content_verdict_and_records_nothing(started,
                                                                               project):
    """What the plain `jarvis doctor` a human types actually does — which is LESS than
    what the daemon does, in two ways, and the docstrings now say so.

    `ops.run_doctor` passes `slow=True` with `repair=False`, so `check_project` hands the
    sweep a `_ReadOnly` proxy. `add_event` — the `landing_checked` cache write — is
    swallowed, so the run pays the full per-file git walk every time. And refreshing the
    default branch is a write to the repository, so this path does not do it and will not
    condemn a branch against a ref it could not bring up to date (issue #271): the
    coverage verdict is withheld as `unknown` and only the daemon's repairing sweep,
    which refreshed, reports it.

    Both left that way on purpose rather than worked around. The pairing is what stops
    that from being a check nobody runs: `merged-tail` reads no base at all, so Mode C —
    a branch carrying commits after the sha that merged — is still reported here.
    """
    stranded = _order(project, "launcher contract", code="launcher")
    clean = _order(project, "answered it")
    tailed = _order(project, "the cap", code="cap")
    _settle(project, stranded["id"], clean["id"])
    ops.finish(tailed["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    try:
        ops.complete_merged(store, store.get_work_order(tailed["id"]),
                            merged_at="2026-08-02T10:00:00Z",
                            head_oid=_git(tailed["worktree_path"],
                                          "rev-parse", "HEAD").strip())
    finally:
        store.close()
    (tailed["worktree_path"] / "onboarding.py").write_text(_feature("onboarding"))
    _git(tailed["worktree_path"], "add", "-A")
    _git(tailed["worktree_path"], "commit", "-qm", "the tail nobody merged")

    store = ProjectStore(project)
    try:
        found = [v for v in invariants.check_project(store, repair=False, slow=True)
                 if v.invariant == "INV-WORK-LANDED"]
    finally:
        store.close()

    # The rung that needs a current default branch is withheld; the one that needs none
    # answers as it always did.
    assert [v.wo_id for v in found] == [tailed["id"]]
    assert found[0].context["rung"] == "merged-tail"
    # Nothing was written, for any verdict — the settled one included.
    assert _events(project, clean["id"], "landing_checked") == []
    assert _events(project, stranded["id"], "landing_checked") == []
    # The repairing path refreshes the ref, so it both fills the cache and says what the
    # read-only run would not.
    assert {v.wo_id for v in _violations(project)} == {stranded["id"], tailed["id"]}
    assert len(_events(project, clean["id"], "landing_checked")) == 1


def test_a_settled_verdict_is_cached_and_an_unsettled_one_is_re_derived(started, project):
    """A landed order must not pay for git on every sweep; a stranded one must, or the
    check would go on complaining after the user merged it."""
    stranded = _order(project, "stranded", code="strand")
    clean = _order(project, "no code")
    _settle(project, stranded["id"], clean["id"])

    assert len(_violations(project)) == 1
    # `not-produced` is settled, so it was recorded once and is not looked at again.
    assert len(_events(project, clean["id"], "landing_checked")) == 1
    assert _violations(project) and len(_events(project, clean["id"],
                                                "landing_checked")) == 1
    # ...while the stranded one carries no cached verdict at all and is re-derived.
    assert _events(project, stranded["id"], "landing_checked") == []
