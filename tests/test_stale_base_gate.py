"""A merge request that names the base it was filed against, and is withdrawn when it moves (docs/superpowers/specs/2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.5)"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis import automerge, db, gates, issues, ops
from jarvis.catalog import PullRequestsConfig, load_catalog
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
    # The window cap alone: the livelock guard (#1512) stops this loop at fewer laps
    policy = PullRequestsConfig(livelock_laps=CATCH_UP_MAX + 1)
    monkeypatch.setattr(ops, "catch_up_policy", lambda store: policy)
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


# -- #1459: one merge request in flight per base tip

PR_2 = "https://github.com/acme/proj/pull/8"
JUDGED_2 = "a650fd2c98000000000000000000000000000ccc"


def two_parked(project_path, fake_gh):
    store, first = parked(project_path)
    second = ops.create_work_order("proj_a", "add feature Y")
    ops.finish(second["id"], "opened a PR", pr_url=PR_2)
    row = store.open_validation_round(wo_id=second["id"], fingerprint="fp-2")
    store.set_validation_head(row["id"], JUDGED_2)
    store.close_validation_round(row["id"], "passed", "")
    green_pr(fake_gh)
    fake_gh.set_pr(PR_2, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED_2, base_oid=BASE_1)
    return store, first, store.get_work_order(second["id"])


def leader(store, *wos):
    """(the order that filed, the one queued behind it) — the poll's order is not this test's"""
    filed = [w for w in wos if store.list_approvals(w["id"])]
    assert len(filed) == 1, [w["id"] for w in filed]
    return filed[0], next(w for w in wos if w is not filed[0])


def test_a_second_pull_request_on_the_same_base_waits_for_the_first(
        started, project, fake_gh, local):
    """#1459: N armed PRs file ONE Neo question, not N"""
    opt_in(started)
    store, first, second = two_parked(project, fake_gh)

    poll(started, store)

    first, second = leader(store, first, second)
    [request] = store.list_approvals(first["id"])
    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(second["id"], "automerge_held")]
    assert held[-1]["code"] == automerge.HELD_MERGE_QUEUED
    assert first["id"] in held[-1]["reason"]

    gates.apply_decision(store, request["id"], "denied", "no", "neo", project="proj_a")
    poll(started, store)

    assert len(store.list_approvals(second["id"])) == 1


def test_a_branch_behind_the_base_is_not_caught_up_while_another_merge_is_in_flight(
        started, project, fake_gh, local):
    """#1459: the base-moved hold queues before the catch-up, so no catch-up CI is wasted"""
    opt_in(started)
    store, first, second = two_parked(project, fake_gh)
    store.add_approval(first["id"], gates.AUTO_MERGE, automerge.merge_command(PR, JUDGED),
                       max_uses=1, base_oid=BASE_2)
    # The leader's checks still run, so it files nothing new this tick
    green_pr(fake_gh, base_oid=BASE_2, checks=[check("unit", "", "IN_PROGRESS")])
    fake_gh.set_pr(PR_2, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED_2,
                   base_oid=BASE_2)
    local.update({"tip": BASE_2, "contains": {BASE_1}})

    poll(started, store)

    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(second["id"], "automerge_held")]
    assert held[-1]["code"] == automerge.HELD_MERGE_QUEUED
    assert PR_2 not in fake_gh.updates
    assert store.list_approvals(second["id"]) == []


# -- #1608: the queue's order survives the merge ahead


PR_3 = "https://github.com/acme/proj/pull/9"
JUDGED_3 = "b761ae3d09000000000000000000000000000ddd"


def three_queued(project_path, fake_gh, daemon):
    """A filed, B then C queued behind it on BASE_1, by a real poll each"""
    store, first, second = two_parked(project_path, fake_gh)
    poll(daemon, store)
    first, second = leader(store, first, second)
    third = ops.create_work_order("proj_a", "add feature Z")
    ops.finish(third["id"], "opened a PR", pr_url=PR_3)
    row = store.open_validation_round(wo_id=third["id"], fingerprint="fp-3")
    store.set_validation_head(row["id"], JUDGED_3)
    store.close_validation_round(row["id"], "passed", "")
    fake_gh.set_pr(PR_3, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED_3,
                   base_oid=BASE_1)
    poll(daemon, store)
    second, third = sorted((second, store.get_work_order(third["id"])), key=lambda w:
                           (store.events_of_kind(w["id"], "automerge_held")[0]["ts"], w["id"]))
    return store, first, second, third


def land(fake_gh, local, leader_wo, waiters, tip=BASE_2):
    """The leader merges: main moves to `tip` and every waiter is behind it"""
    fake_gh.set_pr(leader_wo["pr_url"], "MERGED", checks=GREEN, merge_state="CLEAN",
                   head_oid=automerge_head(leader_wo), base_oid=tip)
    for wo in waiters:
        fake_gh.set_pr(wo["pr_url"], "OPEN", checks=GREEN, merge_state="CLEAN",
                       head_oid=automerge_head(wo), base_oid=tip)
    fake_gh.base_sha(tip)
    local.update({"tip": tip, "contains": {BASE_1}})


def automerge_head(wo):
    return {PR: JUDGED, PR_2: JUDGED_2, PR_3: JUDGED_3}[wo["pr_url"]]


def holds(store, wo):
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]


def test_only_the_front_of_the_queue_catches_up_when_the_leader_merges(
        started, project, fake_gh, local):
    """#1608: one catch-up for the next in line, none for the order behind it, then its turn"""
    opt_in(started)
    store, first, second, third = three_queued(project, fake_gh, started)
    land(fake_gh, local, first, (second, third))

    poll(started, store)

    assert fake_gh.updates.count(second["pr_url"]) == 1
    assert third["pr_url"] not in fake_gh.updates
    assert holds(store, third)[-1]["queued_behind"] == second["id"]
    assert ops.base_chase_laps(store, third["id"]) == []

    land(fake_gh, local, second, (third,), tip=BASE_3)
    poll(started, store)
    poll(started, store)    # the poll's order is not this test's: the merge may close second

    assert fake_gh.updates.count(second["pr_url"]) == 1
    assert fake_gh.updates.count(third["pr_url"]) == 1


def test_the_front_gives_up_its_turn_when_its_ci_fails(started, project, fake_gh, local):
    """#1608: a red front order never stalls the queue behind it"""
    opt_in(started)
    store, _, second, third = three_queued(project, fake_gh, started)
    store.add_event(second["id"], "automerge_held",
                    {"code": automerge.HELD_CHECKS_FAILED, "head_sha": automerge_head(second)})

    assert automerge.queue_entry(store, second) is None
    assert automerge.queue_entry(store, third) is not None


def test_a_wedged_front_is_passed_over_and_the_user_is_told_once(
        started, project, fake_gh, local, monkeypatch):
    """#1608, Neo Q3672: the wedge timer never rotates a turn silently"""
    opt_in(started)
    store, first, second, third = three_queued(project, fake_gh, started)
    land(fake_gh, local, first, (second, third))
    store.add_event(second["id"], "automerge_held",
                    {"code": automerge.HELD_CHECKS_RUNNING, "head_sha": JUDGED_2})
    pr = fake_gh_pr(third, BASE_2)
    assert automerge.merge_queued(store, third, pr).queued_behind == second["id"]
    monkeypatch.setattr(gates, "GRANT_TTL_SECONDS", -1)

    assert automerge.merge_queued(store, third, pr) is None
    started._merge_queued(started.catalog.project("proj_a"), store, third, pr)
    started._merge_queued(started.catalog.project("proj_a"), store, third, pr)

    assert len(store.events_of_kind(second["id"], automerge.TURN_PASSED_OVER_EVENT)) == 1
    assert [row for row in started.central.unacked_inbox()
            if row["title"] == f"The merge queue passed over {second['id']}"]


def fake_gh_pr(wo, base_oid):
    return SimpleNamespace(base_oid=base_oid, base_ref="main", head_oid=automerge_head(wo))


def test_a_stale_queued_front_is_passed_over_only_once_the_order_it_named_stopped_moving(
        started, project, fake_gh, local, clock):
    """#1608: a queued hold never wedges while it waits, and cannot stall the queue for ever"""
    opt_in(started)
    with clock.moved(-3 * gates.GRANT_TTL_SECONDS):
        store, first, second, third = three_queued(project, fake_gh, started)
    pr = fake_gh_pr(third, BASE_2)

    assert automerge.merge_queued(store, third, pr) is None
    assert [e.wo_id for e in automerge.passed_over(store, third, pr)] == [second["id"]]

    store.add_event(first["id"], "pr_merged", {})

    assert automerge.merge_queued(store, third, pr).queued_behind == second["id"]
    assert automerge.passed_over(store, third, pr) == []


def another_parked(store, url, judged, fake_gh, base_oid=BASE_1):
    wo = ops.create_work_order("proj_a", f"add feature for {url}")
    ops.finish(wo["id"], "opened a PR", pr_url=url)
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-" + wo["id"])
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    fake_gh.set_pr(url, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=judged,
                   base_oid=base_oid)
    return store.get_work_order(wo["id"])


def test_a_red_main_voids_the_queue_so_the_fix_is_not_held_behind_it(
        started, project, fake_gh, local, monkeypatch):
    """#1608, Neo Q3672: `base_red` empties the queue, and the fix for it files its merge"""
    opt_in(started)
    store, first, second, third = three_queued(project, fake_gh, started)
    land(fake_gh, local, first, (second, third))
    local["contains"] = {BASE_1, BASE_2}
    issue = "https://github.com/acme/proj/issues/1"
    monkeypatch.setattr(Daemon, "_base_red", lambda self, p: automerge.BaseRed(
        base="main", workflow="ci", run_url="https://run", head_sha=BASE_2,
        issue_url=issue, issue_os_filed=True))

    poll(started, store)

    for wo in (second, third):
        assert holds(store, wo)[-1]["code"] == automerge.HELD_BASE_RED
        assert automerge.queue_entry(store, wo) is None

    url = "https://github.com/acme/proj/pull/10"
    judged = "c872bf4e1a000000000000000000000000000eee"
    fix = another_parked(store, url, judged, fake_gh, base_oid=BASE_2)
    store.conn.execute("UPDATE work_orders SET issue_url=? WHERE id=?", (issue, fix["id"]))
    fix = store.get_work_order(fix["id"])
    assert automerge.merge_queued(store, fix, SimpleNamespace(
        base_oid=BASE_2, base_ref="main", head_oid=judged)) is None

    poll(started, store)

    assert len(store.list_approvals(fix["id"])) == 1
    assert [h for h in holds(store, fix) if h["code"] == automerge.HELD_MERGE_QUEUED] == []


def test_an_expedited_order_ignores_queue_position_but_not_a_lease(
        started, project, fake_gh, local):
    """#1608: expediting is the user's; a same-tip merge in flight still comes first"""
    opt_in(started)
    store, _, second, _ = three_queued(project, fake_gh, started)
    url = "https://github.com/acme/proj/pull/10"
    judged = "c872bf4e1a000000000000000000000000000eee"
    plain = another_parked(store, url, judged, fake_gh)
    expedited = {**plain, "metadata": db.to_json({issues.EXPEDITED_KEY: True})}
    moved = SimpleNamespace(base_oid=BASE_2, base_ref="main", head_oid=judged)
    same_tip = SimpleNamespace(base_oid=BASE_1, base_ref="main", head_oid=judged)

    assert automerge.merge_queued(store, plain, moved).queued_behind == second["id"]
    assert automerge.merge_queued(store, expedited, moved) is None
    assert automerge.merge_queued(store, expedited, same_tip).code == automerge.HELD_MERGE_QUEUED


def _denied(store, wo):
    request = store.add_approval(wo["id"], gates.AUTO_MERGE,
                                 automerge.merge_command(wo["pr_url"], automerge_head(wo)),
                                 max_uses=1, base_oid=BASE_2)
    store.decide_approval(request["id"], "denied", "no", "neo")


def _escalated(store, wo):
    request = store.add_approval(wo["id"], gates.AUTO_MERGE,
                                 automerge.merge_command(wo["pr_url"], automerge_head(wo)),
                                 max_uses=1, base_oid=BASE_2)
    store.mark_approval_escalated(request["id"], "the user decides")


def _base_moved(**fields):
    def held(store, wo):
        store.add_event(wo["id"], "automerge_held", {
            "code": automerge.HELD_BASE_MOVED, "head_sha": automerge_head(wo), **fields})
    return held


@pytest.mark.parametrize("give_up", [
    _denied, _escalated,
    _base_moved(catch_up=ops.CATCH_UP_EXHAUSTED),
    _base_moved(catch_up=ops.CATCH_UP_FAILED),
    _base_moved(catch_up=ops.CATCH_UP_DEFERRED,
                catch_up_cause=ops.CATCH_UP_DEFER_CARRY_UNPROVEN),
], ids=["denied", "escalated", "exhausted", "failed", "carry_unproven"])
def test_a_front_order_that_cannot_merge_gives_up_its_turn(
        started, project, fake_gh, local, give_up):
    """#1608: the orders behind a dead front order never wait on it"""
    opt_in(started)
    store, _, second, third = three_queued(project, fake_gh, started)
    pr = fake_gh_pr(third, BASE_2)
    assert automerge.merge_queued(store, third, pr).queued_behind == second["id"]

    give_up(store, second)

    assert automerge.queue_entry(store, second) is None
    assert automerge.merge_queued(store, third, pr) is None


def test_a_front_order_catching_up_keeps_its_turn(started, project, fake_gh, local):
    """#1608: a catch-up in progress is the turn being used, not given up"""
    opt_in(started)
    store, _, second, third = three_queued(project, fake_gh, started)

    _base_moved(catch_up=ops.CATCH_UP_DONE)(store, second)

    assert automerge.merge_queued(store, third, fake_gh_pr(third, BASE_2)
                                  ).queued_behind == second["id"]


def test_two_lost_turns_are_reported_by_the_invariant(started, project, fake_gh, local):
    """#1608: the detector counts a lap right after a queue hold on an order that merged"""
    from jarvis import invariants

    store, first, second = two_parked(project, fake_gh)
    store.add_event(first["id"], "pr_merged", {})
    for _ in range(2):
        store.add_event(second["id"], "automerge_held",
                        {"code": automerge.HELD_MERGE_QUEUED, "queued_behind": first["id"]})
        store.add_event(second["id"], invariants.PR_BASE_UPDATED_EVENT,
                        {"cause": ops.BASE_UPDATE_BEHIND, "base_sha": BASE_2})

    [violation] = list(invariants.check_queue_turn_survives_merge(store))

    assert violation.wo_id == second["id"]
    assert violation.invariant == "INV-BASE-CHASE-LIVELOCK"


# -- #1513: a grant that lapses unspent is re-requested


def test_a_grant_that_lapses_unspent_is_filed_again(started, project, fake_gh, local):
    """#1513 / gate 821: the expired grant authorised nothing, so the same head is proposed afresh"""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [first] = store.list_approvals(wo["id"])
    gates.apply_decision(store, first["id"], "approved", "ok", "neo", project="proj_a")
    store.conn.execute("UPDATE approvals SET expires_at=? WHERE id=?",
                       (db.now() - 1, first["id"]))
    store.expire_approvals()
    assert automerge.lapsed_unspent(store.get_approval(first["id"]))

    poll(started, store)
    poll(started, store)

    assert merges(fake_gh) == []
    approvals = store.list_approvals(wo["id"])
    assert len(approvals) == 2
    assert approvals[0]["status"] == "pending" and approvals[0]["command"] == first["command"]
    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]
    assert [h["code"] for h in held].count(automerge.HELD_GRANT_LAPSED) == 1
    assert str(first["id"]) in held[-1]["reason"]


def test_an_escalated_request_holds_no_lease(started, project, fake_gh, local):
    """Neo Q3020: a request waiting on the user does not stall the queue"""
    opt_in(started)
    store, first, second = two_parked(project, fake_gh)
    poll(started, store)
    first, second = leader(store, first, second)
    [request] = store.list_approvals(first["id"])
    store.mark_approval_escalated(request["id"], "the user decides")

    poll(started, store)

    assert len(store.list_approvals(second["id"])) == 1


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
