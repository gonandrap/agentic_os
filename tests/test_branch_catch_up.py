"""The OS catches the branch up with its base itself.

docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §5, issue #806
part 2. The other half of the same cause: nothing in the OS performs the catch-up its own
gate reviewer demands, so the head is always moved by somebody else, at a moment the OS
did not choose. §3's carry makes the move free, so the OS makes it.

**THE TRAP THIS FILE EXISTS TO CATCH is a feature that is a no-op on the fleet's busiest
repository.** `PullRequest.behind` is `mergeStateStatus == "BEHIND"`, and with
`strict_required_status_checks_policy` off — this repository — a merely-behind branch
reports `CLEAN`. A test that drives `BEHIND` alone would pass against an implementation
that never fires in production, so the detection here is driven through `base_oid`
ancestry with the merge state CLEAN throughout.

Its own file rather than more of `tests/test_base_heal.py` (already 950 lines): this is a
new behaviour with seven guards of its own.
"""

from __future__ import annotations

import dataclasses

import pytest

from jarvis import branchproof, db, gates, github, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"

#: The commit round 1 judged, the base's head, and the commit the update produces.
JUDGED = "709582ae53000000000000000000000000000aaa"
BASE_OID = "3c4d5e6f7a00000000000000000000000000f003"
BASE_OID2 = "4d5e6f7a8b00000000000000000000000000f004"
UPDATED = "c2120424ba000000000000000000000000000bbb"
PUSHED = "0ddba11000000000000000000000000000000111"


def check(name: str, conclusion: str, status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "startedAt": "2026-09-27T03:12:17Z",
            "workflowName": "ci"}


GREEN = [check("unit", "SUCCESS"), check("evals", "SUCCESS")]
RUNNING = [check("unit", "", status="IN_PROGRESS"), check("evals", "SUCCESS")]


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def local_proof(monkeypatch):
    """What the local checkout answers. The `project` fixture has no `origin`.

    Two different ancestry questions run through one function and the descendant says
    which: `origin/<base>` is proof (a) item 6 (was this a BASE merge), a sha is §5.1
    (does the head already carry the base commit). `contains` empty is a branch behind
    its base, which is the state this whole file is about.
    """
    state = {"id": "b7f0deadbeef", "fetch": True, "contains": set(),
             "base_ancestors": None}

    def fetch(repo, *refs):
        return state["fetch"]

    def patch_id(repo, base_ref, sha):
        return state["id"]

    def is_ancestor(repo, ancestor, descendant):
        if str(descendant).startswith("origin/"):
            return state["base_ancestors"] is None or ancestor in state["base_ancestors"]
        return ancestor in state["contains"]

    monkeypatch.setattr(branchproof, "fetch", fetch)
    monkeypatch.setattr(branchproof, "patch_id", patch_id)
    monkeypatch.setattr(branchproof, "is_ancestor", is_ancestor)
    return state


def parked(project_path, *, judged: str = JUDGED):
    """A work order finished behind a pull request, with a passed round on `judged`."""
    store = ProjectStore(project_path)
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
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


def behind_pr(fake_gh, *, head: str = JUDGED, base_oid: str = BASE_OID,
              checks=None) -> None:
    """A pull request GitHub calls CLEAN and that is nonetheless behind its base."""
    fake_gh.set_pr(PR, "OPEN", checks=checks if checks is not None else GREEN,
                   merge_state="CLEAN", head_oid=head, base_oid=base_oid)
    fake_gh.base_sha(base_oid)
    fake_gh.updated_head(UPDATED)


def updates(store, wo) -> list[dict]:
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], invariants.PR_BASE_UPDATED_EVENT)]


def failures(store, wo) -> list[dict]:
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"],
                                          invariants.PR_BASE_UPDATE_FAILED_EVENT)]


# -- 14. detecting it ------------------------------------------------------------------


def test_a_clean_but_behind_pull_request_is_caught_up_on_the_base_oid_not_on_behind(
        started, project, fake_gh, local_proof):
    """§5.1, AND THE REASON THE PREDICATE IS NOT `.behind`. GitHub reports this pull
    request CLEAN — the strict status-check policy is off on this repository — so an
    implementation keyed on `mergeStateStatus == "BEHIND"` does nothing here, silently,
    on the fleet's busiest project. `baseRefOid` not being an ancestor of the head is the
    authoritative test, and it costs nothing: it rides on a read the poll already makes.
    """
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)

    assert fake_gh.updates == [PR]
    [row] = updates(store, wo)
    assert row["cause"] == "behind"
    assert row["base_sha"] == BASE_OID
    assert row["head_before"] == JUDGED and row["head_after"] == UPDATED
    # NOT MERGED, and no gate asked for: the head the grant would name has just moved.
    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_a_head_that_already_carries_the_base_commit_is_left_alone(
        started, project, fake_gh, local_proof):
    """The control, and the one that keeps this from touching every pull request in the
    fleet: up to date is `base_oid` reachable from the head, and then nothing happens."""
    opt_in(started)
    store, wo = parked(project)
    local_proof["contains"] = {BASE_OID}
    behind_pr(fake_gh)

    poll(started, store)

    assert fake_gh.updates == []
    assert updates(store, wo) == []
    # ...and the ordinary path still runs: green, CLEAN, judged head — so it is asked.
    assert [a["kind"] for a in store.list_approvals(wo["id"])] == ["auto_merge"]


def test_a_pull_request_github_calls_behind_is_caught_up_from_the_held_path(
        started, project, fake_gh, local_proof):
    """THE OTHER ARM, §5.2's first bullet — the rarer half, and the only one `decide` can
    see. GitHub says BEHIND outright, so the hold is `merge_state_unclean` and the catch-up
    fires from the held path rather than from in front of the approval lookup.

    The head already CARRIES the base commit here, so ancestry says up to date and `.behind`
    is the only thing left saying otherwise: an implementation that dropped the cheap
    positive short-circuit leaves this pull request held for ever.
    """
    opt_in(started)
    store, wo = parked(project)
    local_proof["contains"] = {BASE_OID}
    behind_pr(fake_gh)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="BEHIND", head_oid=JUDGED,
                   base_oid=BASE_OID)

    poll(started, store)

    assert fake_gh.updates == [PR]
    [row] = updates(store, wo)
    assert row["cause"] == "behind" and row["head_after"] == UPDATED
    # The hold is on the record too — the held path writes it before catching up, so a
    # reader sees why the branch was moved (§3.5's first ordering note keeps that order).
    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]
    assert [h["code"] for h in held] == ["merge_state_unclean"]
    assert store.list_approvals(wo["id"]) == []


# -- 15. once per base commit ----------------------------------------------------------


def test_one_update_per_base_commit(started, project, fake_gh, local_proof):
    """GUARD 4, §5.3: the key is `base_oid` and it is shared with the red-base heal. The
    bound is about how often the OS may rebuild one merge ref, not about why."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)
    poll(started, store)

    assert fake_gh.updates == [PR]
    assert len(updates(store, wo)) == 1


def test_a_refused_update_spends_the_attempt(started, project, fake_gh, local_proof):
    """GUARD 7 and `ops.base_heal_spent`'s rule: a pull request GitHub will not update is
    otherwise retried every two minutes for ever. Recorded with its cause, so the record
    says which of the two reasons asked for it."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    fake_gh.refuse_update("failed to update branch")

    poll(started, store)
    poll(started, store)

    assert fake_gh.updates == [PR]
    assert updates(store, wo) == []
    [row] = failures(store, wo)
    assert row["cause"] == "behind" and row["base_sha"] == BASE_OID


# -- 16. every guard defers, and writes nothing ----------------------------------------


def assert_deferred(fake_gh, store, wo):
    assert fake_gh.updates == []
    assert updates(store, wo) == [] and failures(store, wo) == []


def test_a_running_turn_defers_the_catch_up(started, project, fake_gh, local_proof):
    """GUARD 2. Moving the head under a running worker is a push it did not make."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.create_turn(wo["id"], "message", "have another look")

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


def test_an_undelivered_message_defers_the_catch_up(started, project, fake_gh,
                                                    local_proof):
    """GUARD 2's other half: an instruction the worker has not read yet may be about this
    very branch, and the turn it starts would push onto a head the OS just moved."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.queue_message(wo["id"], "rebase this yourself")

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


def test_an_open_validation_round_defers_the_catch_up(started, project, fake_gh,
                                                      local_proof):
    """GUARD 3, Neo question 283: the branch must not move beneath the seats mid-round.

    Driven through the method rather than the poll, and the difference is the point:
    `decide` reads the round before it reads anything about the base, so an open round
    already holds on `not_passed` and the poll never gets here. The guard is the belt to
    that braces — it holds whoever calls this, in whatever order `decide` grows."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp-2")
    pr = github.pr_view(PR, cwd=project)

    assert started._catch_up_with_base(started.catalog.project("proj_a"), store, wo,
                                       pr) is pr
    assert_deferred(fake_gh, store, wo)


def test_the_cap_stops_the_os_chasing_a_fast_moving_base(started, project, fake_gh,
                                                         local_proof):
    """GUARD 5, and the per-base-sha key cannot do this job: every commit on `main` is a
    new key and would earn a fresh update, so a busy day would have the OS chasing the
    base for ever. Past `ops.CATCH_UP_MAX` the pull request stays held."""
    opt_in(started)
    store, wo = parked(project)
    for i in range(ops.CATCH_UP_MAX):
        ops.record_base_update(store, wo, base="main", base_sha=f"{i}" * 40,
                               head_before=JUDGED, head_after=JUDGED, checks=(),
                               cause=ops.BASE_UPDATE_BEHIND)
    behind_pr(fake_gh, base_oid=BASE_OID2)

    poll(started, store)

    assert fake_gh.updates == []
    assert len(updates(store, wo)) == ops.CATCH_UP_MAX


def test_a_head_that_moved_between_the_re_read_and_the_update_is_not_updated(
        started, project, fake_gh, local_proof, monkeypatch):
    """GUARD 6. `gh pr update-branch` has no `--match-head-commit`, so the head is read
    again immediately before it and must still be the judged commit — a worker turn that
    ended in the window means the update would merge the base into a push nobody judged.
    """
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    real = github.pr_view
    seen = {"n": 0}

    def moving(url, cwd=None):
        pr = real(url, cwd=cwd)
        seen["n"] += 1
        return dataclasses.replace(pr, head_oid=PUSHED) if seen["n"] > 1 else pr

    monkeypatch.setattr(github, "pr_view", moving)

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


def test_a_project_that_has_not_opted_in_is_never_touched(started, project, fake_gh,
                                                          local_proof):
    """GUARD 1: `auto_merge`'s own gate. A project that has not given the OS permission to
    merge its pull requests pays nothing and no branch of its is moved."""
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


# -- 17. end to end --------------------------------------------------------------------


def test_behind_then_updated_then_green_then_carried_then_merged_with_no_round(
        started, project, fake_gh, local_proof):
    """THE WHOLE LOOP, §5.3's closing paragraph. The OS moves the head itself, waits out
    the CI the move restarted, carries the verdict across its own merge and merges — and
    the round ledger never moves, which is the point of the feature."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)                      # behind -> updated
    assert fake_gh.updates == [PR]

    # The new head's CI is not finished, so `decide` holds and the poll waits — but the
    # CARRY does not wait for it (§3.5's second ordering note): waiting would defer it
    # into exactly the window where a round gets spent.
    fake_gh.set_pr(PR, "OPEN", checks=RUNNING, merge_state="CLEAN", head_oid=UPDATED,
                   base_oid=BASE_OID)
    local_proof["contains"] = {BASE_OID}      # the head carries the base now
    poll(started, store)
    assert store.list_approvals(wo["id"]) == []

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=UPDATED,
                   base_oid=BASE_OID)
    poll(started, store)                      # green -> gate filed

    carried = store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)
    assert len(carried) == 1
    assert db.from_json(carried[0]["payload"], {})["carried_head_sha"] == UPDATED
    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    assert UPDATED in approvals[0]["command"]

    gates.apply_decision(store, approvals[0]["id"], "approved", "ok", "neo",
                         project="proj_a")
    poll(started, store)

    assert [c["argv"][2] for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]] \
        == [PR]
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
