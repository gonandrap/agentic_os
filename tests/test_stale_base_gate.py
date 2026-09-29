"""A merge request that names the base it was filed against, and is withdrawn when it moves.

docs/superpowers/specs/2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.5 and §3.6,
GitHub issue #837. Its own file rather than more of `tests/test_automerge.py`: these are
about the APPROVALS LEDGER — what a pending request records and what closes it — rather
than about the branch.

THE MEASURED INCIDENT IS THE FIRST TEST. Gate 308 authorised a squash of PR #829 whose
judged head did not contain `main`'s tip, and it got there because `pr.base_oid` — GitHub's
cached `baseRefOid` — lagged the real tip by three commits and 5.3 hours.
"""

from __future__ import annotations

import pytest

from jarvis import automerge, db, gates, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"

#: The commit round 1 judged, the base tip it contains, and the tip it does NOT.
JUDGED = "709582ae53000000000000000000000000000aaa"
BASE_1 = "8ea18feec4000000000000000000000000000821"
BASE_2 = "37fe650fab000000000000000000000000000827"
#: Where `origin/main` REALLY is when GitHub is still reporting BASE_2.
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
    """The shared checkout fake (`testing.local_base`), at this file's base commit."""
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


# -- the incident ---------------------------------------------------------------------


def test_gate_308_a_head_that_contains_only_githubs_stale_base_is_never_proposed(
        started, project, fake_gh, local):
    """THE REGRESSION, issue #837 / gate 308. The judged head contains the base commit
    `pr.base_oid` reports (B1); `origin/main` is really at B2, a descendant of B1 the head
    does not contain. Nothing may be proposed and nothing may be merged."""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh, base_oid=BASE_1)         # GitHub's cached reading: 5.3h behind
    local.update({"tip": BASE_2, "contains": {BASE_1}})

    poll(started, store)

    assert store.list_approvals(wo["id"]) == []
    assert merges(fake_gh) == []


def test_a_base_that_moves_between_the_verdict_and_the_merge_refuses_and_catches_up(
        started, project, fake_gh, local):
    """§3.1's daemon arm: the grant is live, the head has not moved, and `main` has. The
    merge never reaches GitHub, the refusal is on the record and the branch is caught up.
    """
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
    """THE LIVELOCK. The pre-`decide` fact is UNFETCHED, so a checkout that does not have
    `pr.base_oid` at all cannot answer the ancestry question — and `ops.catch_up_needed`
    reads "cannot answer" as BEHIND, on every tick. The fetched read is authoritative: the
    tip IS contained, so the tick re-decides without the stale fact and carries on.

    Holding on the cheap read instead would park a mergeable pull request for ever, and
    write nothing saying why. The safety property is untouched — `apply` fetches too.

    THE REQUEST MUST SURVIVE THE RE-DECIDE and the merge must actually land: §3.6's
    supersede reads the cheap fact too, so an over-report there withdraws a healthy
    pending request and blocks its command string for ever."""
    opt_in(started)
    store, wo = parked(project)
    # GitHub reports a base commit this checkout has never seen; `origin/main` is at BASE_1
    # and the judged head contains it.
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


# -- §3.5: the base is ON the request -------------------------------------------------


def test_the_request_records_and_states_the_base_it_was_filed_against(
        started, project, fake_gh, local):
    """§3.5: a column, the `automerge_proposed` payload, and the sentence the reviewer
    reads — which now names the check that will catch the base if it moves again."""
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
    """§3.5 through `ADDED_COLUMNS` + `_migrate`, this repo's one mechanism: an approval
    written before the column existed reads as `''` — "this request records no base"."""
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


# -- §3.6: a pending request whose base moved is superseded ----------------------------


def test_a_pending_request_whose_base_moved_is_superseded_and_never_answered(
        started, project, fake_gh, local):
    """§3.6: nothing is authorised and nothing is refused. The command string stays
    blocked, so the next request is a real review of the commit that will actually land."""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)
    poll(started, store)
    [approval] = store.list_approvals(wo["id"])
    assert approval["status"] == "pending"

    # GitHub says BASE_2; the checkout, after fetching, says BASE_3 — the commit the merge
    # would really land on.
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                   base_oid=BASE_2)
    local["tip"] = BASE_3
    poll(started, store)

    row = store.get_approval(approval["id"])
    assert row["status"] == "expired" and row["closed_as"] == "superseded"
    # The recorded base of the request, and the FETCHED tip — never `pr.base_oid`: §3.1's
    # whole point is that GitHub's cached read is untrustworthy.
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
    """§3.6's race, and why it is safe: Neo can answer between the read and the write.
    `supersede_approval` no-ops on a decided row, and §3.1's precondition is then what
    stops the merge."""
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
        started, project, fake_gh, local):
    """§3.6's three bounds, driven on a base that moves on every tick. The ceiling is
    designed: `ops.CATCH_UP_MAX` updates, then a hold that names the cap and asks the
    user — never an endless pair of requests nobody can answer."""
    opt_in(started)
    store, wo = parked(project)
    green_pr(fake_gh)

    for i in range(ops.CATCH_UP_MAX + 2):
        tip = f"{i}" * 40
        fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED,
                       base_oid=tip)
        fake_gh.base_sha(tip)
        fake_gh.updated_head(JUDGED)           # the head stays judged, the base keeps moving
        local.update({"tip": tip, "contains": set()})
        poll(started, store)

    assert len(fake_gh.updates) <= ops.CATCH_UP_MAX
    held = [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "automerge_held")]
    assert held and f"the cap ({ops.CATCH_UP_MAX})" in held[-1]["reason"]
    assert merges(fake_gh) == []
