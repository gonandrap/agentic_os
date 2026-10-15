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
import subprocess
import time

import pytest

from jarvis import automerge, bugreport, ci, db, gates, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import BASE_HEALTH_FRESH_SECONDS, CentralStore
from jarvis.daemon import Daemon
from jarvis.invariants import check_project
from jarvis.project_store import TERMINAL_STATUSES, ProjectStore
from jarvis.testing import FIXTURE_BUG_REPO, with_origin

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
    fresh fact rather than the previous interval's. The dispatch sweep sits BETWEEN them
    (spec 2026-10-14-a-red-main-dispatches-its-own-fix.md §2): after the poll, so it
    reads this tick's reading, and before the PR poll, so the `fix_wo_id` it writes names
    the order in the same tick's holds."""
    order: list[str] = []
    monkeypatch.setattr(Daemon, "poll_default_branch",
                        lambda self, p, s: order.append("base"))
    monkeypatch.setattr(Daemon, "dispatch_base_fix",
                        lambda self, p, s: order.append("fix"))
    monkeypatch.setattr(Daemon, "poll_pull_requests",
                        lambda self, p, s: order.append("prs"))
    started.tick_count = 0      # the next tick is the PR-poll one

    started.tick()

    assert order == ["base", "fix", "prs"]


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


# -- the dispatch (spec 2026-10-14-a-red-main-dispatches-its-own-fix.md) --------------

#: The failing run's url as a worker-filed issue on the tracker would quote it.
TRACKER_RUN_URL = ci.run_url(*FIXTURE_BUG_REPO.split("/"), 36273250195)
TRACKER_PR = f"https://github.com/{FIXTURE_BUG_REPO}/pull/9"


@pytest.fixture()
def tracker(started, project, fake_gh):
    """`proj_a`'s git `origin` IS `bugreport.bug_repo()` — §1's whole gate."""
    subprocess.run(["git", "-C", str(project), "remote", "set-url", "origin",
                    f"https://github.com/{FIXTURE_BUG_REPO}.git"], check=True)
    fake_gh.set_default_branch(FIXTURE_BUG_REPO, "main")
    fake_gh.set_runs("main", [MAIN_RED])
    return started


def sweep(daemon, store):
    daemon.dispatch_base_fix(daemon.catalog.project("proj_a"), store)


def filings(fake_gh) -> int:
    return len([c for c in fake_gh.calls if c["argv"][:2] == ["issue", "create"]])


def orders(store) -> list[str]:
    return [w["id"] for w in store.list_work_orders(include_hidden=True)]


def test_a_project_that_is_not_the_tracker_dispatches_nothing(started, project, fake_gh):
    """§1: every other project stays notify-only, byte-identical to today."""
    red_fleet(fake_gh)
    store = ProjectStore(project)
    poll(started, store)
    before = len(fake_gh.calls)

    sweep(started, store)

    assert [c for c in fake_gh.calls[before:] if c["argv"][:1] == ["issue"]] == []
    assert orders(store) == []
    held = fact()
    assert held["red"] is True
    assert not held.get("issue_url") and not held.get("fix_wo_id")


def test_a_second_red_run_joins_the_order_already_on_it(tracker, project, fake_gh):
    """§3: dedupe on the ISSUE, not on the run — keying on the run id is the 1007/1010
    shape, one filing per red run."""
    store = ProjectStore(project)
    poll(tracker, store)
    sweep(tracker, store)
    first = fact()
    assert first["issue_url"] and first["fix_wo_id"]

    fake_gh.set_runs("main", [MAIN_RED_AGAIN, MAIN_RED])
    poll(tracker, store)
    sweep(tracker, store)

    now = fact()
    assert now["run_id"] == 36274111111
    assert (now["issue_url"], now["fix_wo_id"]) \
        == (first["issue_url"], first["fix_wo_id"])
    assert filings(fake_gh) == 1
    assert orders(store) == [first["fix_wo_id"]]


def test_an_open_issue_quoting_the_run_id_is_adopted(tracker, project, fake_gh,
                                                     monkeypatch):
    """§4: the run id is the one token unique to this break, and the LOWEST open hit
    wins so two concurrent filings converge on one."""
    filed: list[dict] = []
    monkeypatch.setattr(bugreport, "report_bug", lambda **kw: filed.append(kw))
    later = f"https://github.com/{FIXTURE_BUG_REPO}/issues/1200"
    first = f"https://github.com/{FIXTURE_BUG_REPO}/issues/1054"
    for url in (later, first):
        fake_gh.add_issue(url, "main is red", body=f"run {TRACKER_RUN_URL} failed")
    store = ProjectStore(project)
    poll(tracker, store)

    sweep(tracker, store)

    assert filed == []
    held = fact()
    assert held["issue_url"] == first and held["fix_wo_id"]
    assert [w["issue_url"] for w in store.list_work_orders(include_hidden=True)] \
        == [first]


def test_a_closed_issue_quoting_the_run_id_is_not_adopted(tracker, project, fake_gh):
    """§4 filter 1: a CLOSED issue citing this run is a fix that was tried and did not
    work, and `start_work` refuses a closed issue outright."""
    closed = f"https://github.com/{FIXTURE_BUG_REPO}/issues/1054"
    fake_gh.add_issue(closed, "main is red", body=f"run {TRACKER_RUN_URL} failed",
                      state="CLOSED")
    store = ProjectStore(project)
    poll(tracker, store)

    sweep(tracker, store)

    held = fact()
    assert held["issue_url"] == fake_gh.issue_url != closed
    assert filings(fake_gh) == 1 and held["fix_wo_id"]
    # §2: `high` + `expedite`, and the evidence the NEXT sweep's search keys on.
    assert store.get_work_order(held["fix_wo_id"])["issue_priority"] == "high"
    body = fake_gh.issue(held["issue_url"])["body"]
    assert TRACKER_RUN_URL in body and MAIN_RED_SHA in body and "36273250195" in body


def test_a_filing_that_fails_leaves_nothing_and_the_next_tick_succeeds(
        tracker, project, fake_gh, monkeypatch):
    """§2 step 9: every way the dispatch fails is transient and outside the OS, so the
    sweep retries rather than firing once on the red transition."""
    real = bugreport.report_bug
    boom = {"on": True}

    def flaky(**kw):
        if boom["on"]:
            raise RuntimeError("Neo could not be reached")
        return real(**kw)

    monkeypatch.setattr(bugreport, "report_bug", flaky)
    store = ProjectStore(project)
    poll(tracker, store)

    sweep(tracker, store)

    assert not fact().get("fix_wo_id") and not fact().get("issue_url")
    assert orders(store) == []

    boom["on"] = False
    sweep(tracker, store)

    assert fact()["fix_wo_id"] and fact()["issue_url"]
    assert len(orders(store)) == 1


def test_the_green_transition_drops_both_keys_and_a_re_break_dispatches_again(
        tracker, project, fake_gh):
    """§3: the GREEN transition simply does not set them, so no explicit clear."""
    store = ProjectStore(project)
    poll(tracker, store)
    sweep(tracker, store)
    assert fact()["fix_wo_id"]

    fake_gh.set_runs("main", [MAIN_GREEN, MAIN_RED])
    poll(tracker, store)
    sweep(tracker, store)

    assert not fact().get("fix_wo_id") and not fact().get("issue_url")

    fake_gh.next_issue(f"https://github.com/{FIXTURE_BUG_REPO}/issues/8")
    fake_gh.set_runs("main", [MAIN_RED_AGAIN, MAIN_GREEN, MAIN_RED])
    poll(tracker, store)
    sweep(tracker, store)

    held = fact()
    assert held["issue_url"].endswith("/issues/8") and held["fix_wo_id"]
    assert filings(fake_gh) == 2


def _dead_fix(tracker, store, status: str = "completed") -> str:
    """One dispatched fix order, settled terminal, with `main` still red."""
    poll(tracker, store)
    sweep(tracker, store)
    fix = fact()["fix_wo_id"]
    assert fix
    store.set_status(fix, status)
    return fix


def _owed(store) -> list[str]:
    return [v.owed for v in check_project(store, repair=False)
            if v.invariant == invariants.INV_BASE_FIX_DEAD]


def _detail(store) -> str:
    return next(v.detail for v in check_project(store, repair=False)
                if v.invariant == invariants.INV_BASE_RED)


def _command_for(status: str, fix: str, issue_url: str) -> str:
    """What each dead status may name: `wo retry` is `failed`-only by contract."""
    return (f"`jarvis wo retry {fix}`" if status == "failed"
            else f"`jarvis issues start {issue_url}`")


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_a_terminal_fix_order_on_a_still_red_base_dispatches_nothing(tracker, project,
                                                                     fake_gh, status):
    """§2 step 5, as the user ruled on assumption 2 (Neo 1738): one fix work order per
    break, EVER. A second is an uncapped spend loop, and every environmental cause that
    killed the first kills a replacement."""
    store = ProjectStore(project)
    fix = _dead_fix(tracker, store, status)
    before = filings(fake_gh)

    poll(tracker, store)
    sweep(tracker, store)

    assert fact()["fix_wo_id"] == fix
    assert fact()["issue_url"] == fake_gh.issue_url
    assert filings(fake_gh) == before == 1
    assert orders(store) == [fix]


@pytest.mark.parametrize("status", [*TERMINAL_STATUSES, "deleted"])
def test_the_detail_names_the_dead_fix_order_and_a_command_that_can_run(
        tracker, project, fake_gh, status):
    """§5's third remedy branch: the detail STATES the state the OS is in, names only a
    command that can actually run — `jarvis wo retry` is `failed`-ONLY — and is
    byte-identical across ticks because it is the dedupe key."""
    store = ProjectStore(project)
    if status == "deleted":
        poll(tracker, store)
        sweep(tracker, store)
        fix = fact()["fix_wo_id"]
        store.delete_work_order(fix)
    else:
        fix = _dead_fix(tracker, store, status)

    seen = set()
    for _ in range(2):
        poll(tracker, store)
        sweep(tracker, store)
        seen.add(_detail(store))

    assert len(seen) == 1
    detail = seen.pop()
    assert f"{fix} was dispatched to produce it and is now `{status}`" in detail
    assert "dispatches no second fix order" in detail
    assert _command_for(status, fix, fact()["issue_url"]) in detail
    if status != "failed":
        assert "jarvis wo retry" not in detail
    if status == "deleted":
        assert "Its record is gone, so nothing can be resumed" in detail


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_a_terminal_fix_order_owes_the_user_a_decision(tracker, spec, project, fake_gh,
                                                       status):
    """§5: the OS is out of moves — `jarvis wo retry` is the user's by contract, and
    `failed`-only — so the violation owes a decision, naming a command that can run, and
    the sentence reaches the work order's attention flag."""
    store = ProjectStore(project)
    fix = _dead_fix(tracker, store, status)

    poll(tracker, store)
    sweep(tracker, store)
    tracker.check_invariants(spec, store)

    owed = _owed(store)
    assert len(owed) == 1 and fix in owed[0] and f"`{status}`" in owed[0]
    assert _command_for(status, fix, fact()["issue_url"]) in owed[0]
    if status != "failed":
        assert "jarvis wo retry" not in owed[0]
    assert [r["owed"] for r in store.owed_violations(fix)] == owed
    row = store.get_work_order(fix)
    assert row["needs_attention"] == 1
    assert owed[0] in invariants.true_blockers(store, row)
    if status != "failed":
        # A `failed` order's own blocker outranks it; otherwise this is the only one.
        assert row["attention_reason"] == owed[0]


def test_the_owed_sentence_is_byte_identical_on_two_successive_ticks(tracker, project,
                                                                     fake_gh):
    """The kn-fb4aa3c4 trap: `ack_attention` stores the sentence verbatim and
    `true_blockers` compares it by equality, so nothing in it may move tick to tick."""
    store = ProjectStore(project)
    _dead_fix(tracker, store)

    seen = set()
    for _ in range(2):
        poll(tracker, store)
        sweep(tracker, store)
        seen.update(_owed(store))

    assert len(seen) == 1


def test_a_running_fix_order_owes_no_decision(tracker, project, fake_gh):
    """The order is working on it: nothing is owed and nothing is dispatched."""
    store = ProjectStore(project)
    poll(tracker, store)
    sweep(tracker, store)
    fix = fact()["fix_wo_id"]
    store.set_status(fix, "running")

    poll(tracker, store)
    sweep(tracker, store)

    assert _owed(store) == []
    assert orders(store) == [fix] and filings(fake_gh) == 1


def test_a_deleted_fix_order_behaves_as_terminal(tracker, project, fake_gh):
    """A row that is gone is not a second dispatch either — the break had its order."""
    store = ProjectStore(project)
    poll(tracker, store)
    sweep(tracker, store)
    fix = fact()["fix_wo_id"]
    store.delete_work_order(fix)

    poll(tracker, store)
    sweep(tracker, store)

    assert fact()["fix_wo_id"] == fix
    assert orders(store) == [] and filings(fake_gh) == 1
    owed = _owed(store)
    assert len(owed) == 1 and fix in owed[0]
    assert f"`jarvis issues start {fact()['issue_url']}`" in owed[0]
    assert "jarvis wo retry" not in owed[0]


def test_a_stored_issue_that_was_closed_is_cleared_and_the_break_is_filed_again(
        tracker, project, fake_gh, monkeypatch):
    """§2 step 6: `start_work` refuses a closed issue outright, so a stored url someone
    has since closed is a retry that can never progress — the shape of the defect this
    spec removes, one level down. The url is stored with NO work order (the pickup
    failed), which under step 5 is the only state this branch can be reached in."""
    real = bugreport.report_bug
    pickup_fails = {"on": True}
    monkeypatch.setattr(bugreport, "report_bug", lambda **kw: (
        {**real(**kw), "pickup": {}} if pickup_fails["on"] else real(**kw)))
    store = ProjectStore(project)
    poll(tracker, store)
    sweep(tracker, store)
    first = fact()
    assert first["issue_url"] and not first.get("fix_wo_id")
    fake_gh.set_issue(first["issue_url"], state="CLOSED")

    poll(tracker, store)
    sweep(tracker, store)

    assert not fact().get("issue_url") and not fact().get("fix_wo_id")
    assert filings(fake_gh) == 1

    pickup_fails["on"] = False
    fake_gh.next_issue(f"https://github.com/{FIXTURE_BUG_REPO}/issues/8")
    poll(tracker, store)
    sweep(tracker, store)

    held = fact()
    assert held["issue_url"].endswith("/issues/8") and held["fix_wo_id"]
    assert filings(fake_gh) == 2


def test_a_stale_fact_dispatches_nothing(tracker, project, fake_gh):
    """§2 step 2: freshness is judged in exactly one place, and a stale fact dispatches
    nothing for the same reason it pauses no merge."""
    store = ProjectStore(project)
    poll(tracker, store)
    central = CentralStore()
    central.set_state("base_health:proj_a", json.dumps(
        {**fact(central), "checked_at": time.time() - BASE_HEALTH_FRESH_SECONDS - 1}))
    before = len(fake_gh.calls)

    sweep(tracker, store)

    assert [c for c in fake_gh.calls[before:] if c["argv"][:1] == ["issue"]] == []
    assert orders(store) == []
    assert not fact().get("fix_wo_id")


def no_fix_wo_id() -> dict:
    """The fact as every route other than the sweep leaves it: an issue, no fixer."""
    central = CentralStore()
    central.set_state("base_health:proj_a", json.dumps({**fact(central),
                                                        "fix_wo_id": ""}))
    return fact(central)


# -- exempt by the ISSUE (spec
# docs/superpowers/specs/2026-10-14-the-red-base-fix-is-exempt-by-its-issue.md) --------


def test_an_order_on_the_red_base_issue_is_exempt_with_no_fix_wo_id(
        tracker, spec, project, fake_gh, local_base):
    """wo-e497639e's shape: dispatched on the issue by `jarvis issues start`, so the
    sweep never named it, and held by the very redness its merge removes."""
    opt_in(tracker)
    store, wo = parked(project, pr_url=TRACKER_PR)
    fake_gh.set_pr(TRACKER_PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(tracker, store)
    sweep(tracker, store)
    store.update_work_order(wo["id"], issue_url=fact()["issue_url"])
    assert no_fix_wo_id()["issue_url"]

    tracker.poll_pull_requests(spec, store)

    approvals = store.list_approvals(wo["id"])
    assert len(approvals) == 1 and approvals[0]["kind"] == "auto_merge"


def test_a_parked_order_on_another_issue_is_still_held_on_the_red_base(
        tracker, spec, project, fake_gh, local_base):
    """The exemption stays NARROW: issue linkage exempts the remedy and nobody else."""
    opt_in(tracker)
    store, wo = parked(project, pr_url=TRACKER_PR)
    fake_gh.set_pr(TRACKER_PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(tracker, store)
    sweep(tracker, store)
    store.update_work_order(
        wo["id"], issue_url=f"https://github.com/{FIXTURE_BUG_REPO}/issues/4242")
    no_fix_wo_id()

    tracker.poll_pull_requests(spec, store)

    assert store.list_approvals(wo["id"]) == []
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_BASE_RED


def test_the_remedy_with_red_checks_is_held_on_its_own_checks_not_on_the_base(
        tracker, spec, project, fake_gh, local_base):
    """Conditions 4-6 are untouched: the exemption skips the WORLD condition only, and
    a red remedy is refused on the fact about the artifact in hand."""
    opt_in(tracker)
    store, wo = parked(project, pr_url=TRACKER_PR)
    fake_gh.set_pr(TRACKER_PR, "OPEN", checks=[check("lint", "FAILURE",
                                                     workflow="lint")],
                   merge_state="CLEAN", head_oid=JUDGED)
    poll(tracker, store)
    sweep(tracker, store)
    store.update_work_order(wo["id"], issue_url=fact()["issue_url"])
    no_fix_wo_id()

    tracker.poll_pull_requests(spec, store)

    assert store.list_approvals(wo["id"]) == []
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] \
        == automerge.HELD_CHECKS_FAILED


def test_an_unattributed_red_base_exempts_an_order_with_no_issue_either(
        started, spec, project, fake_gh, local_base):
    """THE `"" == ""` TRAP. Both halves of the guard require the STORED value non-empty,
    so a break filed on no issue exempts nobody — least of all an order whose own
    `issue_url` is empty too."""
    opt_in(started)
    store, wo = parked(project)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=JUDGED)
    red_fleet(fake_gh)
    poll(started, store)
    store.update_work_order(wo["id"], issue_url="")
    assert not fact().get("issue_url")
    assert store.get_work_order(wo["id"])["issue_url"] == ""

    started.poll_pull_requests(spec, store)

    assert store.list_approvals(wo["id"]) == []
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_BASE_RED


def test_the_base_red_fact_carries_the_issue_the_break_is_filed_on(
        tracker, spec, project, fake_gh):
    """The key already exists on the fact; `_base_red` is what carries it to `decide`."""
    store = ProjectStore(project)
    poll(tracker, store)
    sweep(tracker, store)

    assert tracker._base_red(spec).issue_url == fact()["issue_url"] == fake_gh.issue_url


def test_the_hold_reason_and_the_detail_name_the_order_and_never_change(
        tracker, spec, project, fake_gh):
    """§5, the kn-fb4aa3c4 trap: the dedupe key IS the sentence, so both carry the work
    order and nothing that moves tick to tick."""
    opt_in(tracker)
    store, wo = parked(project, pr_url=TRACKER_PR)
    fake_gh.set_pr(TRACKER_PR, "OPEN", checks=GREEN, merge_state="CLEAN",
                   head_oid=JUDGED)
    poll(tracker, store)
    sweep(tracker, store)
    fix = fact()["fix_wo_id"]
    assert fix

    details, reasons = set(), set()
    for _ in range(3):
        poll(tracker, store)
        sweep(tracker, store)
        tracker.check_invariants(spec, store)
        tracker.poll_pull_requests(spec, store)
        details.add(next(v.detail for v in check_project(store, repair=False)
                         if v.invariant == invariants.INV_BASE_RED))
        payload = db.from_json(
            store.events_of_kind(wo["id"], "automerge_held")[-1]["payload"], {})
        assert payload["code"] == automerge.HELD_BASE_RED
        reasons.add(payload["reason"])

    assert len(details) == 1 and fix in details.pop()
    assert len(reasons) == 1 and fix in reasons.pop()
    assert len([n for n in store.unrouted_notifications()
                if invariants.INV_BASE_RED in n["title"]]) == 1
