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

from jarvis import automerge, gate_rules, gates, ops
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
                          "checks": tuple(GREEN), "head_sha": JUDGED, **over})


def rnd(**over) -> dict:
    return {"id": 1, "round": 2, "outcome": "passed", "head_sha": JUDGED, **over}


def cfg(**over) -> ValidationConfig:
    return ValidationConfig(enabled=True, auto_merge=True, **over)


WO = {"id": "wo-1", "status": "waiting_pr_merge", "pr_url": PR, "title": "t"}


# -- `decide`: the condition table, both directions -----------------------------------


def test_the_panel_passed_the_commit_at_the_head_and_ci_is_green():
    decision = automerge.decide(rnd(), WO, pr(), cfg())
    assert decision.armed
    assert decision.judged_sha == JUDGED and decision.round_n == 2


@pytest.mark.parametrize("kw, code", [
    ({"enabled": False}, automerge.HELD_DISABLED),
    ({"auto_merge": False}, automerge.HELD_DISABLED),
])
def test_neither_half_of_the_switch_arms_it_alone(kw, code):
    """`auto_merge` is read BESIDE `enabled`, never instead of it: the acceptance this
    rides on is the panel's, so turning the panel off must stop the merges with it."""
    assert automerge.decide(rnd(), WO, pr(), ValidationConfig(
        **{"enabled": True, "auto_merge": True, **kw})).code == code


@pytest.mark.parametrize("status", ["needs_review", "running", "completed",
                                    "validating", "cancelled"])
def test_only_a_work_order_parked_behind_its_pull_request_merges(status):
    """`needs_review` is the one worth naming: it means a human owes a decision — a panel
    escalation, a closed pull request, a pending assumption — and the machine does not
    merge over a human's outstanding decision."""
    decision = automerge.decide(rnd(), {**WO, "status": status}, pr(), cfg())
    assert not decision.armed and decision.code == automerge.HELD_STATUS


def test_an_assumption_still_waiting_for_the_user_holds_the_merge():
    """Redundant with the status check via `ops.land_when_cleared`, and re-checked
    because the redundancy is the point — two-gates-not-a-chain."""
    decision = automerge.decide(rnd(), WO, pr(), cfg(), pending_assumptions=True)
    assert not decision.armed and decision.code == automerge.HELD_ASSUMPTIONS


@pytest.mark.parametrize("outcome", ["pending", "failed", "rejected", "escalated"])
def test_only_a_passed_round_arms_it_and_a_panel_that_never_answered_does_not(outcome):
    """Issue #235: the panel has died silently on a Claude usage limit, leaving the round
    `pending` or `failed`. The predicate is "the latest round PASSED" and not "no round
    was rejected" precisely so that a review nobody completed reads as not validated."""
    decision = automerge.decide(rnd(outcome=outcome), WO, pr(), cfg())
    assert not decision.armed and decision.code == automerge.HELD_NOT_PASSED


def test_a_work_order_the_panel_never_judged_is_not_merged():
    decision = automerge.decide(None, WO, pr(), cfg())
    assert not decision.armed and decision.code == automerge.HELD_NOT_PASSED


def test_a_round_that_recorded_no_commit_never_reads_as_matching():
    """`''` is the fail-closed value: a worktree packet, or a round written before the
    column existed. "Not recorded" must never read as "matches"."""
    decision = automerge.decide(rnd(head_sha=""), WO, pr(), cfg())
    assert not decision.armed and decision.code == automerge.HELD_SHA_UNRECORDED


def test_a_head_that_moved_after_the_pass_holds_the_merge_and_says_both_commits():
    """THE CASE THE WHOLE DESIGN EXISTS FOR. The reason line names both commits because
    it is what the user reads to decide whether to merge it themselves."""
    decision = automerge.decide(rnd(), WO, pr(head_sha=PUSHED), cfg())
    assert not decision.armed and decision.code == automerge.HELD_SHA_MOVED
    assert JUDGED[:10] in decision.reason and PUSHED[:10] in decision.reason


def test_a_pull_request_with_no_head_sha_at_all_is_not_a_match():
    """GitHub answering null is "unknown", and unknown is not equal to anything."""
    assert not automerge.decide(rnd(), WO, pr(head_sha=None), cfg()).armed


@pytest.mark.parametrize("kw", [
    {"state": "CLOSED"},
    {"state": "MERGED"},
    {"mergeable": "CONFLICTING"},
    {"mergeable": None},                                   # not computed yet
    {"merge_state": "UNKNOWN"},
    {"merge_state": "BLOCKED"},                            # a requirement outstanding
    {"merge_state": "UNSTABLE"},                           # a non-required check red
    {"merge_state": "BEHIND"},
    {"checks": ()},                                        # no checks is not green
    {"checks": (check("unit", "FAILURE"),)},
    {"checks": (check("unit", "", "IN_PROGRESS"),)},       # queued is not passed
    {"checks": (check("unit"), check("evals", "", "QUEUED"))},
])
def test_github_must_positively_say_open_mergeable_green_and_clean(kw):
    decision = automerge.decide(rnd(), WO, pr(**kw), cfg())
    assert not decision.armed and decision.code == automerge.HELD_PR_NOT_READY


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
    assert "--squash" in command and "--delete-branch" in command


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
                   head_sha=JUDGED)

    automerge.apply(store, wo, JUDGED, approval)

    with pytest.raises(automerge.AutoMergeRefused, match="no longer a live grant"):
        automerge.apply(store, wo, JUDGED, store.get_approval(approval["id"]))


def test_github_refuses_the_merge_when_the_head_moved_under_it(granted, fake_gh):
    """The millisecond window between the poll's view and the merge, closed at the SERVER.
    The fake refuses on `--match-head-commit` exactly as GitHub does, so this is testing
    the flag rather than testing the fake's willingness to merge."""
    store, wo, approval = granted
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_sha=PUSHED)

    with pytest.raises(automerge.AutoMergeRefused, match="refused the merge"):
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
                   head_sha=JUDGED)

    poll(started, store)

    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert store.list_approvals(wo["id"]) == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    # ...and it did not even cost the read: the config guard is above the query.
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
                   head_sha=JUDGED)

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
                   head_sha=JUDGED)

    poll(started, store)
    poll(started, store)

    assert len(store.list_approvals(wo["id"])) == 1


def test_a_refused_request_is_not_re_asked_for_the_same_commit(started, project,
                                                               fake_gh):
    """A denial is an answer. Asking again would make the reviewer's verdict advisory."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_sha=JUDGED)
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
                   head_sha=JUDGED)
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
                   head_sha=JUDGED)
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
                   head_sha=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    # The worker pushes. Same branch, same pull request, a commit no seat has read.
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_sha=PUSHED)
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
                   head_sha=PUSHED)

    poll(started, store)
    poll(started, store)
    poll(started, store)

    assert len(store.events_of_kind(wo["id"], "automerge_held")) == 1
    assert not store.get_work_order(wo["id"])["attention_reason"]


def test_a_worktree_round_binds_nothing_and_so_merges_nothing(started, project,
                                                              fake_gh):
    """A round judged against a local diff — or one whose PR fetch failed and fell back —
    has no commit behind its verdict. `''` never reads as "matches"."""
    store, wo = arm(started, project, auto_merge=True, judged="")
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_sha=JUDGED)

    poll(started, store)

    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_a_panel_that_never_answered_does_not_merge(started, project, fake_gh):
    """Issue #235 through the loop, not only through `decide`: a round left `pending` by
    a panel that died on a usage limit must read as not validated."""
    store, wo = arm(started, project, auto_merge=True, outcome="pending")
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_sha=JUDGED)

    poll(started, store)

    assert store.list_approvals(wo["id"]) == []


def test_the_merge_stops_after_three_failures_on_one_commit(started, project, fake_gh):
    """The likeliest first failure of this whole feature is the daemon's `gh` having read
    credentials but no write scope, which no amount of retrying fixes. The work order is
    not moved and not flagged: hand-merging still works."""
    store, wo = arm(started, project, auto_merge=True)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_sha=JUDGED)
    poll(started, store)
    approval = store.list_approvals(wo["id"])[0]
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")
    fake_gh.fail("HTTP 403: Resource not accessible by integration")

    for _ in range(5):
        poll(started, store)

    assert len(store.events_of_kind(wo["id"], "automerge_failed")) <= \
        automerge.AUTO_MERGE_MAX_ATTEMPTS
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge" and not row["attention_reason"]


def test_the_work_order_says_why_it_did_not_merge_itself(started, project, fake_gh,
                                                          capsys):
    """A hold the user cannot see is a hold that reads as the OS doing nothing.

    Both surfaces, from one recorded fact: the line a person gets, and the payload
    `--json` keeps for anything that wants the SHAs. The commit is on the round line too,
    so "which diff was accepted" is answerable without a second command.
    """
    from jarvis import cli

    store, wo = arm(started, project, auto_merge=True, judged=JUDGED)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_sha=PUSHED)
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
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_sha=JUDGED)
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
                   head_sha=PUSHED)
    poll(started, store)                       # held: the head moved
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-14T10:00:00Z", head_sha=PUSHED)

    poll(started, store)

    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert store.events_of_kind(wo["id"], "automerge_merged") == []
