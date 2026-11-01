"""A merge request that names the base it was filed against, and is withdrawn when it moves (docs/superpowers/specs/2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.5)"""

from __future__ import annotations

import pytest

from jarvis import automerge, db, gates, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.catalog import DEFAULT_PULL_REQUESTS_CATCH_UP_MAX as CATCH_UP_MAX

PR = "https://github.com/acme/proj/pull/7"

#: The commit round 1 judged, the base tip it contains, and the tip it does NOT
JUDGED = "709582ae53000000000000000000000000000aaa"
BASE_1 = "8ea18feec4000000000000000000000000000821"
BASE_2 = "37fe650fab000000000000000000000000000827"
#: Where `origin/main` REALLY is when GitHub is still reporting BASE_2
BASE_3 = "9d41c0be77000000000000000000000000000833"
UPDATED = "c2120424ba000000000000000000000000000bbb"


def check(name: str, conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "workflowName": "ci"}


GREEN = [check("unit"), check("evals")]


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def local(local_base):
    """The shared checkout fake (`testing.local_base`), at this file's base commit"""
    local_base.update({"tip": BASE_1, "contains": {BASE_1}})
    return local_base


def opt_in(daemon):
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_merge = True


def parked(project_path, *, judged: str = JUDGED):
    store = ProjectStore(project_path)
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-" + wo["id"])
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    return store, store.get_work_order(wo["id"])


def poll(daemon, store):
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def green_pr(fake_gh, *, head: str = JUDGED, base_oid: str = BASE_1, checks=None):
    fake_gh.set_pr(PR, "OPEN", checks=checks if checks is not None else GREEN,
                   merge_state="CLEAN", head_oid=head, base_oid=base_oid)
    fake_gh.base_sha(base_oid)
    fake_gh.updated_head(UPDATED)


def merges(fake_gh) -> list:
    return [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]


# -- the incident


def test_gate_308_a_head_that_contains_only_githubs_stale_base_is_never_proposed(
        started, project, fake_gh, local):
    """THE REGRESSION, issue #837 / gate 308"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh, base_oid=BASE_1)         # GitHub's cached reading: 5.3h behind
    local.update({"tip": BASE_2, "contains": {BASE_1}})

    poll(started, store)

    assert store.list_approvals(wo["id"]) == []
    assert merges(fake_gh) == []


def test_a_base_that_moves_between_the_verdict_and_the_merge_refuses_and_catches_up(
        started, project, fake_gh, local):
    """§3.1's daemon arm: the grant is live, the head has not moved, and `main` has"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    local["tip"] = BASE_2                      # `main` moved while Neo was reading
    poll(started, store)

    assert merges(fake_gh) == []
    [event] = store.events_of_kind(wo["id"], ops.MERGE_BASE_STALE_EVENT)
    payload = db.from_json(event["payload"], {})
    assert payload["head_sha"] == JUDGED and payload["base_oid"] == BASE_2
    assert fake_gh.updates == [PR]             # ...and the catch-up ran instead
    assert store.get_approval(approval["id"])["uses"] == 0


def test_a_checkout_that_cannot_resolve_githubs_base_oid_does_not_stall_for_ever(
        started, project, fake_gh, local):
    """THE LIVELOCK"""
    opt_in(started)
    store, wo = parked(project)
    # GitHub reports a base commit this checkout has never seen
    green_pr(fake_gh, base_oid=BASE_2)
    local.update({"tip": BASE_1, "contains": {BASE_1}})

    poll(started, store)
    poll(started, store)

    assert [db.from_json(e["payload"], {})["code"]
            for e in store.events_of_kind(wo["id"], "automerge_held")] == []
    [approval] = store.list_approvals(wo["id"])
    assert approval["kind"] == gates.AUTO_MERGE
    assert approval["status"] == "pending" and approval["closed_as"] == ""
    assert fake_gh.updates == []

    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")
    poll(started, store)

    assert [c["argv"][:2] for c in merges(fake_gh)] == [["pr", "merge"]]


# -- §3.5: the base is ON the request


def test_the_request_records_and_states_the_base_it_was_filed_against(
        started, project, fake_gh, local):
    """§3.5: a column, the `automerge_proposed` payload, and the sentence the reviewer reads"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)

    poll(started, store)

    [approval] = store.list_approvals(wo["id"])
    assert approval["base_oid"] == BASE_1
    [proposed] = store.events_of_kind(wo["id"], "automerge_proposed")
    assert db.from_json(proposed["payload"], {})["base_oid"] == BASE_1
    neo = NeoStore()
    try:
        text = neo.list_questions()[0]["question"]
    finally:
        neo.close()
    assert f"current tip of `main` ({BASE_1})" in text
    assert "re-reads `main` immediately before merging" in text


def test_the_column_arrives_on_a_database_that_was_created_without_it(started, project):
    """§3.5 through `ADDED_COLUMNS` + `_migrate`"""
    store = ProjectStore(project)
    store.conn.execute("ALTER TABLE approvals DROP COLUMN base_oid")
    wo = ops.create_work_order("proj_a", "old order")
    store.conn.execute(
        "INSERT INTO approvals (wo_id, ts, kind, command, max_uses) VALUES (?,?,?,?,?)",
        (wo["id"], db.now(), gates.AUTO_MERGE, "gh pr merge old", 1))
    store.close()

    store = ProjectStore(project)
    try:
        assert [a["base_oid"] for a in store.list_approvals(wo["id"])] == [""]
    finally:
        store.close()


# -- §3.6: a pending request whose base moved is superseded


def test_a_pending_request_whose_base_moved_is_superseded_and_never_answered(
        started, project, fake_gh, local):
    """§3.6: nothing is authorised and nothing is refused"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    assert approval["status"] == "pending"

    # GitHub says BASE_2; the checkout, after fetching, says BASE_3
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_2)
    local["tip"] = BASE_3
    poll(started, store)

    row = store.get_approval(approval["id"])
    assert row["status"] == "expired" and row["closed_as"] == "superseded"
    # The recorded base of the request, and the FETCHED tip
    assert row["decided_by"] == "os" and BASE_1[:10] in row["decision_reason"]
    assert BASE_3[:10] in row["decision_reason"]
    assert BASE_2[:10] not in row["decision_reason"]
    assert store.usable_grant(wo["id"], gates.AUTO_MERGE, row["command"]) is None
    neo = NeoStore()
    try:
        question = neo.get(approval["neo_question_id"])
    finally:
        neo.close()
    assert question["answered_by"] == "os" and "SUPERSEDED" in (question["answer"] or "")


def test_an_already_approved_request_is_left_exactly_as_it_was(started, project,
                                                               fake_gh, local):
    """§3.6's race, and why it is safe: Neo can answer between the read and the write"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo",
                         project="proj_a")

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_2)
    local["tip"] = BASE_2
    poll(started, store)

    row = store.get_approval(approval["id"])
    assert row["status"] == "approved" and row["closed_as"] == ""
    assert merges(fake_gh) == []               # ...and it still merged nothing


def test_the_propose_supersede_loop_is_bounded_and_ends_in_one_exhausted_hold(
        started, project, fake_gh, local, monkeypatch):
    """§3.6's three bounds, driven on a base that moves on every tick"""
    # The ticks here are instants apart: the backoff between them is not what this bounds
    monkeypatch.setattr(ops, "catch_up_backoff", lambda *a, **k: 0.0)
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)

    for i in range(CATCH_UP_MAX + 2):
        tip = f"{i}" * 40
        fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                       base_oid=tip)
        fake_gh.base_sha(tip)
        fake_gh.updated_head(JUDGED)           # the head stays judged, the base keeps moving
        local.update({"tip": tip, "contains": set()})
        poll(started, store)

    assert len(fake_gh.updates) <= CATCH_UP_MAX
    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]
    assert held and f"the cap ({CATCH_UP_MAX})" in held[-1]["reason"]
    assert merges(fake_gh) == []


# -- §3.4: the hold names the FETCHED tip, never `pr.base_oid`


def test_the_hold_on_a_refused_catch_up_names_the_fetched_tip_not_githubs(
        started, project, fake_gh, local):
    """§3.1: GitHub's cached `baseRefOid` is untrustworthy, so it may not be quoted at a user"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_2)
    local["tip"] = BASE_3
    poll(started, store)

    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]
    assert held, "the refused catch-up wrote no hold"
    assert BASE_3[:10] in held[-1]["reason"]
    assert BASE_2[:10] not in held[-1]["reason"]


# -- §3.6 / Amendment E: only a FETCHED base fact withdraws a request


def test_a_deferred_catch_up_leaves_the_pending_request_standing(started, project,
                                                                 fake_gh, local):
    """A deferral is retried, not withdrawn (§3.6 / Amendment E)"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    assert approval["status"] == "pending"

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_2)
    local["tip"] = BASE_3
    store.queue_message(wo["id"], "rebase this yourself")   # guard 2: pre-fetch deferral
    poll(started, store)

    row = store.get_approval(approval["id"])
    assert row["status"] == "pending" and row["closed_as"] == ""
    assert [db.from_json(e["payload"], {})["code"]
            for e in store.events_of_kind(wo["id"], "automerge_held")] == ["base_moved"]


def test_a_catch_up_that_never_fetched_leaves_the_pending_request_standing(
        started, project, fake_gh, local):
    """A pre-fetch FAILED carries no base fact at all — `base_tip` is empty (§3.6 / Amendment E)"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    assert approval["status"] == "pending"

    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_2)
    local["fetch"] = False                     # the checkout could not fetch `main`
    poll(started, store)

    row = store.get_approval(approval["id"])
    assert row["status"] == "pending" and row["closed_as"] == ""


def test_held_base_moved_without_a_fetched_tip_names_no_commit_at_all():
    """No fetched read means no commit in the sentence — not `pr.base_oid`, and not a placeholder either"""
    catch_up = ops.CatchUp(pr=None, outcome=ops.CATCH_UP_FAILED, reason="fetch failed")
    pr = type("PR", (), {"base_ref": "main", "base_oid": BASE_2, "head_oid": JUDGED})()

    reason = automerge.held_base_moved(catch_up, pr).reason

    assert BASE_2[:10] not in reason and BASE_2 not in reason
    assert "unknown commit" not in reason
    assert "`main` has moved" in reason


# -- #1459 / kn-41e5dd52: a batch merges back to back while `main` moves

PR_2 = "https://github.com/acme/proj/pull/8"
PR_3 = "https://github.com/acme/proj/pull/9"
JUDGED_2 = "a650fd2c98000000000000000000000000000ccc"
JUDGED_3 = "b760fd2c98000000000000000000000000000ddd"


def three_parked(project_path, fake_gh):
    """Three armed orders on one tip: (store, [(wo, pr_url, judged), ...])"""
    store, first = parked(project_path)
    green_pr(fake_gh)
    members = [(first, PR, JUDGED)]
    for pr_url, judged in ((PR_2, JUDGED_2), (PR_3, JUDGED_3)):
        wo = ops.create_work_order("proj_a", f"add {pr_url}")
        ops.finish(wo["id"], "opened a PR", pr_url=pr_url)
        row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-" + wo["id"])
        store.set_validation_head(row["id"], judged)
        store.close_validation_round(row["id"], "passed", "")
        fake_gh.set_pr(pr_url, "OPEN", checks=GREEN, merge_state="CLEAN",
                       head_oid=judged, base_oid=BASE_1)
        members.append((store.get_work_order(wo["id"]), pr_url, judged))
    return store, members


def test_a_batch_merges_back_to_back_on_its_own_heads_as_main_moves(
        started, project, fake_gh, local):
    """#1459 / kn-41e5dd52: one approval per PR, no catch-up, a conflict alone drops out"""
    opt_in(started)
    store, members = three_parked(project, fake_gh)

    poll(started, store)

    grants = {wo["id"]: store.list_approvals(wo["id"]) for wo, _, _ in members}
    assert all(len(g) == 1 for g in grants.values()), grants
    (first, _, _), (second, pr_2, judged_2), (third, pr_3, _) = members
    gates.apply_decision(store, grants[first["id"]][0]["id"], "approved", "ok", "neo",
                         project="proj_a")
    poll(started, store)
    assert store.get_work_order(first["id"])["status"] == "completed"

    # The first member landed: `main` moved past every other member's head
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-11-01T10:00:00Z")
    fake_gh.set_pr(pr_2, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=judged_2,
                   base_oid=BASE_2)
    fake_gh.set_pr(pr_3, "OPEN", checks=GREEN, merge_state="DIRTY",
                   mergeable="CONFLICTING", head_oid=JUDGED_3, base_oid=BASE_2)
    fake_gh.base_sha(BASE_2)
    local.update({"tip": BASE_2, "contains": {BASE_1}})
    for wo in (second, third):
        gates.apply_decision(store, grants[wo["id"]][0]["id"], "approved", "ok", "neo",
                             project="proj_a")
    poll(started, store)

    assert store.get_work_order(second["id"])["status"] == "completed"
    assert [c["argv"][c["argv"].index("--match-head-commit") + 1]
            for c in merges(fake_gh)] == [JUDGED, judged_2]
    assert store.events_of_kind(second["id"], "pr_base_updated") == []
    assert pr_2 not in fake_gh.updates
    # The conflicting member alone takes the catch-up path
    assert store.get_work_order(third["id"])["status"] == "waiting_pr_merge"
    held = [db.from_json(e["payload"], {})["code"]
            for e in store.events_of_kind(third["id"], "automerge_held")]
    assert automerge.HELD_BASE_MOVED in held or pr_3 in fake_gh.updates, held
    assert len(store.list_approvals(second["id"])) == 1


def test_main_moving_with_no_batch_merge_still_refuses_a_stale_base(
        started, project, fake_gh, local):
    """The batch carve-out needs an OS merge since the grant was filed, not just a live grant"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    gates.apply_decision(store, approval["id"], "approved", "ok", "neo", project="proj_a")

    local.update({"tip": BASE_2, "contains": {BASE_1}})
    poll(started, store)

    assert merges(fake_gh) == []


def test_a_new_request_expires_the_grant_its_old_head_left_unspent(started, project):
    """#1459: superseded approvals are expired, not left `approved`"""
    store, wo = parked(project)
    old = store.add_approval(wo["id"], gates.AUTO_MERGE,
                             automerge.merge_command(PR, JUDGED), max_uses=1)
    store.decide_approval(old["id"], "approved", "ok", "neo")
    decision = automerge.Decision(armed=True, code="armed", reason="round 2 passed",
                                  judged_sha=UPDATED, head_sha=UPDATED, round_n=2)
    neo = NeoStore()
    try:
        new = automerge.propose(store, neo, "proj_a", wo, decision)
    finally:
        neo.close()

    assert new is not None and new["id"] != old["id"]
    row = store.get_approval(old["id"])
    assert (row["status"], row["closed_as"]) == ("expired", "superseded")
    [event] = store.events_of_kind(wo["id"], "gate_superseded")
    assert db.from_json(event["payload"], {})["approval_id"] == old["id"]
