"""The OS catches the branch up with its base itself (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §5)"""

from __future__ import annotations

import dataclasses

import pytest

from jarvis import branchproof, db, gates, github, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"

#: The commit round 1 judged, the base's head, and the commit the update produces
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
def local_proof(local_base, monkeypatch):
    """The shared checkout fake (`testing.local_base`) plus the one thing it does not fake:..."""
    local_base.update({"id": "b7f0deadbeef", "contains": set(), "tip": BASE_OID})
    monkeypatch.setattr(branchproof, "diff_fingerprint",
                        lambda repo, base_ref, sha: local_base["id"])
    return local_base


def parked(project_path, *, judged: str = JUDGED):
    """A work order finished behind a pull request, with a passed round on `judged`"""
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
    """A pull request GitHub calls CLEAN and that is nonetheless behind its base"""
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


# -- 14. detecting it


def test_a_clean_but_behind_pull_request_is_caught_up_on_the_base_oid_not_on_behind(
        started, project, fake_gh, local_proof):
    """§5.1, AND THE REASON THE PREDICATE IS NOT `.behind`"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)

    assert fake_gh.updates == [PR]
    [row] = updates(store, wo)
    assert row["cause"] == "behind"
    assert row["base_sha"] == BASE_OID
    assert row["head_before"] == JUDGED and row["head_after"] == UPDATED
    # NOT MERGED, and no gate asked for: the head the grant would name has just moved
    assert store.list_approvals(wo["id"]) == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def test_a_head_that_already_carries_the_base_commit_is_left_alone(
        started, project, fake_gh, local_proof):
    """The control, and the one that keeps this from touching every pull request in the fleet: up to date..."""
    opt_in(started)
    store, wo = parked(project)
    local_proof["contains"] = {BASE_OID}
    behind_pr(fake_gh)

    poll(started, store)

    assert fake_gh.updates == []
    assert updates(store, wo) == []
    # ...and the ordinary path still runs: green, CLEAN, judged head — so it is asked
    assert [a["kind"] for a in store.list_approvals(wo["id"])] == ["auto_merge"]


def test_a_pull_request_github_calls_behind_is_caught_up_from_the_held_path(
        started, project, fake_gh, local_proof):
    """THE OTHER ARM, §5.2's first bullet — the rarer half"""
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
    # The hold is on the record too — the held path writes it before catching up
    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]
    # DONE, not a refusal: the sentence says the head moved and CI is running on it
    assert [h["code"] for h in held] == ["base_moved"]
    assert "caught up with `main`" in held[0]["reason"]
    assert store.list_approvals(wo["id"]) == []


# -- 15. once per base commit


def test_one_update_per_base_commit(started, project, fake_gh, local_proof):
    """GUARD 4, §5.3: the key is `base_oid` and it is shared with the red-base heal"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)
    poll(started, store)

    assert fake_gh.updates == [PR]
    assert len(updates(store, wo)) == 1


def test_a_refused_update_spends_the_attempt(started, project, fake_gh, local_proof):
    """GUARD 7 and `ops.base_heal_spent`'s rule: a pull request GitHub will not update is otherwise..."""
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


# -- 16. every guard defers, and writes nothing


def assert_deferred(fake_gh, store, wo):
    assert fake_gh.updates == []
    assert updates(store, wo) == [] and failures(store, wo) == []


def test_a_running_turn_defers_the_catch_up(started, project, fake_gh, local_proof):
    """GUARD 2"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.create_turn(wo["id"], "message", "have another look")

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


def test_an_undelivered_message_defers_the_catch_up(started, project, fake_gh,
                                                    local_proof):
    """GUARD 2's other half: an instruction the worker has not read yet may be about this very branch"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.queue_message(wo["id"], "rebase this yourself")

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


def test_an_open_validation_round_defers_the_catch_up(started, project, fake_gh,
                                                      local_proof):
    """GUARD 3, Neo question 283: the branch must not move beneath the seats mid-round"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp-2")
    pr = github.pr_view(PR, cwd=project)

    caught = started._catch_up_with_base(started.catalog.project("proj_a"), store, wo, pr)
    assert caught.pr is pr and caught.outcome == ops.CATCH_UP_DEFERRED
    assert_deferred(fake_gh, store, wo)


def test_the_cap_stops_the_os_chasing_a_fast_moving_base(started, project, fake_gh,
                                                         local_proof):
    """GUARD 5, and the per-base-sha key cannot do this job: every commit on `main` is a new key and would..."""
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
    """GUARD 6"""
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
    """GUARD 1: `auto_merge`'s own gate"""
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)

    assert_deferred(fake_gh, store, wo)


# -- 17. end to end


def test_behind_then_updated_then_green_then_carried_then_merged_with_no_round(
        started, project, fake_gh, local_proof):
    """THE WHOLE LOOP, §5.3's closing paragraph"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)

    poll(started, store)                      # behind -> updated
    assert fake_gh.updates == [PR]

    # The new head's CI is not finished, so `decide` holds and the poll waits
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


def test_a_merge_that_moved_the_base_makes_the_next_one_catch_up_before_it_merges(
        started, project, fake_gh, local_proof):
    """TWO APPROVED MERGES IN A ROW (spec 2026-09-28 §5 test 7, §4)"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh, base_oid=BASE_OID)
    local_proof["contains"] = {BASE_OID}          # up to date when the grant was given

    poll(started, store)                          # armed -> a request is filed
    [approval] = store.list_approvals(wo["id"])
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    # The other merge lands: `main` moves to a commit this head cannot contain
    local_proof["tip"] = BASE_OID2
    poll(started, store)
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert fake_gh.updates == [PR]                # caught up instead
    assert store.get_approval(approval["id"])["uses"] == 0

    # CI runs on the new head, goes green, the verdict carries and the merge happens
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=UPDATED,
                   base_oid=BASE_OID2)
    local_proof["contains"] = {BASE_OID, BASE_OID2}
    poll(started, store)
    fresh = [a for a in store.list_approvals(wo["id"]) if UPDATED in a["command"]]
    assert len(fresh) == 1
    gates.apply_decision(store, fresh[0]["id"], "approved", "ok", "neo",
                         project="proj_a")

    poll(started, store)

    assert [c["argv"][2] for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]] \
        == [PR]
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1


# -- 18. the base tip the ancestry test is asked about (spec 2026-09-28 Amendment B)


def a_pull_request(**over):
    """The three fields `ops.catch_up_needed` reads, and nothing else"""
    import types

    return types.SimpleNamespace(**{"behind": False, "base_oid": BASE_OID,
                                    "head_oid": JUDGED, **over})


def test_a_given_base_tip_outranks_githubs_cached_reading(project, local_proof):
    """THE MEASURED CASE, issue #837"""
    local_proof["contains"] = {BASE_OID}          # the head carries what GitHub reported

    assert not ops.catch_up_needed(a_pull_request(), repo=project)
    assert ops.catch_up_needed(a_pull_request(), repo=project, base_tip=BASE_OID2)


def test_no_base_tip_keeps_todays_behaviour_exactly(project, local_proof):
    """The empty default is the other arm: `pr.base_oid` is read"""
    local_proof["contains"] = {BASE_OID}

    assert not ops.catch_up_needed(a_pull_request(), repo=project, base_tip="")
    assert ops.catch_up_needed(a_pull_request(base_oid=BASE_OID2), repo=project)
    # `.behind` stays a positive short-circuit in BOTH arms — kn-907c9a61
    assert ops.catch_up_needed(a_pull_request(behind=True), repo=project)
    assert ops.catch_up_needed(a_pull_request(behind=True), repo=project,
                               base_tip=BASE_OID)


# -- 19. every guard has an outcome, and none of them reaches `propose`


def held(store, wo) -> list[dict]:
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]


def test_a_deferred_catch_up_says_so_and_proposes_nothing(started, project, fake_gh,
                                                          local_proof):
    """§3.4 `DEFERRED`: a guard that clears by itself, and the sentence says the OS retries"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    store.queue_message(wo["id"], "rebase this yourself")

    poll(started, store)

    assert [h["code"] for h in held(store, wo)] == ["base_moved"]
    assert "retries every tick" in held(store, wo)[0]["reason"]
    assert store.list_approvals(wo["id"]) == [] and fake_gh.updates == []


def test_an_exhausted_catch_up_names_the_cap_and_proposes_nothing(started, project,
                                                                  fake_gh, local_proof):
    """§3.4 `EXHAUSTED`, AND THE REGRESSION THIS SPEC TURNS ON"""
    opt_in(started)
    store, wo = parked(project)
    for i in range(ops.CATCH_UP_MAX):
        ops.record_base_update(store, wo, base="main", base_sha=f"{i}" * 40,
                               head_before=JUDGED, head_after=JUDGED, checks=(),
                               cause=ops.BASE_UPDATE_BEHIND)
    behind_pr(fake_gh, base_oid=BASE_OID2)
    local_proof["tip"] = BASE_OID2

    poll(started, store)

    [row] = held(store, wo)
    assert row["code"] == "base_moved"
    assert f"the cap ({ops.CATCH_UP_MAX})" in row["reason"] and "will NOT" in row["reason"]
    assert store.list_approvals(wo["id"]) == [] and fake_gh.updates == []


def test_a_base_commit_already_chased_once_is_exhausted_too(started, project, fake_gh,
                                                            local_proof):
    """§3.4's other `EXHAUSTED` row: `ops.base_heal_spent`, keyed on the FRESHLY READ tip (Amendment B)"""
    opt_in(started)
    store, wo = parked(project)
    ops.record_base_update(store, wo, base="main", base_sha=BASE_OID,
                           head_before=JUDGED, head_after=JUDGED, checks=(),
                           cause=ops.BASE_UPDATE_BEHIND)
    behind_pr(fake_gh)

    poll(started, store)

    [row] = held(store, wo)
    assert row["code"] == "base_moved" and BASE_OID[:10] in row["reason"]
    assert store.list_approvals(wo["id"]) == [] and fake_gh.updates == []


def test_a_failed_update_holds_with_the_reason_github_gave(started, project, fake_gh,
                                                           local_proof):
    """§3.4 `FAILED`: the attempt is spent, the record says why, and the OS retries"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    fake_gh.refuse_update("failed to update branch")

    poll(started, store)

    [row] = held(store, wo)
    assert row["code"] == "base_moved" and "retries next tick" in row["reason"]
    assert store.list_approvals(wo["id"]) == []


def test_a_fetch_that_fails_holds_rather_than_proposing(started, project, fake_gh,
                                                        local_proof):
    """§3.4 `FAILED`'s first row, and the fall-through this spec exists to close: a checkout that cannot..."""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    local_proof["fetch"] = False

    poll(started, store)

    assert [h["code"] for h in held(store, wo)] == ["base_moved"]
    assert store.list_approvals(wo["id"]) == [] and fake_gh.updates == []


# -- 20. the cap is exhausted, and a human has to be told (#1173)


def merges(fake_gh) -> list[str]:
    return [c["argv"][2] for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


def at_the_cap(started, project, fake_gh, local_proof):
    """A granted merge whose base has run on past a `pr.base_oid` that lags it (#1173)"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh, base_oid=BASE_OID)
    local_proof["contains"] = {BASE_OID}      # up to date when the grant was given

    poll(started, store)                      # armed -> a request is filed
    [approval] = store.list_approvals(wo["id"])
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    for i in range(ops.CATCH_UP_MAX):
        ops.record_base_update(store, wo, base="main", base_sha=f"{i}" * 40,
                               head_before=JUDGED, head_after=JUDGED, checks=(),
                               cause=ops.BASE_UPDATE_BEHIND)
    # `main` moved again; GitHub's cached `baseRefOid` still names the contained commit
    local_proof["tip"] = BASE_OID2
    return store, wo


def test_a_base_that_outran_the_cached_reading_holds_at_the_cap(started, project,
                                                                fake_gh, local_proof):
    """THE MEASURED CASE, issue #1173: the granted merge could only refuse, every tick"""
    store, wo = at_the_cap(started, project, fake_gh, local_proof)

    poll(started, store)
    poll(started, store)

    assert merges(fake_gh) == [] and fake_gh.updates == []
    # NOT ATTEMPTED AT ALL: the granted merge was refused as stale on every tick before
    assert store.events_of_kind(wo["id"], ops.MERGE_BASE_STALE_EVENT) == []
    [row] = held(store, wo)
    assert row["code"] == "base_moved" and row["catch_up"] == ops.CATCH_UP_EXHAUSTED
    assert f"the cap ({ops.CATCH_UP_MAX})" in row["reason"]
    # ...and the order asks the user, instead of sitting on a hold nobody owns
    assert invariants.CATCH_UP_EXHAUSTED_BLOCKER in invariants.true_blockers(
        store, store.get_work_order(wo["id"]))
    list(invariants.check_blocked_work_is_surfaced(store))
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 1
    assert fresh["attention_reason"] == invariants.CATCH_UP_EXHAUSTED_BLOCKER


def lagging(monkeypatch) -> dict:
    """GitHub publishing the merge commit AFTER the OS re-reads the head (#1188)"""
    real = github.pr_view
    lag = {"on": True}

    def view(url, cwd=None):
        pr = real(url, cwd=cwd)
        return dataclasses.replace(pr, head_oid=JUDGED) if lag["on"] else pr

    monkeypatch.setattr(github, "pr_view", view)
    return lag


def test_a_merge_commit_published_late_is_carried_and_not_deferred_for_ever(
        started, project, fake_gh, local_proof, monkeypatch):
    """#1188: the head the OS'S OWN catch-up produced held the order for 2.5h"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh)
    lag = lagging(monkeypatch)

    poll(started, store)                      # the update lands; the re-read lags it
    [row] = updates(store, wo)
    assert row["head_before"] == JUDGED and row["head_after"] == JUDGED

    # GitHub publishes the merge commit, and `main` moves on again
    lag["on"] = False
    local_proof["tip"] = BASE_OID2
    poll(started, store)

    carried = [db.from_json(e["payload"], {})
               for e in store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)]
    assert [c["carried_head_sha"] for c in carried] == [UPDATED]
    assert invariants.CARRY_UNPROVEN_BLOCKER not in invariants.true_blockers(
        store, store.get_work_order(wo["id"]))


def test_a_head_the_os_cannot_prove_asks_the_user_instead_of_deferring(
        started, project, fake_gh, local_proof, monkeypatch):
    """#1188's other arm, Neo question 2153: a hold nothing can prove must be VISIBLE"""
    opt_in(started)
    store, wo = parked(project)
    behind_pr(fake_gh, head=PUSHED)           # somebody else's commit at the head
    local_proof["tip"] = BASE_OID2
    monkeypatch.setattr(branchproof, "diff_fingerprint",
                        lambda repo, base_ref, sha: "fp-" + sha)

    poll(started, store)

    [row] = held(store, wo)
    assert row["code"] == "base_moved" and row["catch_up"] == ops.CATCH_UP_DEFERRED
    assert row["catch_up_cause"] == ops.CATCH_UP_DEFER_CARRY_UNPROVEN
    assert "will NOT" in row["reason"] and "retries every tick" not in row["reason"]
    assert fake_gh.updates == []
    assert invariants.CARRY_UNPROVEN_BLOCKER in invariants.true_blockers(
        store, store.get_work_order(wo["id"]))
    list(invariants.check_blocked_work_is_surfaced(store))
    fresh = store.get_work_order(wo["id"])
    assert fresh["needs_attention"] == 1
    assert fresh["attention_reason"] == invariants.CARRY_UNPROVEN_BLOCKER


def test_the_exhausted_hold_is_not_overwritten_by_a_lesser_reading(started, project,
                                                                   fake_gh, local_proof):
    """§3.1's rule, one hold later: `mergeable` going UNKNOWN must not retire the ask (#1173)"""
    store, wo = at_the_cap(started, project, fake_gh, local_proof)
    poll(started, store)

    # The fetch fails, so nothing contradicts the cached base, and GitHub stops answering
    local_proof["fetch"] = False
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_OID, mergeable="UNKNOWN")

    poll(started, store)

    [row] = held(store, wo)
    assert row["code"] == "base_moved" and row["catch_up"] == ops.CATCH_UP_EXHAUSTED
    assert invariants.CATCH_UP_EXHAUSTED_BLOCKER in invariants.true_blockers(
        store, store.get_work_order(wo["id"]))
