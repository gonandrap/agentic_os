"""A pull request merges itself only if the panel read the commit that merges.

docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md.

Three properties are worth more than the rest and each has its own section below:

* **`decide` is a table, and it is tested as one.** Every condition is exercised in both
  directions, without a network and without a daemon, because the safety rule lives in
  that function and a rule only tested through the loop that calls it is a rule tested
  once.
* **A push after acceptance invalidates the acceptance.** `Daemon.heal_pull_request`
  exists to make workers push to parked branches, so this is an ordinary Tuesday here
  rather than an edge case — and GitHub does NOT disarm its own auto-merge on a push from
  someone with write permission, which is why the OS cannot delegate this.
* **It ships off, per project.** A project that has not opted in is never merged by the
  OS, whatever the fleet says.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from jarvis import automerge, db, gate_rules, gates, ops
from jarvis.catalog import ValidationConfig, load_catalog
from jarvis.daemon import Daemon
from jarvis.github import PullRequest
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"
JUDGED = "a1b2c3d4e5f6000000000000000000000000aaaa"
PUSHED = "e4f5a6b7c8d9000000000000000000000000bbbb"


def check(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


GREEN = [check("unit (3.13)"), check("evals")]


def pr(**over) -> PullRequest:
    """An open, green, CLEAN pull request at the commit the panel judged."""
    return PullRequest(**{"state": "OPEN", "mergeable": "MERGEABLE",
                          "merge_state": "CLEAN", "base_ref": "main",
                          "checks": tuple(GREEN), "head_oid": JUDGED, **over})


def rnd(**over) -> dict:
    return {"id": 1, "round": 2, "outcome": "passed", "head_sha": JUDGED, **over}


def cfg(**over) -> ValidationConfig:
    return ValidationConfig(enabled=True, auto_merge=True, **over)


WO = {"id": "wo-1", "status": "waiting_pr_merge", "pr_url": PR, "title": "t"}


def decide(round_row, wo=WO, pull=None, config=None, **kw):
    """`automerge.decide` with `validated_head` taken from the SAME row, as the poll does.

    The predicate is passed in — that is the point of the signature — and it comes from
    `ProjectStore.validated_head` itself rather than a second copy of the rule written
    here, so a change to the rule that this file did not follow fails these rows too.
    """
    return automerge.decide(round_row, wo, pull if pull is not None else pr(),
                            config if config is not None else cfg(),
                            validated_head=kw.pop("validated_head",
                                                  ProjectStore.validated_head(round_row)),
                            **kw)


# -- `decide`: the condition table, both directions -----------------------------------


def test_the_panel_passed_the_commit_at_the_head_and_ci_is_green():
    decision = decide(rnd())
    assert decision.armed
    assert decision.judged_sha == JUDGED and decision.round_n == 2


@pytest.mark.parametrize("kw, code", [
    ({"enabled": False}, automerge.HELD_DISABLED),
    ({"auto_merge": False}, automerge.HELD_DISABLED),
])
def test_neither_half_of_the_switch_arms_it_alone(kw, code):
    """`auto_merge` is read BESIDE `enabled`, never instead of it: the acceptance this
    rides on is the panel's, so turning the panel off must stop the merges with it."""
    assert decide(rnd(), config=ValidationConfig(
        **{"enabled": True, "auto_merge": True, **kw})).code == code


@pytest.mark.parametrize("status", ["needs_review", "running", "completed",
                                    "validating", "cancelled"])
def test_only_a_work_order_parked_behind_its_pull_request_merges(status):
    """`needs_review` is the one worth naming: it means a human owes a decision — a panel
    escalation, a closed pull request, a pending assumption — and the machine does not
    merge over a human's outstanding decision."""
    decision = decide(rnd(), wo={**WO, "status": status})
    assert not decision.armed and decision.code == automerge.HELD_STATUS


def test_an_assumption_still_waiting_for_the_user_holds_the_merge():
    """Redundant with the status check via `ops.land_when_cleared`, and re-checked
    because the redundancy is the point — two-gates-not-a-chain."""
    decision = decide(rnd(), pending_assumptions=True)
    assert not decision.armed and decision.code == automerge.HELD_ASSUMPTIONS


@pytest.mark.parametrize("outcome", ["pending", "failed", "rejected", "escalated"])
def test_only_a_passed_round_arms_it_and_a_panel_that_never_answered_does_not(outcome):
    """Issue #235: the panel has died silently on a Claude usage limit, leaving the round
    `pending` or `failed`. The predicate is "the latest round PASSED" and not "no round
    was rejected" precisely so that a review nobody completed reads as not validated."""
    decision = decide(rnd(outcome=outcome))
    assert not decision.armed and decision.code == automerge.HELD_NOT_PASSED


def test_a_work_order_the_panel_never_judged_is_not_merged():
    decision = decide(None)
    assert not decision.armed and decision.code == automerge.HELD_NOT_PASSED


def test_a_round_that_recorded_no_commit_never_reads_as_matching():
    """`''` is the fail-closed value: a worktree packet, or a round written before the
    column existed. "Not recorded" must never read as "matches"."""
    decision = decide(rnd(head_sha=""))
    assert not decision.armed and decision.code == automerge.HELD_SHA_UNRECORDED


def test_a_head_that_moved_after_the_pass_holds_the_merge_and_says_both_commits():
    """THE CASE THE WHOLE DESIGN EXISTS FOR. The reason line names both commits because
    it is what the user reads to decide whether to merge it themselves."""
    decision = decide(rnd(), pull=pr(head_oid=PUSHED))
    assert not decision.armed and decision.code == automerge.HELD_SHA_MOVED
    assert JUDGED[:10] in decision.reason and PUSHED[:10] in decision.reason


def test_a_pull_request_with_no_head_oid_at_all_is_not_a_match():
    """GitHub not answering leaves `""`, and `""` is not equal to anything."""
    assert not decide(rnd(), pull=pr(head_oid="")).armed


@pytest.mark.parametrize("kw,code", [
    ({"state": "CLOSED"}, automerge.HELD_PR_CLOSED),
    ({"state": "MERGED"}, automerge.HELD_PR_CLOSED),
    ({"mergeable": "CONFLICTING"}, automerge.HELD_NOT_MERGEABLE),
    ({"mergeable": None}, automerge.HELD_NOT_MERGEABLE),   # not computed yet
    ({"merge_state": "UNKNOWN"}, automerge.HELD_MERGE_STATE_UNCLEAN),
    ({"merge_state": "BLOCKED"}, automerge.HELD_MERGE_STATE_UNCLEAN),   # a requirement out
    ({"merge_state": "UNSTABLE"}, automerge.HELD_MERGE_STATE_UNCLEAN),  # non-required red
    ({"merge_state": "BEHIND"}, automerge.HELD_MERGE_STATE_UNCLEAN),
    ({"checks": ()}, automerge.HELD_CHECKS_NOT_GREEN),     # no checks is not green
    ({"checks": (check("unit", "FAILURE"),)}, automerge.HELD_CHECKS_NOT_GREEN),
    ({"checks": (check("unit", "", "IN_PROGRESS"),)},      # queued is not passed
     automerge.HELD_CHECKS_NOT_GREEN),
    ({"checks": (check("unit"), check("evals", "", "QUEUED"))},
     automerge.HELD_CHECKS_NOT_GREEN),
])
def test_github_must_positively_say_open_mergeable_green_and_clean(kw, code):
    """Each condition holds, and each holds under ITS OWN code (issue #263).

    The code is what `Daemon._note_automerge_held` dedupes on, so one code shared by
    several conditions means the second condition to hold a commit is filed as a repeat
    of the first and never reaches the user.
    """
    decision = decide(rnd(), pull=pr(**kw))
    assert not decision.armed and decision.code == code


def test_no_two_conditions_share_a_hold_code():
    """The property behind the row above, asserted where a new condition would break it:
    `decide`'s tokens are distinct, so the dedupe key can tell any two of them apart."""
    codes = [v for k, v in vars(automerge).items() if k.startswith("HELD_")]
    assert len(codes) == len(set(codes))


def test_decide_takes_the_predicate_and_re_derives_none_of_it():
    """`validated_head` IS the rule; `round_row` only supplies the wording.

    Asserted the only way that cannot pass by accident: hand it a round that says
    `passed` on the commit at the head — everything `decide` would need to conclude
    "merge it" if it looked — while the predicate says no. If `decide` re-derived the
    rule from the row, this arms. It must hold instead, which is what makes
    `ProjectStore.validated_head` the single home rather than a second copy that rots.
    """
    decision = decide(rnd(), validated_head=None)
    assert not decision.armed and decision.code == automerge.HELD_SHA_UNRECORDED
    # ...and the other direction: the predicate alone is enough to arm it.
    assert decide(rnd(outcome="passed", head_sha="ignored"),
                  validated_head=JUDGED).armed


def test_the_store_and_the_decision_agree_about_what_validated_means(started, project):
    """The two halves of that split, pinned against each other on a real store, so the
    helper above cannot drift into testing a rule the OS does not run."""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "ship it")
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")

    def head():
        return store.validated_head(store.latest_validation_round(wo_id=wo["id"]))

    assert head() is None                                  # pending
    store.set_validation_head(row["id"], JUDGED)
    assert head() is None                                  # still pending
    store.close_validation_round(row["id"], "rejected", "")
    assert head() is None                                  # judged, and refused
    store.close_validation_round(row["id"], "passed", "")
    assert head() == JUDGED

    # A LATER round supersedes it, whatever the earlier one said.
    later = store.open_validation_round(wo_id=wo["id"], fingerprint="fp2")
    assert head() is None
    store.set_validation_head(later["id"], "")
    store.close_validation_round(later["id"], "passed", "")
    assert head() is None                                  # passed, nothing recorded
    store.close()


def test_the_predicate_is_read_off_the_row_it_was_handed_and_never_re_fetched():
    """THE RACE. The validation panel runs on another thread and opens rounds while this
    poll is reading, so a predicate that fetched the round FOR ITSELF could be answering
    about a different round from the one supplying the wording. The pair that produces —
    a `passed` row beside "no accepted head" — is exactly `HELD_SHA_UNRECORDED`, whose
    hold is deduped for ever: the user would keep being told that a round which in fact
    passed on a known commit "read a worktree".

    Pinned by construction rather than by threading: the function is static and takes the
    row, so there is no store for it to re-read from.
    """
    import inspect

    assert isinstance(
        inspect.getattr_static(ProjectStore, "validated_head"), staticmethod)
    assert list(inspect.signature(ProjectStore.validated_head).parameters) == ["round_row"]
    assert ProjectStore.validated_head({"outcome": "passed", "head_sha": JUDGED}) == JUDGED
    assert ProjectStore.validated_head({"outcome": "pending", "head_sha": JUDGED}) is None
    assert ProjectStore.validated_head(None) is None


def test_decide_touches_no_store_no_clock_and_no_network():
    """Pure by construction, asserted by the shape of the module rather than by trust:
    the whole condition table is unit-testable without a network, which is why every row
    above is one line."""
    source = ast.parse(Path(automerge.__file__).read_text())
    fn = next(n for n in ast.walk(source)
              if isinstance(n, ast.FunctionDef) and n.name == "decide")
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"execute", "add_event", "run", "now", "usable_grant"}


# -- the write verb is declared and minimal -------------------------------------------


def test_this_module_builds_no_gh_command_but_the_one_it_declares():
    """`github.READ_ONLY_VERBS`' mechanism, pointed at the one module allowed to write.

    A second write verb cannot arrive here without a commit that also edits this test,
    which is the whole point — and the reason the write lives here rather than in
    `github.py`, whose claim that everything in it is a question is what the panel's
    blind review rests on.
    """
    tree = ast.parse(Path(automerge.__file__).read_text())
    verbs = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        words = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
        if all(isinstance(w, str) for w in words[:2]):
            verbs.add((words[0], words[1]))
    assert verbs, "found no gh argument lists at all — has the module been rewritten?"
    assert verbs <= set(automerge.WRITE_VERBS), sorted(verbs - set(automerge.WRITE_VERBS))


def test_github_itself_stays_read_only():
    """The other half of the containment: adding a head sha to `PR_FIELDS` must not have
    smuggled a write verb into the module that may not have one."""
    from jarvis import github

    assert ("pr", "merge") not in github.READ_ONLY_VERBS


def test_the_merge_is_pinned_to_the_judged_commit():
    """`--match-head-commit` is the API's `sha` parameter — GitHub refuses the merge
    server-side if the head has moved. The comparison in `decide` exists so the OS can
    SAY why it is holding; this is what makes it true."""
    command = automerge.merge_command(PR, JUDGED)
    assert f"--match-head-commit {JUDGED}" in command
    assert "--squash" in command


def test_the_merge_deletes_no_branch_anywhere():
    """ISSUE #253's CAUSE, refused at source, and spec §10.1's answer reversed.

    `--delete-branch` deletes the remote branch and then the LOCAL one, and the local
    delete fails whenever a worktree still has that branch checked out — which on this
    path is always, since nothing removes a worker's worktree and the work order
    completes only after the merge. So the flag made every OS merge a command that
    half-failed. Deleting a developer's local branches was never this mechanism's
    business, and the remote half belongs to the repository's own setting.
    """
    assert "--delete-branch" not in automerge.merge_command(PR, JUDGED)


# -- the gate: a kind nothing classifies into, sharing no authority with `pr_merge` ----


def test_nothing_a_worker_can_type_classifies_as_an_automatic_merge():
    """Filed programmatically only, exactly as `self_heal` is. A recogniser for this kind
    would mean a worker could trip it, and the kind is the OS's own authority."""
    rules = gate_rules.RuleSet.from_seeds()
    config = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))
    for command in (automerge.merge_command(PR, JUDGED),
                    f"gh pr merge {PR} --squash",
                    "gh pr merge 31 --merge"):
        action = gates.classify(command, config, rules)
        assert action is None or action.kind != gates.AUTO_MERGE, command


def test_an_automatic_merge_grant_cannot_clear_a_workers_own_merge(started, project):
    """The two kinds share a command SPELLING and share no authority: a grant is scoped
    to (work order, kind, exact command). This is why it is a separate kind rather than
    a reuse of `pr_merge`."""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "ship it")
    command = automerge.merge_command(PR, JUDGED)
    approval = store.add_approval(wo["id"], gates.AUTO_MERGE, command)
    store.decide_approval(approval["id"], verdict="approved", reason="ok",
                          decided_by="neo")

    assert store.usable_grant(wo["id"], gates.AUTO_MERGE, command) is not None
    # ...and the same string under the kind a worker's attempt classifies into: nothing.
    assert store.usable_grant(wo["id"], "pr_merge", command) is None
    store.close()


def test_an_automatic_merge_verdict_never_messages_the_worker(started, project):
    """The worker finished long ago and is parked behind its pull request. A queued
    message would start a turn on a work order nobody asked to reopen — the precise act
    `self_heal` is fenced against, one level along."""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "ship it")
    approval = store.add_approval(wo["id"], gates.AUTO_MERGE,
                                  automerge.merge_command(PR, JUDGED))

    gates.apply_decision(store, approval["id"], "approved", "go ahead", "neo",
                         project="proj_a")

    assert store.queued_messages(wo["id"]) == []
    assert [e["kind"] for e in store.events_of_kind(wo["id"], "automerge_decided")]
    store.close()


def test_a_denial_leaves_the_pull_request_open_and_tells_nobody_off(started, project):
    """A refused automatic merge is not a problem: the user merges it by hand, which is
    what they did for every pull request before this existed. No flag, no status move."""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "ship it")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    approval = store.add_approval(wo["id"], gates.AUTO_MERGE,
                                  automerge.merge_command(PR, JUDGED))

    gates.apply_decision(store, approval["id"], "denied", "not yet", "neo",
                         project="proj_a")

    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge" and not row["attention_reason"]
    assert store.queued_messages(wo["id"]) == []
    store.close()


# -- `apply`: what refuses, and what one grant buys ------------------------------------


@pytest.fixture()
def granted(started, project):
    """A work order parked behind a pull request with an approved merge grant."""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "ship it")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    approval = store.add_approval(wo["id"], gates.AUTO_MERGE,
                                  automerge.merge_command(PR, JUDGED),
                                  max_uses=automerge.GRANT_USES)
    store.decide_approval(approval["id"], verdict="approved", reason="ok",
                          decided_by="neo")
    yield store, store.get_work_order(wo["id"]), store.get_approval(approval["id"])
    store.close()


def test_a_dismissal_is_not_permission_to_merge(started, project):
    """THE HOLE THIS GUARD CLOSES. `usable_grant` clears a command on TWO statuses and
    only one is an authorisation: `dismissed` means the recogniser matched something that
    performs no privileged action. Nothing classifies into this kind, so a dismissal here
    can only be a reviewer's slip — and taking it as permission would turn the one verdict
    that means "this was never privileged" into the thing that merges to `main`."""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "ship it")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    command = automerge.merge_command(PR, JUDGED)
    approval = store.add_approval(wo["id"], gates.AUTO_MERGE, command)
    store.decide_approval(approval["id"], verdict="dismissed", reason="not privileged",
                          decided_by="neo")

    # The dismissal DOES clear the command generally — that is the documented behaviour.
    assert store.usable_grant(wo["id"], gates.AUTO_MERGE, command) is not None
    with pytest.raises(automerge.AutoMergeRefused, match="not approved"):
        automerge.apply(store, store.get_work_order(wo["id"]), JUDGED,
                        store.get_approval(approval["id"]))
    store.close()


def test_no_grant_at_all_merges_nothing(granted):
    store, wo, _ = granted
    with pytest.raises(automerge.AutoMergeRefused, match="no approved gate request"):
        automerge.apply(store, wo, JUDGED, None)


def test_a_grant_of_another_kind_merges_nothing(granted):
    store, wo, approval = granted
    with pytest.raises(automerge.AutoMergeRefused, match="not a auto_merge"):
        automerge.apply(store, wo, JUDGED, {**approval, "kind": "pr_merge"})


def test_a_verdict_with_no_judged_commit_merges_nothing(granted):
    """`''` reaches here only through a bug, and it must refuse rather than run a merge
    with an empty `--match-head-commit`, which GitHub would read as no constraint."""
    store, wo, approval = granted
    with pytest.raises(automerge.AutoMergeRefused, match="no judged commit"):
        automerge.apply(store, wo, "", approval)


def test_one_grant_buys_one_merge(granted, fake_gh):
    """A grant is a receipt for landing ONE commit, not a budget. A retry after a failure
    needs a fresh review rather than a free second go at an irreversible act."""
    store, wo, approval = granted
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)

    automerge.apply(store, wo, JUDGED, approval)

    with pytest.raises(automerge.AutoMergeRefused, match="no longer a live grant"):
        automerge.apply(store, wo, JUDGED, store.get_approval(approval["id"]))


def test_github_refuses_the_merge_when_the_head_moved_under_it(granted, fake_gh):
    """The millisecond window between the poll's view and the merge, closed at the SERVER.
    The fake refuses on `--match-head-commit` exactly as GitHub does, so this is testing
    the flag rather than testing the fake's willingness to merge."""
    store, wo, approval = granted
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=PUSHED)

    with pytest.raises(automerge.AutoMergeRefused, match="refused the merge"):
        automerge.apply(store, wo, JUDGED, approval)


def test_a_merge_that_landed_is_not_a_failure_however_gh_exited(granted, fake_gh):
    """**ISSUE #253, AT THE SEAM THAT BROKE.** `gh` exits non-zero AND the merge landed —
    that exact combination, which no test drove before and both live merges of 0.10.0
    produced. The command merges remotely and then tidies up locally; one exit status
    reports two acts, and the local half failed on a branch a worktree still held.

    The outcome is the PULL REQUEST's, not the exit code's. `apply` returns rather than
    raising, and the failure text comes back as a cleanup note for the timeline.
    """
    store, wo, approval = granted
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    fake_gh.fail_merge_cleanup(
        "failed to delete local branch worktree-wo-1: cannot delete branch "
        "'worktree-wo-1' used by worktree at '/repo/.claude/worktrees/wo-1'")

    merged = automerge.apply(store, wo, JUDGED, approval)

    assert merged["head_sha"] == JUDGED
    assert merged["after_merge_cause"] == automerge.CLEANUP
    assert "cannot delete branch" in merged["after_merge_error"]


def test_a_merge_that_timed_out_is_not_called_a_cleanup_failure(granted, fake_gh):
    """THE OTHER LANDED SHAPE, and the reason the cause is carried rather than assumed.

    The command never FINISHED — GitHub computes the squash on the way, which is why
    `MERGE_TIMEOUT` is longer than `github.GH_TIMEOUT` in the first place — and it may
    well have merged before the timeout expired. That is still a landed merge, and it is
    still not a cleanup failure: no branch deletion was attempted, so a record saying
    one failed sends the reader hunting something that never happened. With
    `--delete-branch` gone this is now the likelier of the two.
    """
    store, wo, approval = granted
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    fake_gh.hang_merge_after_landing()

    merged = automerge.apply(store, wo, JUDGED, approval)

    assert merged["head_sha"] == JUDGED
    assert merged["after_merge_cause"] == automerge.UNFINISHED
    assert "did not complete" in merged["after_merge_error"]
    assert "cleanup" not in merged["after_merge_error"]


def test_a_merge_github_really_refused_is_still_a_failure(granted, fake_gh):
    """The other direction of the same read, and the reason it is a read rather than a
    shrug: the likeliest real failure is a `gh` with no write scope. The pull request is
    still OPEN afterwards, so this must raise exactly as it always did."""
    store, wo, approval = granted
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    fake_gh.fail_merge("HTTP 403: Resource not accessible by integration")

    with pytest.raises(automerge.MergeFailed, match="403"):
        automerge.apply(store, wo, JUDGED, approval)


def test_an_outcome_the_os_cannot_read_is_never_guessed_as_merged(granted, fake_gh):
    """`gh` failed twice and nothing knows what happened. Claiming the merge landed would
    complete a work order whose pull request is still open, which is the worse of the two
    errors — so it says the outcome is unknown, in those words, and the pull-request poll
    settles it either way within a tick."""
    store, wo, approval = granted
    fake_gh.fail_merge("HTTP 502: upstream timed out")   # ...and `pr view` finds no PR

    with pytest.raises(automerge.MergeFailed, match="unknown"):
        automerge.apply(store, wo, JUDGED, approval)


def test_a_pull_request_on_another_repository_never_becomes_an_argument(granted,
                                                                       fake_gh):
    """The URL is submitter-written and this is the one command in the OS that can change
    a repository, so it is checked before it becomes an argument — `github.checked_pr_url`
    is not skipped just because the poll already read the same column."""
    from jarvis import github

    store, wo, approval = granted
    with pytest.raises(github.UntrustedPullRequest):
        automerge.apply(store, {**wo, "pr_url": "--repo=someone/else"}, JUDGED, approval)
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


# -- through the daemon: the per-project switch, and the push that invalidates ---------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def arm(daemon, project_path, *, auto_merge: bool,
        judged: str = JUDGED, outcome: str = "passed"):
    """A work order parked behind a green pull request with a settled round on `judged`.

    Everything the six conditions need, so each test moves exactly one of them.
    """
    store = ProjectStore(project_path)
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store.update_work_order(wo["id"], session_id="sess-1")
    round_row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp1")
    store.set_validation_head(round_row["id"], judged)
    store.close_validation_round(round_row["id"], outcome, "")
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = auto_merge
    return store, store.get_work_order(wo["id"])


def poll(daemon, store):
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def test_a_project_that_has_not_opted_in_is_never_merged_by_the_os(
        started, project, fake_gh):
    """THE SWITCH IS REAL AND NOT DECORATIVE. Everything else lines up — the panel
    accepted this exact commit, CI is green, the pull request is CLEAN — and the only
    missing fact is the project's own permission. Nothing merges, nothing is proposed,
    and the work order stays where the user can merge it themselves."""
    store, wo = arm(started, project, auto_merge=False)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)

    poll(started, store)

    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert store.list_approvals(wo["id"]) == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    # ...and it did not even cost the read: the config guard is above the query.
    assert store.events_of_kind(wo["id"], "automerge_held") == []


def test_an_order_awaiting_a_person_is_not_told_the_merge_declined_it(
        started, project, fake_gh):
    """AN ORDER THE MECHANISM NEVER APPLIES TO MUST NOT BE ANNOTATED BY IT.

    `PR_POLL_STATUSES` is deliberately wider than `waiting_pr_merge` — a `needs_review`
    order behind a red build has to be polled, issue #224 — so every such order reaches
    this code. Letting it reach `decide` produced a `HELD_STATUS` hold, which
    `ops.automerge_state` renders on the work order as "auto-merge: held — the work order
    is needs_review": the user told that the automatic merge declined a pull request it
    was never a candidate for, on the one surface that exists to report what it did.

    Everything else here lines up, so only the status is holding it.
    """
    store, wo = arm(started, project, auto_merge=True)
    store.set_status(wo["id"], "needs_review")
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)

    poll(started, store)

    row = store.get_work_order(wo["id"])
    assert store.events_of_kind(wo["id"], "automerge_held") == []
    assert ops.automerge_state(store, row) is None
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert store.list_approvals(wo["id"]) == []
    # And `_note_automerge_held` refuses it from the other side, so a second caller
    # cannot reintroduce the line by skipping the guard in `Daemon.auto_merge`.
    started._note_automerge_held(
        store, wo["id"], automerge.decide(None, row, pr(), cfg(), validated_head=None))
    assert store.events_of_kind(wo["id"], "automerge_held") == []


def test_the_fleet_default_ships_off_and_a_project_inherits_it(catalog_file):
    """Per project, and opting in is an explicit act at either level — the field-level
    fallback every `inspect.alarm_*` threshold already uses."""
    catalog = load_catalog(catalog_file)
    assert catalog.os.validation.auto_merge is False
    assert catalog.project("proj_a").validation.auto_merge is False


def test_a_project_keeps_its_own_answer_over_the_fleets(tmp_path):
    """The precedence convention: a project that names a value keeps it, a project that
    does not falls back to the fleet answer."""
    import json

    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "os": {"validation": {"enabled": True, "auto_merge": True}},
        "projects": [
            {"name": "opted_out", "path": str(tmp_path),
             "validation": {"auto_merge": False}},
            {"name": "inherits", "path": str(tmp_path)},
        ]}))
    catalog = load_catalog(path)
    assert catalog.project("opted_out").validation.auto_merge is False
    assert catalog.project("inherits").validation.auto_merge is True


def test_a_project_opts_in_through_the_same_cli_as_every_other_setting(
        started, catalog_file, monkeypatch):
    """Not a constant and not an environment variable: the ordinary config console, on
    the ordinary per-project path, and — because `*.validation.*` already covers it — with
    the mandatory `--reason` and a recorded version that every safety key carries.

    The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID` and is refused
    a config write on purpose (`ops._refuse_worker_write`). This test is standing in for
    the USER at the console, so it drops the variable — the same thing
    `tests/test_config_console.py` does, and for the same reason.
    """
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    assert ops.safety_key("projects.proj_a.validation.auto_merge")
    with pytest.raises(ops.OpsError):
        ops.set_config("validation.auto_merge", True, project="proj_a",
                       catalog_path=str(catalog_file))          # no reason given

    out = ops.set_config("validation.auto_merge", True, project="proj_a",
                         reason="this project may merge its own validated PRs",
                         catalog_path=str(catalog_file))

    assert out["safety"] and out["path"] == "projects.proj_a.validation.auto_merge"
    assert load_catalog(catalog_file).project("proj_a").validation.auto_merge is True


def test_an_opted_in_project_asks_before_it_merges(started, project, fake_gh):
    """The merge is a mandatory gated action. Armed is not permitted: the first tick
    files the request and merges nothing."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)

    poll(started, store)

    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == [gates.AUTO_MERGE]
    assert JUDGED in approvals[0]["command"]
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_it_asks_once_and_not_every_two_minutes(started, project, fake_gh):
    """A parked pull request is polled for as long as it is open. Re-proposing would put
    a fresh Neo review of the same decision on the queue every tick."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)

    poll(started, store)
    poll(started, store)

    assert len(store.list_approvals(wo["id"])) == 1


def test_a_refused_request_is_not_re_asked_for_the_same_commit(started, project,
                                                               fake_gh):
    """A denial is an answer. Asking again would make the reviewer's verdict advisory."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    store.decide_approval(approval["id"], verdict="denied", reason="no",
                          decided_by="neo")

    poll(started, store)

    assert len(store.list_approvals(wo["id"])) == 1
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_once_approved_the_next_poll_merges_it_and_closes_the_work_order(
        started, project, fake_gh):
    """The verdict records; the POLL merges. That ordering is what re-verifies the head
    sha after the approval rather than before it — a grant lives an hour."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "the panel read it", "neo",
                         project="proj_a")

    poll(started, store)

    merges = [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert len(merges) == 1
    assert merges[0]["argv"][-2:] == ["--match-head-commit", JUDGED]
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_the_record_never_reads_as_though_a_person_merged_it(started, project, fake_gh):
    """`complete_merged`'s own rule, cutting the other way: the same `gh pr view` reports
    a merge by the OS and a merge by the user, so the one path that knows says so."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    poll(started, store)

    events = store.events_of_kind(wo["id"], "automerge_merged")
    assert len(events) == 1
    from jarvis import db
    payload = db.from_json(events[0]["payload"], {})
    assert payload["head_sha"] == JUDGED and payload["approval_id"] == approval["id"]
    assert ops.automerge_state(store, store.get_work_order(wo["id"]))["kind"] == \
        "automerge_merged"


def test_a_push_after_the_approval_stops_the_merge_dead(started, project, fake_gh):
    """**THE NON-NEGOTIABLE.** `Daemon.heal_pull_request` exists to make workers push to
    parked branches, and GitHub does not disarm its own auto-merge on a push from anyone
    with write permission — so a pass, a nudge, a new commit and a green build is an
    ordinary Tuesday here. The acceptance must not survive it.

    Belt AND braces, both asserted: the poll declines because the SHAs differ, and the
    merge command is never built at all.
    """
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    # The worker pushes. Same branch, same pull request, a commit no seat has read.
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=PUSHED)
    poll(started, store)

    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert len(held) == 1
    from jarvis import db
    payload = db.from_json(held[0]["payload"], {})
    assert payload["judged_sha"] == JUDGED and payload["head_sha"] == PUSHED


def test_a_held_merge_is_said_once_per_commit_and_never_flags_the_user(
        started, project, fake_gh):
    """A pull request is polled every couple of minutes for as long as it is open, and a
    heal-loop push is normal: the user's attention list is not a place to put normal."""
    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=PUSHED)

    poll(started, store)
    poll(started, store)
    poll(started, store)

    assert len(store.events_of_kind(wo["id"], "automerge_held")) == 1
    assert not store.get_work_order(wo["id"])["attention_reason"]


def test_the_hold_the_user_reads_is_the_one_blocking_the_merge_now(started, project,
                                                                   fake_gh):
    """ISSUE #263. A hold that CHANGES at one commit is news, and the user reads the
    newest one: CI was still running, then main moved and the branch stopped merging.
    Keyed on the code alone, the second hold was filed as a repeat of the first and the
    user was sent to look at a CI run that had since gone green."""
    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    fake_gh.set_pr(PR, "OPEN", head_oid=JUDGED, merge_state="CLEAN",
                   checks=[check("unit (3.13)", "", "IN_PROGRESS")])
    poll(started, store)
    assert "CI" in ops.automerge_state(store, store.get_work_order(wo["id"]))["line"]

    # Same commit, CI now green — and main has moved underneath the branch.
    fake_gh.set_pr(PR, "OPEN", head_oid=JUDGED, checks=GREEN,
                   mergeable="CONFLICTING", merge_state="DIRTY")
    poll(started, store)

    held = store.events_of_kind(wo["id"], "automerge_held")
    assert [db.from_json(e["payload"], {})["code"] for e in held] == [
        automerge.HELD_CHECKS_NOT_GREEN, automerge.HELD_NOT_MERGEABLE]
    line = ops.automerge_state(store, store.get_work_order(wo["id"]))["line"]
    assert "merges cleanly" in line and "CI" not in line
    # The repair still ran: the hold is a sentence about the pull request, not a claim
    # on it, and nothing was merged or proposed while the worker was being nudged.
    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_a_red_build_refreshes_the_hold_too(started, project, fake_gh):
    """THE FAILING-CHECKS TWIN, and it is driven rather than reasoned about.

    `poll_pull_requests` repairs on two branches and the poll body runs inside a bare
    `except Exception: log.exception(...)` — so a mistake on either recording call is
    swallowed in production and in this suite alike, and the defect being fixed here is
    exactly a branch that never wrote the record. One branch proven is not two.
    """
    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    fake_gh.set_pr(PR, "OPEN", head_oid=JUDGED, checks=GREEN, merge_state="BEHIND")
    poll(started, store)
    assert "BEHIND" in ops.automerge_state(store, store.get_work_order(wo["id"]))["line"]

    # Same commit, and now a check has gone red: `elif pr.failing:` owns this tick.
    fake_gh.set_pr(PR, "OPEN", head_oid=JUDGED, merge_state="BEHIND",
                   checks=[check("unit (3.13)", "FAILURE"), check("evals")])
    poll(started, store)

    held = store.events_of_kind(wo["id"], "automerge_held")
    assert [db.from_json(e["payload"], {})["code"] for e in held] == [
        automerge.HELD_MERGE_STATE_UNCLEAN, automerge.HELD_CHECKS_NOT_GREEN]
    line = ops.automerge_state(store, store.get_work_order(wo["id"]))["line"]
    assert "CI has not finished" in line and "BEHIND" not in line
    # The nudge went out and the merge did not: recording a hold claims nothing.
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


@pytest.mark.parametrize("pull", [
    pr(mergeable="CONFLICTING", merge_state="DIRTY"),
    pr(checks=(check("unit", "FAILURE"),)),
])
def test_a_pull_request_being_repaired_can_never_arm(pull):
    """What makes `record_only` safe, pinned where a change to either property breaks it:
    the two shapes `poll_pull_requests` repairs are shapes `decide` refuses. `conflicting`
    means `mergeable_now` is false and a failing check means `checks_green` is false, so
    the recording call cannot reach a grant even before the flag stops it."""
    assert not decide(rnd(), pull=pull).armed


def test_a_changed_wording_under_one_code_is_still_a_changed_hold(started, project,
                                                                  fake_gh):
    """The half a code cannot carry: one condition, two values, two different things for
    the user to do. `BEHIND` is a branch to update and `DIRTY` is a conflict to resolve,
    so the dedupe keys on the sentence as well as the token."""
    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    for state in ("BEHIND", "DIRTY", "BEHIND"):        # and back: already said, so no row
        fake_gh.set_pr(PR, "OPEN", head_oid=JUDGED, checks=GREEN, merge_state=state)
        poll(started, store)

    held = store.events_of_kind(wo["id"], "automerge_held")
    assert [db.from_json(e["payload"], {})["reason"] for e in held] == [
        "GitHub reports the merge state as BEHIND, not CLEAN",
        "GitHub reports the merge state as DIRTY, not CLEAN"]


def test_a_worktree_round_binds_nothing_and_so_merges_nothing(started, project,
                                                              fake_gh):
    """A round judged against a local diff — or one whose PR fetch failed and fell back —
    has no commit behind its verdict. `''` never reads as "matches"."""
    store, wo = arm(started, project, auto_merge=True, judged="")
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)

    poll(started, store)

    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_a_panel_that_never_answered_does_not_merge(started, project, fake_gh):
    """Issue #235 through the loop, not only through `decide`: a round left `pending` by
    a panel that died on a usage limit must read as not validated."""
    store, wo = arm(started, project, auto_merge=True, outcome="pending")
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)

    poll(started, store)

    assert store.list_approvals(wo["id"]) == []


def test_an_approved_merge_is_attempted_once_and_the_failure_is_told_once(
        started, project, fake_gh):
    """ONE APPROVAL BUYS ONE ATTEMPT, and the refusal is said out loud exactly once.

    The likeliest first failure of this whole feature is the daemon's `gh` having read
    credentials but no write scope, which no amount of retrying fixes. What bounds the
    retries is `GRANT_USES = 1`, not a counter: every later poll refuses inside `apply`
    before reaching GitHub, and those refusals are neither recorded nor counted — writing
    them as `automerge_failed` made the user's inbox row quote "the grant is spent" as
    the reason the merge failed instead of the 403 that actually caused it.

    The work order is not moved and not flagged: hand-merging still works.
    """
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")
    fake_gh.fail_merge("HTTP 403: Resource not accessible by integration")

    for _ in range(5):
        poll(started, store)

    # EXACTLY once, not "at most once": this assertion passed vacuously at zero while the
    # fixture was failing `gh pr view` as well, so the merge was never attempted.
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]) == 1
    assert len(store.events_of_kind(wo["id"], "automerge_failed")) == 1
    assert "403" in db.from_json(
        store.events_of_kind(wo["id"], "automerge_failed")[0]["payload"], {})["reason"]
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge" and not row["attention_reason"]
    # One inbox row, quoting what GitHub said and not what the spent grant said.
    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:
        told = [i for i in central.unacked_inbox()
                if (i["wo_id"] or "") == wo["id"]]
    finally:
        central.close()
    assert len(told) == 1
    assert "403" in told[0]["body"] and "grant" not in told[0]["body"]


def test_a_landed_merge_is_never_reported_as_failed(started, project, fake_gh):
    """**ISSUE #253 THROUGH THE WHOLE LOOP.** GitHub accepted the merge and `gh` exited
    non-zero over the local tidy-up after it. Before this, the OS told the user "GitHub
    refused the merge: failed to delete local branch …" — false twice over — wrote
    `automerge_failed` and an inbox row about a merge that had landed, and spent the one
    attempt `GRANT_USES` allows on an operation that had succeeded. The work orders were
    rescued only by the separate pull-request poll, which made this mechanism correct by
    accident: remove that poll and the order parks for ever behind a merge that landed.

    The four things the record must now say, and the existing suite asserted none of
    them because nothing drove this combination.
    """
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)
    gates.apply_decision(store, store.list_approvals(wo["id"])[0]["id"], "approved",
                         "ok", "neo", project="proj_a")
    fake_gh.fail_merge_cleanup(
        "failed to delete local branch worktree-wo-1: failed to run git: error: cannot "
        "delete branch 'worktree-wo-1' used by worktree at '/repo/.claude/worktrees'")

    poll(started, store)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "completed"
    assert store.events_of_kind(wo["id"], "automerge_failed") == []
    assert ops.automerge_state(store, row)["kind"] == "automerge_merged"
    # The cleanup IS on the record — a warning, and nothing that needs a person.
    cleanup = store.events_of_kind(wo["id"], "automerge_cleanup_failed")
    assert len(cleanup) == 1
    assert "cannot delete branch" in db.from_json(cleanup[0]["payload"], {})["reason"]
    assert not row["attention_reason"]
    # ...and the OTHER landed shape is not claimed alongside it.
    assert store.events_of_kind(wo["id"], "automerge_command_unfinished") == []

    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:
        told = [i for i in central.unacked_inbox() if (i["wo_id"] or "") == wo["id"]]
    finally:
        central.close()
    assert told == [], [i["title"] for i in told]


def test_a_timed_out_merge_that_landed_says_so_and_does_not_blame_the_cleanup(
        started, project, fake_gh):
    """Same four guarantees as the test above, through the whole poll, for the OTHER
    landed shape — and one more: the record must not describe a timeout as a tidy-up
    that failed. Nothing drove this path when the cause was assumed rather than carried,
    which is how a timed-out merge came to read as a branch deletion nobody attempted."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)
    gates.apply_decision(store, store.list_approvals(wo["id"])[0]["id"], "approved",
                         "ok", "neo", project="proj_a")
    fake_gh.hang_merge_after_landing()

    poll(started, store)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "completed"
    assert store.events_of_kind(wo["id"], "automerge_failed") == []
    assert store.events_of_kind(wo["id"], "automerge_cleanup_failed") == []
    assert ops.automerge_state(store, row)["kind"] == "automerge_merged"
    unfinished = store.events_of_kind(wo["id"], "automerge_command_unfinished")
    assert len(unfinished) == 1
    assert "did not complete" in db.from_json(unfinished[0]["payload"], {})["reason"]
    assert not row["attention_reason"]

    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:
        told = [i for i in central.unacked_inbox() if (i["wo_id"] or "") == wo["id"]]
    finally:
        central.close()
    assert told == [], [i["title"] for i in told]


def test_an_approval_does_not_go_on_claiming_a_merge_that_will_never_happen(
        started, project, fake_gh):
    """THE DEFECT A FIXED-ORDER SCAN HID. `gates.apply_decision` writes
    `automerge_decided` on every verdict, so reading the kinds in a fixed finality order
    let an approval outrank every later event for ever: the line went on saying
    "approved by neo" about a pull request whose head had since moved and which was
    therefore never going to merge. That is the exact case the line exists for."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "the panel read it", "neo",
                         project="proj_a")
    assert ops.automerge_state(store, wo)["kind"] == "automerge_decided"

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=PUSHED)
    poll(started, store)

    state = ops.automerge_state(store, store.get_work_order(wo["id"]))
    assert state["kind"] == "automerge_held"
    assert PUSHED[:10] in state["line"] and "approved" not in state["line"]


def test_an_approval_does_not_outrank_the_merge_that_kept_failing(started, project,
                                                                  fake_gh):
    """The other half of the same defect: three failed attempts under an approval must
    not read as "approved by neo", or the one surface that could say the merge is stuck
    says the opposite."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")
    fake_gh.fail_merge("HTTP 403: Resource not accessible by integration")

    poll(started, store)

    state = ops.automerge_state(store, store.get_work_order(wo["id"]))
    assert state["kind"] == "automerge_failed"
    assert "403" in state["line"]


def test_a_merge_is_terminal_and_outranks_anything_stamped_later(started, project,
                                                                 fake_gh):
    """The one exception to newest-wins, asserted rather than assumed: nothing follows a
    merge, and a completed work order must never render as held."""
    store, wo = arm(started, project, auto_merge=True)
    store.add_event(wo["id"], "automerge_merged", {
        "approval_id": 1, "round_id": 1, "round": 1, "head_sha": JUDGED})
    store.add_event(wo["id"], "automerge_held", {
        "code": "sha_moved", "reason": "later, and wrong", "judged_sha": JUDGED,
        "head_sha": PUSHED, "round": 1})

    state = ops.automerge_state(store, wo)

    assert state["kind"] == "automerge_merged" and "merged by the OS" in state["line"]


def test_a_pending_request_is_not_re_proposed_every_tick(started, project, fake_gh):
    """A request with Neo, or escalated to the user, is somebody's business and not this
    loop's. Re-proposing opened a NeoStore every two minutes for as long as the pull
    request stayed open, to discover each time that `propose` would refuse."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)
    assert store.list_approvals(wo["id"])[0]["status"] == "pending"

    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)
    poll(started, store)
    store.conn.set_trace_callback(None)

    assert len(store.list_approvals(wo["id"])) == 1
    # The tick reads the approval and stops there: nothing is written, and no second
    # request, question or link is created.
    assert [s for s in sql if not s.lstrip().upper().startswith("SELECT")] == []


def test_the_work_order_says_why_it_did_not_merge_itself(started, project, fake_gh,
                                                          capsys):
    """A hold the user cannot see is a hold that reads as the OS doing nothing.

    Both surfaces, from one recorded fact: the line a person gets, and the payload
    `--json` keeps for anything that wants the SHAs. The commit is on the round line too,
    so "which diff was accepted" is answerable without a second command.
    """
    from jarvis import cli

    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=PUSHED)
    poll(started, store)

    assert cli.main(["wo", "show", wo["id"]]) == 0
    human = capsys.readouterr().out
    assert "auto_merge" in human and JUDGED[:10] in human and PUSHED[:10] in human
    assert f"commit {JUDGED[:10]}" in human            # on the round line as well

    assert cli.main(["wo", "show", wo["id"], "--json"]) == 0
    import json as _json
    document = _json.loads(capsys.readouterr().out)
    assert document["auto_merge"]["judged_sha"] == JUDGED
    assert document["auto_merge"]["head_sha"] == PUSHED
    assert document["validation_rounds"][0]["head_sha"] == JUDGED


def test_a_work_order_the_mechanism_never_touched_says_nothing_at_all(started, project,
                                                                      fake_gh, capsys):
    """Every work order in a fleet that has not opted in. A line reading "auto-merge:
    off" on all of them would spend attention on the absence of a feature."""
    from jarvis import cli

    store, wo = arm(started, project, auto_merge=False)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    poll(started, store)

    assert cli.main(["wo", "show", wo["id"], "--json"]) == 0
    import json as _json
    assert "auto_merge" not in _json.loads(capsys.readouterr().out)


def test_a_user_merging_by_hand_is_unaffected_and_still_completes_the_order(
        started, project, fake_gh):
    """Requirement 2, end to end. The mechanism gates the MACHINE; the person merges any
    pull request at any moment and the existing poll notices, exactly as it always did."""
    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=PUSHED)
    poll(started, store)                       # held: the head moved
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-14T10:00:00Z", head_oid=PUSHED)

    poll(started, store)

    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert store.events_of_kind(wo["id"], "automerge_merged") == []
