"""A red default branch raises itself.

docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md.

THE INCIDENT, measured 2026-09-26: PR #765 (wo-15f5d969) auto-merged onto a stale base,
`main` went red on run 36273250195, and nothing in the OS noticed for ~4h — every
base-health fact it held was written per work order, by the heal path, only for an order
whose OWN pull request was failing. #765 had merged and left nothing parked, so there was
no order for that path to run on. The test that would have caught it is
`test_a_project_with_nothing_parked_and_auto_merge_off_still_learns_main_is_red`: no
parked pull request, `auto_merge` off, and the fact plus the finding still arrive.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import automerge, ci, db, gates, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import BASE_HEALTH_FRESH_SECONDS, CentralStore
from jarvis.daemon import Daemon
from jarvis.invariants import check_project
from jarvis.project_store import ProjectStore
from jarvis.testing import with_origin

PR = "https://github.com/acme/proj/pull/7"
REPO = "acme/proj"
JUDGED = "709582ae53000000000000000000000000000aaa"
#: The red commit ON `main` — a squash, so a different object from any judged head.
MAIN_RED_SHA = "36273250000000000000000000000000000fff1"


def run(run_id: int, sha: str, conclusion: str, started: str,
        workflow: str = "ci", status: str = "completed") -> dict:
    """One `gh run list` row. LOWER CASE conclusions, as the real CLI answers them."""
    return {"databaseId": run_id, "headSha": sha, "conclusion": conclusion,
            "status": status, "startedAt": started, "workflowName": workflow}


#: The incident's own run, and the recovery after it.
MAIN_RED = run(36273250195, MAIN_RED_SHA, "failure", "2026-09-26T21:35:00Z")
MAIN_GREEN = run(36273999999, "e080156", "success", "2026-09-26T23:00:00Z")
#: A second break, after the recovery.
MAIN_RED_AGAIN = run(36274111111, "abc1234", "failure", "2026-09-27T01:00:00Z")


def check(name: str, conclusion: str, started: str = "2026-09-26T21:40:00Z",
          workflow: str = "ci") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
            "conclusion": conclusion, "startedAt": started, "workflowName": workflow}


GREEN = [check("unit (3.13)", "SUCCESS"), check("evals", "SUCCESS")]
RED = [check("unit (3.13)", "FAILURE")]


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    with_origin(project, REPO)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def spec(started):
    return started.catalog.project("proj_a")


def poll(daemon, store):
    daemon.poll_default_branch(daemon.catalog.project("proj_a"), store)


def fact(central=None) -> dict:
    central = central or CentralStore()
    return json.loads(central.get_state("base_health:proj_a") or "{}")


def red_fleet(fake_gh, rows=(MAIN_RED,)):
    fake_gh.set_default_branch(REPO, "main")
    fake_gh.set_runs("main", list(rows))


def runs_called(fake_gh) -> int:
    return len([c for c in fake_gh.calls if c["argv"][:2] == ["run", "list"]])


def parked(project_path, *, pr_url: str = PR, judged: str = JUDGED,
           title: str = "add feature X"):
    """A work order finished behind a pull request, with a passed round on `judged`."""
    store = ProjectStore(project_path)
    wo = ops.create_work_order("proj_a", title)
    ops.finish(wo["id"], "opened a PR", pr_url=pr_url)
    store.update_work_order(wo["id"], session_id="sess-" + wo["id"])
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-" + wo["id"])
    store.set_validation_head(row["id"], judged)
    store.close_validation_round(row["id"], "passed", "")
    return store, store.get_work_order(wo["id"])


def opt_in(daemon):
    cfg = daemon.catalog.project("proj_a").validation
    cfg.enabled = True
    cfg.auto_merge = True


# -- 1. the poll ----------------------------------------------------------------------


def test_a_renamed_default_branch_is_re_read_rather_than_reported_green(
        started, project, fake_gh):
    """REVIEW FINDING 1. The cached name is the one thing about the fact that can go
    stale silently: `gh run list --branch main` on a repository renamed to `trunk`
    answers NO ROWS, no workflow has a red newest run, and the fact would be written
    `red: false` — the OS positively asserting a green base it never looked at, which is
    the one direction the spec forbids. An empty answer on a CACHED name re-reads it."""
    central = CentralStore()
    central.set_base_health("proj_a", {"red": False, "base": "main",
                                       "base_read_at": time.time(),
                                       "checked_at": time.time()})
    fake_gh.set_default_branch(REPO, "trunk")
    fake_gh.set_runs("trunk", [MAIN_RED])
    store = ProjectStore(project)

    poll(started, store)

    held = fact()
    assert held["base"] == "trunk"
    assert held["red"] is True and held["run_id"] == 36273250195
    # The retry is bounded: the name is read once and the runs twice, never in a loop.
    assert runs_called(fake_gh) == 2
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["api", "--method"]
                and c["argv"][-1] == ".default_branch"]) == 1


def test_an_empty_answer_on_a_freshly_read_name_is_not_re_read(started, project, fake_gh):
    """A project whose default branch has no CI at all is not a rename: one name read,
    one runs read, and a fact that is not red."""
    red_fleet(fake_gh, rows=())
    store = ProjectStore(project)

    poll(started, store)

    assert runs_called(fake_gh) == 1
    assert fact()["red"] is False and fact()["base"] == "main"


def test_a_project_with_nothing_parked_and_auto_merge_off_still_learns_main_is_red(
        started, project, fake_gh):
    """THE #793 SHAPE, and the test that would have caught the incident. No parked pull
    request, no `auto_merge`, and the fact plus the finding still arrive."""
    store = ProjectStore(project)
    assert store.list_work_orders() == []
    assert not started.catalog.project("proj_a").validation.auto_merge
    red_fleet(fake_gh)

    poll(started, store)

    held = fact()
    assert held["red"] is True and held["base"] == "main"
    assert held["workflow"] == "ci" and held["run_id"] == 36273250195
    assert held["run_url"] == ci.run_url("acme", "proj", 36273250195)
    assert held["head_sha"] == MAIN_RED_SHA
    found = [v for v in check_project(store, repair=False)
             if v.invariant == invariants.INV_BASE_RED]
    assert len(found) == 1 and found[0].wo_id is None and not found[0].repaired
    assert held["run_url"] in found[0].detail


def test_the_poll_costs_one_run_list_per_project_per_tick_and_nothing_else(
        started, project, fake_gh):
    """The whole added cost Neo costed: ~30 calls/hour/project."""
    red_fleet(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    assert runs_called(fake_gh) == 1
    # Two pinned GETs on this FIRST poll and neither again: the default branch name,
    # cached in the same fact, and the one attribution read the red transition buys.
    assert [c["argv"][:2] for c in fake_gh.calls
            if c["argv"][:2] != ["run", "list"]] == [["api", "--method"]] * 2

    poll(started, store)

    assert runs_called(fake_gh) == 2
    assert len([c for c in fake_gh.calls if c["argv"][:2] == ["api", "--method"]]) == 2


def test_a_project_with_no_origin_is_never_asked(started, project, fake_gh):
    """`github.origin_repo` is the step's gate: no remote, no call, no fact."""
    import subprocess

    subprocess.run(["git", "-C", str(project), "remote", "remove", "origin"], check=True)
    red_fleet(fake_gh)
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.calls == []
    assert fact() == {}


def test_a_gh_that_fails_writes_no_state_and_raises_nothing(started, project, fake_gh):
    """Unreadable means NOT KNOWN — never green and never red."""
    red_fleet(fake_gh)
    fake_gh.fail("gh: could not reach github.com")
    store = ProjectStore(project)

    poll(started, store)

    assert fact() == {}
    assert check_project(store, repair=False) == []


def test_a_green_default_branch_writes_a_fact_that_is_not_red(started, project, fake_gh):
    red_fleet(fake_gh, rows=(MAIN_GREEN,))
    store = ProjectStore(project)

    poll(started, store)

    assert fact()["red"] is False
    assert not [v for v in check_project(store, repair=False)
                if v.invariant == invariants.INV_BASE_RED]


def test_a_cancelled_newest_run_is_not_a_red_default_branch(started, project, fake_gh):
    """`ci.RED_RUN_CONCLUSIONS` — a human stopping a run says nothing about the code."""
    red_fleet(fake_gh, rows=(run(1, "abc", "cancelled", "2026-09-26T23:00:00Z"), MAIN_RED))
    store = ProjectStore(project)

    poll(started, store)

    assert fact()["red"] is False


def test_an_unfinished_newer_run_does_not_hide_the_red_completed_one(
        started, project, fake_gh):
    """Red iff any workflow's newest COMPLETED run is red."""
    red_fleet(fake_gh, rows=(run(2, "abc", "", "2026-09-26T23:00:00Z", status="in_progress"),
                             MAIN_RED))
    store = ProjectStore(project)

    poll(started, store)

    assert fact()["red"] is True and fact()["run_id"] == 36273250195


def test_the_poll_runs_before_the_pull_request_poll_on_the_pr_cadence(
        started, project, fake_gh, monkeypatch):
    """Same tick, BEFORE `poll_pull_requests`, so this tick's `auto_merge` reads a
    fresh fact rather than the previous interval's."""
    order: list[str] = []
    monkeypatch.setattr(Daemon, "poll_default_branch",
                        lambda self, p, s: order.append("base"))
    monkeypatch.setattr(Daemon, "poll_pull_requests",
                        lambda self, p, s: order.append("prs"))
    started.tick_count = 0      # the next tick is the PR-poll one

    started.tick()

    assert order == ["base", "prs"]


# -- 2. the fact, the rows and the attribution ----------------------------------------


def test_the_red_commit_is_attributed_through_the_squash_subject(
        started, project, fake_gh):
    """The squash subject's `(#N)`, resolved to an order that carries
    `automerge_merged` — the row says "the OS merged this", so it must be true."""
    store, wo = parked(project)
    store.add_event(wo["id"], "automerge_merged", {"head_sha": JUDGED})
    red_fleet(fake_gh)
    fake_gh.set_commit_subject(MAIN_RED_SHA, "[wo-x] Attribution is not a choice (#7)")

    poll(started, store)

    assert fact()["wo_id"] == wo["id"]
    found = [v for v in check_project(store, repair=False)
             if v.invariant == invariants.INV_BASE_RED]
    assert wo["id"] in found[0].detail


def test_the_last_pull_request_number_in_the_subject_is_the_merged_one(
        started, project, fake_gh):
    """Production shape: `… (#749) (#765)` — the squash's own number is the LAST."""
    store, wo = parked(project)
    store.add_event(wo["id"], "automerge_merged", {"head_sha": JUDGED})
    red_fleet(fake_gh)
    fake_gh.set_commit_subject(MAIN_RED_SHA, "[wo-x] a fix (#749) (#7)")

    poll(started, store)

    assert fact()["wo_id"] == wo["id"]


def test_an_order_outside_the_listing_window_is_still_attributed(
        started, project, fake_gh):
    """REVIEW FINDING 2. `list_work_orders` is `created_at DESC LIMIT 200`, so on a
    mature project the order that merged five minutes ago — created months ago — is not
    in the window and would be named nobody. The lookup is by pull-request NUMBER."""
    store, wo = parked(project)
    store.add_event(wo["id"], "automerge_merged", {"head_sha": JUDGED})
    # Older than every other order, so `created_at DESC LIMIT 200` provably excludes it.
    store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?", (1.0, wo["id"]))
    for n in range(200):
        store.create_work_order(title=f"filler {n}")
    assert wo["id"] not in [w["id"] for w in store.list_work_orders(include_hidden=True)]
    red_fleet(fake_gh)
    fake_gh.set_commit_subject(MAIN_RED_SHA, "a fix (#7)")

    poll(started, store)

    assert fact()["wo_id"] == wo["id"]


def test_the_pull_request_lookup_never_matches_a_number_by_prefix(project):
    """`/pull/7` must not claim `/pull/17` or `/pull/70`."""
    store = ProjectStore(project)
    for url in ("https://github.com/acme/proj/pull/17",
                "https://github.com/acme/proj/pull/70"):
        wo = store.create_work_order(title=url)
        store.update_work_order(wo["id"], pr_url=url)

    assert store.work_orders_for_pr_number(7) == []
    assert [w["pr_url"] for w in store.work_orders_for_pr_number(17)] \
        == ["https://github.com/acme/proj/pull/17"]


def test_an_unreadable_subject_names_no_work_order(started, project, fake_gh):
    """A red `main` with no attribution is still the row the user needs."""
    store, wo = parked(project)
    store.add_event(wo["id"], "automerge_merged", {"head_sha": JUDGED})
    red_fleet(fake_gh)   # no subject registered: the commit read 404s

    poll(started, store)

    held = fact()
    assert held["red"] is True and held["wo_id"] == ""


def test_an_order_without_an_automerge_merged_event_is_not_claimed(
        started, project, fake_gh):
    """The user merged it by hand: the row must not say the OS did."""
    store, _ = parked(project)
    red_fleet(fake_gh)
    fake_gh.set_commit_subject(MAIN_RED_SHA, "a fix (#7)")

    poll(started, store)

    assert fact()["wo_id"] == ""


def test_attribution_is_never_by_comparing_the_judged_sha_with_the_base_run_sha(
        started, project, fake_gh):
    """THE REGRESSION THIS SPEC EXISTS TO PREVENT. `automerge_merged.head_sha` is the
    PULL REQUEST's head and the merge is `--squash`, so the commit on `main` is a
    different object — the two never compare equal, and an implementation that matched
    on them would attribute nothing in production and everything here."""
    store, wo = parked(project)
    store.add_event(wo["id"], "automerge_merged", {"head_sha": MAIN_RED_SHA})
    red_fleet(fake_gh)
    fake_gh.set_commit_subject(MAIN_RED_SHA, "a fix with no number in it")

    poll(started, store)

    assert fact()["wo_id"] == ""


def test_the_commit_is_read_only_on_the_red_transition(started, project, fake_gh):
    """One extra `gh` call per red transition, never per tick."""
    store, wo = parked(project)
    store.add_event(wo["id"], "automerge_merged", {"head_sha": JUDGED})
    red_fleet(fake_gh)
    fake_gh.set_commit_subject(MAIN_RED_SHA, "a fix (#7)")

    poll(started, store)
    subjects = [c for c in fake_gh.calls if "--jq" in c["argv"]
                and c["argv"][c["argv"].index("--jq") + 1] == ".commit.message"]
    assert len(subjects) == 1

    poll(started, store)
    poll(started, store)
    assert len([c for c in fake_gh.calls if "--jq" in c["argv"]
                and c["argv"][c["argv"].index("--jq") + 1] == ".commit.message"]) == 1


# -- 3. the invariant and the announcements -------------------------------------------


def test_the_invariant_is_derived_and_makes_no_subprocess(started, project, fake_gh):
    """An invariant that shelled out to `gh` would put a subprocess behind
    `jarvis wo list` — `invariants.base_red_note`'s rule."""
    red_fleet(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    fake_gh.fail("gh: no network")
    before = list(fake_gh.calls)

    found = [v for v in check_project(store, repair=False)
             if v.invariant == invariants.INV_BASE_RED]

    assert len(found) == 1
    assert fake_gh.calls == before


def test_the_break_is_announced_once_across_repeated_reconcile_passes(
        started, spec, project, fake_gh):
    """One writer per direction: the unrepaired violation's notification IS the red
    row, deduped by `open_violation_report`."""
    red_fleet(fake_gh)
    store = ProjectStore(project)

    poll(started, store)
    for _ in range(3):
        started.check_invariants(spec, store)

    rows = [n for n in store.unrouted_notifications()
            if invariants.INV_BASE_RED in n["title"]]
    assert len(rows) == 1 and rows[0]["level"] == "warning"


def test_the_green_transition_announces_recovery_and_re_arms_the_report(
        started, spec, project, fake_gh):
    """`close_violation_report` for THIS key only, so a break-recover-break inside the
    hourly landing sweep window announces twice rather than once."""
    red_fleet(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    started.check_invariants(spec, store)

    fake_gh.set_runs("main", [MAIN_GREEN])
    poll(started, store)
    started.check_invariants(spec, store)

    recovered = [n for n in store.unrouted_notifications() if n["level"] == "info"
                 and "main" in n["title"] + n["body"]]
    assert len(recovered) == 1
    assert not [r for r in store.violation_reports()
                if r["invariant"] == invariants.INV_BASE_RED]

    # A SECOND break, newer than the recovery — `ci.latest` sorts by `startedAt`, so a
    # re-registered older run is not a new break.
    fake_gh.set_runs("main", [MAIN_RED_AGAIN, MAIN_GREEN, MAIN_RED])
    poll(started, store)
    started.check_invariants(spec, store)

    warnings = [n for n in store.unrouted_notifications()
                if invariants.INV_BASE_RED in n["title"]]
    assert len(warnings) == 2


def test_closing_one_report_leaves_every_other_standing(project):
    """Why the plural sweep version is unusable here: it DELETES every report not in
    the iterable it is given."""
    store = ProjectStore(project)
    store.open_violation_report(invariants.INV_BASE_RED)
    store.open_violation_report("INV-SOMETHING-ELSE", "wo-1")

    assert store.close_violation_report(invariants.INV_BASE_RED) is True
    assert store.close_violation_report(invariants.INV_BASE_RED) is False

    assert [(r["invariant"], r["wo_id"]) for r in store.violation_reports()] \
        == [("INV-SOMETHING-ELSE", "wo-1")]


def test_a_still_red_base_writes_no_second_row_on_the_next_three_polls(
        started, spec, project, fake_gh):
    red_fleet(fake_gh)
    store = ProjectStore(project)

    for _ in range(4):
        poll(started, store)
        started.check_invariants(spec, store)

    assert len([n for n in store.unrouted_notifications()
                if invariants.INV_BASE_RED in n["title"]]) == 1


# -- the merge pause, wired ------------------------------------------------------------


def test_a_fresh_red_fact_pauses_a_parked_merge(started, spec, project, fake_gh):
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    red_fleet(fake_gh)

    poll(started, store)
    started.poll_pull_requests(spec, store)

    assert store.list_approvals(wo["id"]) == []
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_BASE_RED


def test_a_stale_reading_never_pauses_a_merge(started, spec, project, fake_gh, local_base):
    """A `gh` that stopped answering must not pause the fleet's merges for ever on a
    fact nobody can confirm."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    red_fleet(fake_gh)
    poll(started, store)
    central = CentralStore()
    stale = {**fact(central),
             "checked_at": time.time() - BASE_HEALTH_FRESH_SECONDS - 1}
    central.set_state("base_health:proj_a", json.dumps(stale))

    started.poll_pull_requests(spec, store)

    assert len(store.list_approvals(wo["id"])) == 1


def test_the_stored_fact_is_read_once_per_project_per_poll(started, spec, project,
                                                           fake_gh, monkeypatch):
    """REVIEW FINDING 3. It is the same fact for every order in the loop, so it is read
    once per project per poll and passed down — not once per parked order, which is a
    central read the poll's stated budget does not name."""
    opt_in(started)
    store, one = parked(project, pr_url=PR, title="one")
    _, two = parked(project, pr_url=PR.replace("/7", "/8"), judged=JUDGED, title="two")
    for url in (PR, PR.replace("/7", "/8")):
        fake_gh.set_pr(url, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    red_fleet(fake_gh)
    poll(started, store)
    reads: list[str] = []
    real = Daemon._base_red
    monkeypatch.setattr(Daemon, "_base_red",
                        lambda self, p: (reads.append(p.name), real(self, p))[1])

    started.poll_pull_requests(spec, store)

    assert reads == ["proj_a"]
    for wo in (one, two):
        held = store.events_of_kind(wo["id"], "automerge_held")
        assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_BASE_RED


def test_an_absent_fact_never_pauses_a_merge(started, spec, project, fake_gh, local_base):
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)

    started.poll_pull_requests(spec, store)

    assert len(store.list_approvals(wo["id"])) == 1


def test_the_pause_is_a_pause_and_not_a_stall(started, spec, project, fake_gh, local_base):
    """END TO END, §Tests item 5: a parked order held on a red `main`, `main` goes
    green, and the merge proceeds. A held merge that never resumed would be the same
    stall wearing a better label."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    red_fleet(fake_gh)

    poll(started, store)                        # 1. `main` is red: the merge pauses
    started.poll_pull_requests(spec, store)
    assert store.list_approvals(wo["id"]) == []

    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    poll(started, store)                        # 2. `main` recovers: the merge arms
    started.poll_pull_requests(spec, store)
    approvals = store.list_approvals(wo["id"])
    assert len(approvals) == 1 and approvals[0]["kind"] == "auto_merge"
    gates.apply_decision(store, approvals[0]["id"], "approved", "ok", "neo",
                         project="proj_a")

    started.poll_pull_requests(spec, store)     # 3. it merges

    assert [c["argv"][2] for c in fake_gh.calls
            if c["argv"][:2] == ["pr", "merge"]] == [PR]
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_a_red_base_spends_no_round_number_on_a_moved_head(
        started, spec, project, fake_gh):
    """`HELD_BASE_RED` sits before the sha checks, so no round is spent re-judging a
    branch whose CI is inheriting `main`'s failure."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=RED, merge_state="CLEAN", head_oid="f00dfeed0000")
    red_fleet(fake_gh)
    before = store.latest_validation_round(wo_id=wo["id"])

    poll(started, store)
    started.poll_pull_requests(spec, store)

    assert store.latest_validation_round(wo_id=wo["id"]) == before
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_BASE_RED
    # The rider: this pull request's own `ci` failure is the same break.
    assert "not its fault" in db.from_json(held[-1]["payload"], {})["reason"]
