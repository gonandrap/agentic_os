"""A release order overtaken mid-CI-wait settles itself — issue #784.

docs/superpowers/specs/2026-09-26-a-release-order-overtaken-mid-ci-wait-settles-itself.md

Twice measured (wo-e5816dd7, wo-4301a7a6): a release work order the daemon filed sat open
after the release it was going to cut had already shipped, and a worker turn was spent
running two git commands to close it. The arithmetic lives here instead.

Real local repositories, real `git tag --contains`: the defect is an ancestry question and
a fake would let the wrong field pass (kn-179cd767, kn-4f5aaa2b).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis import db, release
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import commit

ISSUE = "https://github.com/acme/proj/issues/784"
PR = "https://github.com/acme/proj/pull/7"
TAG = "jarvis-0.10.24"
LATER_TAG = "jarvis-0.10.25"
OLD_TAG = "jarvis-0.10.20"


# -- fixtures -------------------------------------------------------------------------


def sha(repo: Path, rev: str = "HEAD") -> str:
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", rev],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def land(repo: Path, name: str) -> str:
    """One commit on `main`, as a squash merge leaves it. Returns its sha."""
    (repo / name).write_text(name)
    commit(repo, f"squash: {name}", name)
    return sha(repo)


def tag(repo: Path, name: str, rev: str = "HEAD") -> None:
    """An ANNOTATED tag, which is what the release path cuts."""
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "tag", "-a", name, "-m", name, rev], check=True)


def on_a_branch(repo: Path, name: str) -> str:
    """A commit that never reached `main` — a pull request's `headRefOid`."""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", name], check=True)
    head = land(repo, f"{name}.txt")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    return head


@pytest.fixture()
def daemon(catalog_file):
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def deploy(tmp_path, monkeypatch):
    """Put the production checkout at a tag, with a pyproject.toml version to match.

    BOTH readings, because `overtaken_by` checks both (kn-58429229): the ref from git and
    the version from the file.
    """
    root = tmp_path / "production"
    monkeypatch.setenv("PRODUCTION_CODE", str(root))

    def put(source: Path, ref: str, version: str | None = None) -> None:
        dest = root / "jarvis_os"
        shutil.rmtree(dest, ignore_errors=True)
        root.mkdir(exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(source), str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", ref], check=True)
        if version is None:
            version = ref.removeprefix("jarvis-")
        (dest / "pyproject.toml").write_text(
            f'[project]\nname = "jarvis-os"\nversion = "{version}"\n')

    return put


def release_order(store: ProjectStore, project: Path, *, shas: dict[str, str],
                  status: str = "running") -> str:
    """A release order as `ensure_release` files it, with its batch already resolved.

    `shas` maps issue url to the merge commit of the fix that closed it: one fixing work
    order per entry, carrying the `pr_merged` event that records it (§2).
    """
    rel = store.create_work_order("Ship the fix", status=status,
                                  metadata={Daemon.RELEASE_BATCH_KEY: []})
    for url, merge_commit in shas.items():
        batch_in(store, str(rel["id"]), url, merge_commit)
    return str(rel["id"])


def batch_in(store: ProjectStore, rel_id: str, url: str, merge_commit: str) -> str:
    """One more landed fix joining an open release order, `ensure_release`'s way."""
    meta = db.from_json(store.get_work_order(rel_id).get("metadata"), {})
    store.update_work_order(rel_id, metadata=db.to_json(
        {**meta, Daemon.RELEASE_BATCH_KEY: [*meta[Daemon.RELEASE_BATCH_KEY], url]}))
    fix = store.create_work_order("fix the bug", status="completed", issue_url=url)
    store.update_work_order(fix["id"], pr_url=PR)
    payload = {"pr_url": PR, "head_oid": "branchtip"}
    if merge_commit:
        payload["merge_commit"] = merge_commit
    store.add_event(fix["id"], "pr_merged", payload)
    store.add_event(rel_id, "release_batched", {"issue_url": url, "wo_id": fix["id"]})
    return str(fix["id"])


def step(daemon: Daemon, store: ProjectStore) -> None:
    """The settlement step alone, without the rest of the tick around it."""
    daemon.settle_shipped_releases(daemon.catalog.project("proj_a"), store)


def snapshot(store: ProjectStore, wo_id: str) -> tuple:
    return store.get_work_order(wo_id), store.list_events(wo_id)


def events(store: ProjectStore, wo_id: str, kind: str) -> list[dict]:
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo_id, kind)]


# -- 1 & 2: the ancestry test, on real git --------------------------------------------


def test_a_tag_containing_the_whole_batch_is_found_and_half_a_batch_is_not(project):
    """`git tag --contains A --contains B` is a UNION, so one call would report a tag
    carrying half the batch as carrying all of it. The intersection is what this pins."""
    first = land(project, "fix-one.txt")
    tag(project, TAG)
    second = land(project, "fix-two.txt")

    assert release.tags_containing(project, [first]) == ([TAG], "")
    assert release.tags_containing(project, [first, second]) == ([], "")

    tag(project, LATER_TAG)
    assert release.tags_containing(project, [first, second]) == ([LATER_TAG], "")
    # Ordered by version, lowest first: the first release carrying the whole batch.
    assert release.tags_containing(project, [first]) == ([TAG, LATER_TAG], "")


def test_the_squash_trap_a_branch_tip_is_in_no_tag(project):
    """`automerge._merge_args` merges with `--squash`, so `headRefOid` is NEVER an
    ancestor of `main`. Comparing it against a tag waits for ever."""
    head_ref_oid = on_a_branch(project, "feature")
    land(project, "fix-one.txt")
    tag(project, TAG)

    assert release.tags_containing(project, [head_ref_oid]) == ([], "")


# -- 3: the whole point ---------------------------------------------------------------


def test_a_release_already_shipped_and_deployed_completes_the_order(
        project, store, daemon, deploy):
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE: landed})

    step(daemon, store)

    assert store.get_work_order(rel)["status"] == "completed"
    done = events(store, rel, "release_completed")
    assert len(done) == 1
    assert done[0]["tag"] == TAG
    assert TAG in done[0]["why"]
    assert not store.get_work_order(rel)["needs_attention"]


# -- 4: a tag nobody deployed ---------------------------------------------------------


def test_a_containing_tag_production_is_behind_says_so_exactly_once(
        project, store, daemon, deploy):
    """Neo question 751 option B: a tag nobody deployed does not pay the debt, so the
    order stays open — and the note is written once per TAG, never once per tick."""
    land(project, "old.txt")
    tag(project, OLD_TAG)
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, OLD_TAG)
    rel = release_order(store, project, shas={ISSUE: landed})

    for _ in range(3):
        step(daemon, store)

    assert store.get_work_order(rel)["status"] == "running"
    assert not store.get_work_order(rel)["needs_attention"]
    said = events(store, rel, "release_overtaken")
    assert len(said) == 1
    assert said[0]["tag"] == TAG
    assert said[0]["deployed"] == OLD_TAG

    # A SECOND fix joins the open batch (`ensure_release` batches while the order waits),
    # and only a later tag carries it. The user hears about that tag too — dedupe by kind
    # alone would have gone silent here.
    second = land(project, "fix-two.txt")
    tag(project, LATER_TAG)
    batch_in(store, rel, ISSUE + "-b", second)
    step(daemon, store)

    said = events(store, rel, "release_overtaken")
    assert [s["tag"] for s in said] == [TAG, LATER_TAG]


# -- 5: every refusal in §7 -----------------------------------------------------------


def unresolved(project, store, daemon, deploy, landed):
    """No recorded merge commit, and `gh` cannot answer — the payload is half known."""
    tag(project, TAG)
    deploy(project, TAG)
    return release_order(store, project, shas={ISSUE: ""})


def git_unreadable(project, store, daemon, deploy, landed):
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE: "not-a-sha-at-all"})
    return rel


def no_containing_tag(project, store, daemon, deploy, landed):
    tag(project, OLD_TAG, rev="HEAD~1")  # cut BEFORE the fix landed
    deploy(project, OLD_TAG)
    return release_order(store, project, shas={ISSUE: landed})


def no_production_checkout(project, store, daemon, deploy, landed):
    tag(project, TAG)
    return release_order(store, project, shas={ISSUE: landed})


def production_on_head(project, store, daemon, deploy, landed):
    tag(project, TAG)
    deploy(project, TAG)
    subprocess.run(["git", "-C", str(Path(release.production_code_dir())),
                    "checkout", "-q", "--detach", "HEAD~1"], check=True)
    return release_order(store, project, shas={ISSUE: landed})


def version_disagrees_with_the_tag(project, store, daemon, deploy, landed):
    tag(project, TAG)
    deploy(project, TAG, version="0.10.20")
    return release_order(store, project, shas={ISSUE: landed})


def a_marker_names_this_order(project, store, daemon, deploy, landed):
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE: landed})
    release.write_marker({"state": "failed_verification", "wo_id": rel,
                          "project": "proj_a", "version": "0.10.24", "tag": TAG})
    return rel


@pytest.mark.parametrize("setup", [
    unresolved,
    git_unreadable,
    no_containing_tag,
    no_production_checkout,
    production_on_head,
    version_disagrees_with_the_tag,
    a_marker_names_this_order,
], ids=lambda f: f.__name__)
def test_every_refusal_leaves_the_work_order_exactly_as_it_was(
        project, store, daemon, deploy, fake_gh, setup):
    """§7: it never fails open. No event, no status change, no flag — retried next tick."""
    fake_gh.fail("gh: could not connect")
    landed = land(project, "fix-one.txt")
    rel = setup(project, store, daemon, deploy, landed)
    before = snapshot(store, rel)

    step(daemon, store)

    assert snapshot(store, rel) == before


# -- 6: `settle`'s traps, through the new caller -------------------------------------


def test_a_pending_assumption_is_not_accepted_by_the_back_door(
        project, store, daemon, deploy):
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE: landed}, status="needs_review")
    store.add_assumption(rel, "shipped without running the browser suite")

    step(daemon, store)

    assert store.get_work_order(rel)["status"] == "needs_review"
    assert not events(store, rel, "release_completed")


def test_an_order_behind_a_pull_request_stays_parked_on_its_merge(
        project, store, daemon, deploy):
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE: landed},
                        status="waiting_pr_merge")

    step(daemon, store)

    assert store.get_work_order(rel)["status"] == "waiting_pr_merge"
    assert not events(store, rel, "release_completed")


# -- 7: the back-fill is paid once ---------------------------------------------------


def test_the_back_fill_of_a_merge_commit_costs_one_gh_call_ever(
        project, store, daemon, deploy, fake_gh):
    """A fix that merged before §2 shipped has no recorded sha. One `pr_view` resolves
    it, and the event it writes is what the next tick reads instead."""
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    tag(project, OLD_TAG, rev="HEAD~1")
    deploy(project, OLD_TAG)  # behind, so the order stays open and the step runs twice
    rel = release_order(store, project, shas={ISSUE: ""})
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-25T10:00:00Z",
                   merge_commit=landed)

    step(daemon, store)
    step(daemon, store)

    views = [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]]
    assert len(views) == 1
    fix = store.get_work_order(
        db.from_json(store.events_of_kind(rel, "release_batched")[0]["payload"],
                     {})["wo_id"])
    recorded = events(store, fix["id"], "pr_merge_commit_recorded")
    assert [r["merge_commit"] for r in recorded] == [landed]
    assert len(events(store, rel, "release_overtaken")) == 1


# -- 8: what the common case costs ---------------------------------------------------


def test_with_no_open_release_order_the_step_only_reads_and_spawns_nothing(
        project, store, daemon, monkeypatch):
    """§4's budget, executed rather than commented."""
    store.create_work_order("something else", status="running")
    spawned: list[list[str]] = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda a, *ar, **kw: (spawned.append(a), real(a, *ar, **kw))[1])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    step(daemon, store)

    store.conn.set_trace_callback(None)
    assert sql, "the step must at least look for an open release order"
    assert all(s.strip().upper().startswith("SELECT") for s in sql), sql
    assert all("work_orders" in s.lower() for s in sql), sql
    assert spawned == []


def test_a_project_that_does_not_own_the_os_pays_nothing(
        project, store, daemon, monkeypatch, tmp_path):
    """§8: the test reads `paths.production_code_dir()` and globs `jarvis-*`, both facts
    about the OS's own release path. A second project gets exactly today's behaviour."""
    land(project, "fix-one.txt")
    release_order(store, project, shas={ISSUE: sha(project)})
    monkeypatch.setattr(daemon, "_os_owner", lambda: "someone-else")
    spawned: list[list[str]] = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda a, *ar, **kw: (spawned.append(a), real(a, *ar, **kw))[1])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    step(daemon, store)

    store.conn.set_trace_callback(None)
    assert sql == []
    assert spawned == []


# -- the hook ------------------------------------------------------------------------


def test_the_tick_runs_the_step_after_the_issue_sweep(project, daemon, monkeypatch):
    """§4: inside the existing `if poll_prs:` block, immediately after
    `sync_issue_references` — so after `sync_issues`, which is what files these orders.

    And the red-base hold immediately BEFORE `dispatch_pending` (2026-09-27 spec §5):
    a release order filed late in one tick is claimed early in the next, so a hold
    running after dispatch would hold nothing on the first opportunity."""
    order: list[str] = []
    for name in ("hold_red_release", "dispatch_pending", "poll_pull_requests",
                 "sync_issues", "sync_issue_references", "settle_shipped_releases"):
        monkeypatch.setattr(daemon, name,
                            lambda *a, _n=name, **k: order.append(_n))
    daemon.tick_count = 0

    daemon.tick()

    assert order == ["hold_red_release", "dispatch_pending", "poll_pull_requests",
                     "sync_issues", "sync_issue_references", "settle_shipped_releases"]


# -- the red-`main` hold ---------------------------------------------------------------
#
# docs/superpowers/specs/2026-09-27-an-expedited-order-that-lands-ships-a-release.md §5.
# `ensure_release` files unless production already runs the payload (#934), so the hold is
# on DISPATCH: a call skipped because the base was red never comes back, and the fix would
# be dropped from every future batch.


def run(sha_: str, conclusion: str = "failure", workflow: str = "tests",
        run_id: int = 1, status: str = "completed"):
    """One base run, as `ci.base_runs` normalises it."""
    from jarvis import ci
    return ci.Run(run_id=run_id, head_sha=sha_, conclusion=conclusion, status=status,
                  started_at=1.0, workflow=workflow)


@pytest.fixture()
def base_ci(monkeypatch):
    """What `ci.base_runs` answers for the base branch, newest first."""
    from jarvis import ci

    state: dict = {"runs": (), "error": None, "calls": []}

    def fake(base_ref, cwd=None, limit=None):
        state["calls"].append(base_ref)
        if state["error"] is not None:
            raise state["error"]
        return state["runs"]

    monkeypatch.setattr(ci, "base_runs", fake)
    return state


def pending_release(store: ProjectStore) -> str:
    """A release order waiting to be claimed — what the hold acts on."""
    rel = store.create_work_order("Ship the fix", status="pending",
                                  metadata={Daemon.RELEASE_BATCH_KEY: [ISSUE]})
    return str(rel["id"])


def hold(daemon: Daemon, store: ProjectStore) -> None:
    daemon.hold_red_release(daemon.catalog.project("proj_a"), store)


def test_a_red_main_holds_a_pending_release(project, store, daemon, base_ci):
    """§5: red base, so the order is not claimable and the timeline says why."""
    rel = pending_release(store)
    base_ci["runs"] = (run("deadbeefcafe"),)

    hold(daemon, store)

    assert float(store.get_work_order(rel)["retry_after"] or 0) > db.now()
    assert store.claim_next_pending() is None, "a held order is not dispatched"
    said = events(store, rel, Daemon.RED_HOLD_EVENT)
    assert len(said) == 1
    assert "deadbeefca" in said[0]["detail"] and "tests" in said[0]["detail"]


def test_a_green_main_lets_the_release_go(project, store, daemon, base_ci):
    """The other half: nothing is written and the order is claimed."""
    rel = pending_release(store)
    base_ci["runs"] = (run("deadbeefcafe", conclusion="success"),)

    hold(daemon, store)

    assert store.get_work_order(rel)["retry_after"] is None
    assert events(store, rel, Daemon.RED_HOLD_EVENT) == []
    claimed = store.claim_next_pending()
    assert claimed is not None and str(claimed["id"]) == rel


def test_a_still_red_main_says_so_once_per_commit(project, store, daemon, base_ci):
    """kn-7b122cd9: a code per CONDITION is also a code per WORLD. Same broken commit is
    the same news; a DIFFERENT broken commit is not."""
    rel = pending_release(store)
    base_ci["runs"] = (run("aaaaaaaaaaaa"),)

    hold(daemon, store)
    store.update_work_order(rel, retry_after=None)  # the hold lapsed; still red
    hold(daemon, store)

    assert len(events(store, rel, Daemon.RED_HOLD_EVENT)) == 1
    base_ci["runs"] = (run("bbbbbbbbbbbb", run_id=2),)
    store.update_work_order(rel, retry_after=None)
    hold(daemon, store)

    said = events(store, rel, Daemon.RED_HOLD_EVENT)
    assert len(said) == 2
    assert [e["head_sha"] for e in said] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]


def test_an_unreadable_ci_does_not_hold_the_release(project, store, daemon, base_ci):
    """§5: an unreadable base must never strand the user's release on a `gh` outage.
    The brief still tells the worker to check, and the ship is a gated action."""
    from jarvis.github import GitHubError
    rel = pending_release(store)
    base_ci["error"] = GitHubError("gh is off", GitHubError.NO_GH)

    hold(daemon, store)

    assert store.get_work_order(rel)["retry_after"] is None
    assert events(store, rel, Daemon.RED_HOLD_EVENT) == []
    claimed = store.claim_next_pending()
    assert claimed is not None and str(claimed["id"]) == rel


def test_a_red_hold_never_shortens_a_dispatch_backoff(project, store, daemon, base_ci):
    """`dispatch_attempts` and its ladder belong to `release_dispatch_claim`. A red hold
    may extend that backoff, never shorten it, and green may not erase it."""
    rel = pending_release(store)
    for _ in range(3):
        store.release_dispatch_claim(rel, "claude would not launch", max_attempts=10)
    backoff = float(store.get_work_order(rel)["retry_after"])
    attempts = store.get_work_order(rel)["dispatch_attempts"]
    assert backoff > db.now() + Daemon.RED_HOLD_SECONDS and attempts

    base_ci["runs"] = (run("aaaaaaaaaaaa"),)
    hold(daemon, store)
    assert float(store.get_work_order(rel)["retry_after"]) == backoff

    # The ladder LAPSED, and the base is green: still not this step's to erase.
    store.update_work_order(rel, retry_after=db.now() - 1)
    lapsed = float(store.get_work_order(rel)["retry_after"])
    base_ci["runs"] = (run("aaaaaaaaaaaa", conclusion="success"),)
    hold(daemon, store)
    row = store.get_work_order(rel)
    assert float(row["retry_after"]) == lapsed, "green may not clear a launch backoff"
    assert row["dispatch_attempts"] == attempts


# -- the deferred release ------------------------------------------------------------
#
# docs/superpowers/specs/2026-09-29-a-release-blocked-by-a-red-main-retries-itself.md §4.


def test_overtaken_settles_a_release_deferred_in_pending(project, store, daemon, deploy):
    """§4: a release re-parked into `pending` by a red base is still settled when a newer
    release ships its batch.

    Works today by the accident of one tuple's contents — `pending` is the FIRST entry of
    `OPEN_STATUSES` and `_settle_shipped_release` has no status guard at all — and either
    would break it silently. This is the pin.
    """
    from jarvis.project_store import OPEN_STATUSES

    assert "pending" in OPEN_STATUSES
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE: landed}, status="pending")
    store.hold_dispatch(rel, until=db.now() + Daemon.RED_HOLD_SECONDS)

    step(daemon, store)

    assert store.get_work_order(rel)["status"] == "completed"
    assert len(events(store, rel, "release_completed")) == 1


# -- a landed fix batched into a release order that already finished — issue #945 -------
#
# docs/superpowers/specs/2026-10-06-a-landed-fix-batched-into-a-release-order-that-already-finished.md

OTHER = "https://github.com/acme/proj/issues/900"


def sweep(daemon: Daemon, store: ProjectStore) -> None:
    daemon.refile_dropped_fixes(daemon.catalog.project("proj_a"), store)


def batch_urls(store: ProjectStore) -> dict[str, list[str]]:
    """Every work order carrying a release batch, by id — the urls in it.

    `batches` below answers the other question (which ORDERS carry one) and the two
    cannot share a name.
    """
    out = {}
    for wo in store.list_work_orders(include_hidden=True):
        meta = db.from_json(wo.get("metadata"), {}) or {}
        batch = meta.get(Daemon.RELEASE_BATCH_KEY)
        if isinstance(batch, list):
            out[str(wo["id"])] = [str(u) for u in batch]
    return out


# -- §3.1-3.3: selection ---------------------------------------------------------------


def test_a_finished_candidate_is_skipped_and_a_fresh_order_is_filed(
        project, store, daemon):
    """wo-29f44978 reproduced: `needs_review` is in `OPEN_STATUSES`, so the fix joined a
    batch whose worker had already delivered and no tag ever carried it (§1.1)."""
    landed = land(project, "fix-one.txt")
    rel = release_order(store, project, shas={OTHER: landed}, status="needs_review")
    store.add_event(rel, "finished", {"summary": "No release cut: nothing to ship"})
    fix = landed_fix(store, ISSUE, landed)

    target = filing(daemon, store, fix)

    assert target != rel
    assert batch_urls(store)[rel] == [OTHER], "a finished order's batch must not grow"
    assert batch_urls(store)[target] == [ISSUE]
    assert [e["issue_url"] for e in events(store, target, "release_batched")] == [ISSUE]
    assert "shipit" in (store.get_work_order(target)["description"] or "")


def test_a_still_running_candidate_still_batches(project, store, daemon):
    """§1.2: two blockers ten minutes apart still make one release."""
    landed = land(project, "fix-one.txt")
    rel = release_order(store, project, shas={OTHER: landed})
    fix = landed_fix(store, ISSUE, landed)

    target = filing(daemon, store, fix)

    assert target == rel
    assert batch_urls(store) == {rel: [OTHER, ISSUE]}
    assert [e["issue_url"]
            for e in events(store, rel, "release_batched")] == [OTHER, ISSUE]


@pytest.mark.parametrize("finished", [True, False])
def test_an_url_already_in_a_batch_adds_nothing_and_files_nothing(
        project, store, daemon, finished):
    """§1.3: read before any write, and it holds when the holder is FINISHED too — the
    sweep rescues that url, and a second release order filed here would race it."""
    landed = land(project, "fix-one.txt")
    rel = release_order(store, project, shas={ISSUE: landed})
    if finished:
        store.add_event(rel, "finished", {})
    fix = store.get_work_order(
        db.from_json(store.events_of_kind(rel, "release_batched")[0]["payload"],
                     {})["wo_id"])
    before = snapshot(store, rel)

    target = filing(daemon, store, fix)

    assert target == rel
    assert snapshot(store, rel) == before
    assert list(batch_urls(store)) == [rel], "no second release order"


# -- §3.4-3.8: the sweep ---------------------------------------------------------------


def test_the_sweep_refiles_an_unshipped_entry_exactly_once(project, store, daemon):
    """The issue's last sentence: settling must never drop a fix no live tag carries."""
    landed = land(project, "fix-one.txt")
    rel = release_order(store, project, shas={ISSUE: landed}, status="completed")
    fix_id = db.from_json(store.events_of_kind(rel, "release_batched")[0]["payload"],
                          {})["wo_id"]

    sweep(daemon, store)

    target = next(i for i in batch_urls(store) if i != rel)
    assert batch_urls(store)[target] == [ISSUE]
    refiled = events(store, rel, Daemon.RELEASE_REFILED_EVENT)
    assert len(refiled) == 1
    assert refiled[0]["issue_url"] == ISSUE
    assert refiled[0]["release_wo_id"] == target
    assert refiled[0]["fix_wo_id"] == fix_id
    assert events(store, target, Daemon.RELEASE_REFILED_FROM_EVENT) == [
        {"issue_url": ISSUE, "dropped_by": rel}]
    before = (snapshot(store, rel), snapshot(store, target), batch_urls(store))

    sweep(daemon, store)

    assert (snapshot(store, rel), snapshot(store, target), batch_urls(store)) == before


def test_the_sweep_files_nothing_when_a_tag_carries_the_entry(project, store, daemon,
                                                             monkeypatch):
    """§2.3: a containing tag accounts for the entry — whether production runs it is the
    deploy question and belongs to `_settle_shipped_release`. Then the order is CLEARED,
    so every later pass does zero git work."""
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    rel = release_order(store, project, shas={ISSUE: landed}, status="completed")

    sweep(daemon, store)

    assert list(batch_urls(store)) == [rel]
    assert events(store, rel, Daemon.RELEASE_REFILED_EVENT) == []
    assert len(events(store, rel, Daemon.RELEASE_BATCH_CLEARED_EVENT)) == 1

    reads: list = []
    real = release.overtaken_by
    monkeypatch.setattr(release, "overtaken_by",
                        lambda *a, **k: (reads.append(a), real(*a, **k))[1])
    sweep(daemon, store)
    sweep(daemon, store)

    assert reads == []
    assert len(events(store, rel, Daemon.RELEASE_BATCH_CLEARED_EVENT)) == 1


def test_the_sweep_pays_nothing_for_a_project_that_does_not_own_the_os(
        project, store, daemon, monkeypatch):
    """§2.1: it reads `jarvis-*` tags and the production checkout, both facts about this
    repository. Mirrors `test_a_project_that_does_not_own_the_os_pays_nothing`."""
    landed = land(project, "fix-one.txt")
    release_order(store, project, shas={ISSUE: landed}, status="completed")
    monkeypatch.setattr(daemon, "_os_owner", lambda: "someone-else")
    spawned: list[list[str]] = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda a, *ar, **kw: (spawned.append(a), real(a, *ar, **kw))[1])
    sql: list[str] = []
    store.conn.set_trace_callback(sql.append)

    sweep(daemon, store)

    store.conn.set_trace_callback(None)
    assert sql == []
    assert spawned == []


def test_the_tick_runs_the_sweep_after_the_settlement_step(project, daemon, monkeypatch):
    """§2.1: AFTER `settle_shipped_releases`, so an order that settles on this tick is
    read in its settled state by the next pass, and on the `poll_prs` beat."""
    order: list[str] = []
    for name in ("poll_pull_requests", "sync_issues", "sync_issue_references",
                 "settle_shipped_releases", "refile_dropped_fixes"):
        monkeypatch.setattr(daemon, name,
                            lambda *a, _n=name, **k: order.append(_n))
    daemon.tick_count = 0

    daemon.tick()

    assert order == ["poll_pull_requests", "sync_issues", "sync_issue_references",
                     "settle_shipped_releases", "refile_dropped_fixes"]


def test_a_filing_that_files_nothing_is_not_a_refile(project, store, daemon,
                                                     monkeypatch):
    """§2.3.4: `ensure_release` can decline to file — nothing to ship. No marker, so the
    url stays in the window and the next pass tries again."""
    landed = land(project, "fix-one.txt")
    rel = release_order(store, project, shas={ISSUE: landed}, status="completed")
    calls: list[str] = []
    monkeypatch.setattr(Daemon, "ensure_release",
                        lambda *a, **k: (calls.append("x"), "")[1])

    sweep(daemon, store)
    sweep(daemon, store)

    assert len(calls) == 2, "an unresolved url is looked at again"
    assert list(batch_urls(store)) == [rel]
    assert events(store, rel, Daemon.RELEASE_REFILED_EVENT) == []
    assert events(store, rel, Daemon.RELEASE_REFILED_FROM_EVENT) == []
    assert events(store, rel, Daemon.RELEASE_BATCH_CLEARED_EVENT) == []


def test_an_unreadable_tag_list_files_nothing_and_is_retried(project, store, daemon,
                                                            monkeypatch):
    """§2.3: fail closed. Read as "not shipped", a `git fetch` failure would file a
    release order per poll."""
    landed = land(project, "fix-one.txt")
    rel = release_order(store, project, shas={ISSUE: landed}, status="completed")
    monkeypatch.setattr(release, "overtaken_by",
                        lambda *a, **k: release.Overtaken(error="git is unreadable"))
    before = snapshot(store, rel)

    sweep(daemon, store)

    assert snapshot(store, rel) == before
    assert list(batch_urls(store)) == [rel]
    assert events(store, rel, Daemon.RELEASE_BATCH_CLEARED_EVENT) == []


# -- no release is filed for a fix a live tag already carries — issue #934 ------------
#
# `ensure_release` batched only against OPEN release orders, so once the previous release
# settled the next landed fix always earned a new one — five times a worker turn, a gate
# and a tag whose only commit was its own version bump (issue 837 was eight tags behind).


def landed_fix(store: ProjectStore, url: str, merge_commit: str) -> dict:
    """The FIXING order as `sync_issues` hands it to `ensure_release`."""
    fix = store.create_work_order("fix the bug", status="completed", issue_url=url)
    store.update_work_order(fix["id"], pr_url=PR)
    payload = {"pr_url": PR, "head_oid": "branchtip"}
    if merge_commit:
        payload["merge_commit"] = merge_commit
    store.add_event(fix["id"], "pr_merged", payload)
    return store.get_work_order(fix["id"])


def filing(daemon: Daemon, store: ProjectStore, fix: dict) -> str:
    """The filing step alone, without the rest of the issue sweep around it."""
    return daemon.ensure_release(daemon.catalog.project("proj_a"), store, fix)


def batches(store: ProjectStore) -> list[dict]:
    """Every order carrying a release batch — what `ensure_release` files or joins."""
    return [w for w in store.list_work_orders(include_hidden=True)
            if isinstance(db.from_json(w.get("metadata"), {}).get(
                Daemon.RELEASE_BATCH_KEY), list)]


def test_a_fix_a_live_tag_already_carries_files_no_release_at_all(
        project, store, daemon, deploy):
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    fix = landed_fix(store, ISSUE, landed)

    assert filing(daemon, store, fix) == ""

    assert batches(store) == []
    said = events(store, fix["id"], Daemon.ALREADY_SHIPPED_EVENT)
    assert len(said) == 1
    assert said[0]["tag"] == TAG
    assert said[0]["deployed"] == TAG
    assert said[0]["shas"] == [landed]
    assert TAG in said[0]["detail"]


def test_a_fix_a_live_tag_already_carries_does_not_join_an_open_batch_either(
        project, store, daemon, deploy):
    """The guard runs BEFORE the batch loop: joining would put a shipped fix in the next
    release's notes and keep that order open on work already live."""
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    rel = release_order(store, project, shas={ISSUE + "-other": landed})
    before = snapshot(store, rel)
    fix = landed_fix(store, ISSUE, landed)

    assert filing(daemon, store, fix) == ""

    assert snapshot(store, rel) == before
    assert len(events(store, fix["id"], Daemon.ALREADY_SHIPPED_EVENT)) == 1


def unreadable_repository(project, store, daemon, deploy, landed):
    tag(project, TAG)
    deploy(project, TAG)
    return landed_fix(store, ISSUE, "not-a-sha-at-all")


def no_resolvable_merge_commit(project, store, daemon, deploy, landed):
    tag(project, TAG)
    deploy(project, TAG)
    return landed_fix(store, ISSUE, "")


def a_tag_production_is_behind(project, store, daemon, deploy, landed):
    tag(project, OLD_TAG, rev="HEAD~1")
    tag(project, TAG)
    deploy(project, OLD_TAG)
    return landed_fix(store, ISSUE, landed)


def nothing_carries_it_yet(project, store, daemon, deploy, landed):
    tag(project, OLD_TAG, rev="HEAD~1")  # cut BEFORE the fix landed
    deploy(project, OLD_TAG)
    return landed_fix(store, ISSUE, landed)


@pytest.mark.parametrize("setup", [
    unreadable_repository,
    no_resolvable_merge_commit,
    a_tag_production_is_behind,
    nothing_carries_it_yet,
], ids=lambda f: f.__name__)
def test_everything_short_of_already_live_still_files_a_release(
        project, store, daemon, deploy, fake_gh, setup):
    """An unreadable repository or production checkout must NEVER strand a release —
    `an unreadable CI does not hold` points the same way."""
    fake_gh.fail("gh: could not connect")
    landed = land(project, "fix-one.txt")
    fix = setup(project, store, daemon, deploy, landed)

    rel = filing(daemon, store, fix)

    assert [w["id"] for w in batches(store)] == [rel]
    assert events(store, fix["id"], Daemon.ALREADY_SHIPPED_EVENT) == []


def test_a_project_that_does_not_own_the_os_files_exactly_as_before(
        project, store, daemon, deploy, monkeypatch):
    """`overtaken_by` reads `jarvis-*` tags and the OS's own production checkout, so a
    second project keeps today's behaviour — `settle_shipped_releases`' guard."""
    landed = land(project, "fix-one.txt")
    tag(project, TAG)
    deploy(project, TAG)
    monkeypatch.setattr(daemon, "_os_owner", lambda: "someone-else")
    fix = landed_fix(store, ISSUE, landed)

    rel = filing(daemon, store, fix)

    assert [w["id"] for w in batches(store)] == [rel]
    assert events(store, fix["id"], Daemon.ALREADY_SHIPPED_EVENT) == []
