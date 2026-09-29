"""A release blocked by a red `main` retries itself — issue #838.

docs/superpowers/specs/2026-09-29-a-release-blocked-by-a-red-main-retries-itself.md

The live case is wo-fc61d0cd: gate 310 approved, `scripts/shipit.sh --stage` ran, `main`
turned red under it (5a4e14d, #829) while it waited for CI, and the worker correctly
refused to ship an older green commit. The order settled `needs_review` and the expedited
fix sat on `main` unreleased until a human typed something. The re-park lives here.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import db, invariants, ops, release
from jarvis.catalog import load_catalog
from jarvis.central_store import BASE_HEALTH_FRESH_SECONDS, CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

ISSUE = "https://github.com/acme/proj/issues/826"
#: The incident's own broken commit on `main`.
RED_SHA = "5a4e14d0000000000000000000000000000000ff"
RUN_URL = "https://github.com/acme/proj/actions/runs/36273250195"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def release_order(store: ProjectStore, *, status: str = "running") -> str:
    """A release order mid-run: the batch in its metadata, no release delivered."""
    wo = store.create_work_order("Ship the fix", status=status,
                                 metadata={release.BATCH_KEY: [ISSUE]})
    return str(wo["id"])


def base_health(*, red: bool = True, checked_at: float | None = -1.0,
                head_sha: str = RED_SHA) -> None:
    """The stored reading `Daemon.poll_default_branch` writes, in its own shape."""
    fact = {"red": red, "base": "main", "workflow": "ci", "run_id": 36273250195,
            "run_url": RUN_URL, "head_sha": head_sha if red else "", "wo_id": ""}
    if checked_at is not None:
        fact["checked_at"] = time.time() if checked_at == -1.0 else checked_at
    central = CentralStore()
    try:
        central.set_base_health("proj_a", fact)
    finally:
        central.close()


def events(store: ProjectStore, wo_id: str, kind: str) -> list[dict]:
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo_id, kind)]


def backdate(store: ProjectStore, wo_id: str, kind: str, seconds: float) -> None:
    """Age every event of one kind, so a threshold measured from it has passed."""
    store.conn.execute("UPDATE wo_events SET ts=? WHERE wo_id=? AND kind=?",
                       (db.now() - seconds, wo_id, kind))
    store.conn.commit()


# -- 1: the red reading -----------------------------------------------------------


def test_red_reading_reparks_release_to_pending(started, store):
    """§1: a release that delivered no release, over a red base, goes back to `pending`
    with a hold — not to the user. And `hold_dispatch` writes `retry_after` ONLY:
    `dispatch_attempts` is `release_dispatch_claim`'s ladder, and a deferral for a reason
    outside the order must not spend a launch."""
    rel = release_order(store)
    base_health()

    out = ops.finish(rel, "Release BLOCKED on a red main")

    assert out["status"] == "pending"
    wo = store.get_work_order(rel)
    assert wo["status"] == "pending"
    assert abs(float(wo["retry_after"]) - (db.now() + Daemon.RED_HOLD_SECONDS)) < 10
    assert int(wo["dispatch_attempts"] or 0) == 0
    assert not wo["needs_attention"] and not wo["attention_reason"]
    said = events(store, rel, ops.RED_DEFER_EVENT)
    assert len(said) == 1 and said[0]["head_sha"] == RED_SHA


def test_the_defer_event_is_said_once_per_broken_commit(started, store):
    """kn-7b122cd9, `_say_base_is_red`'s rule: the same broken commit is the same news;
    `main` breaking again on a different commit is not."""
    rel = release_order(store)
    base_health()

    ops.finish(rel, "blocked")
    store.set_status(rel, "running")
    ops.finish(rel, "blocked again")
    assert len(events(store, rel, ops.RED_DEFER_EVENT)) == 1

    base_health(head_sha="bbbbbbbb00000000000000000000000000000000")
    store.set_status(rel, "running")
    ops.finish(rel, "red on a new commit")

    said = events(store, rel, ops.RED_DEFER_EVENT)
    assert [e["head_sha"][:8] for e in said] == ["5a4e14d0", "bbbbbbbb"]


# -- 2: the unusable reading ------------------------------------------------------


@pytest.mark.parametrize("reading", ["absent", "unparseable", "no_checked_at", "stale"])
def test_unusable_reading_reparks_like_red(started, store, reading):
    """§1, Neo's explicit condition: only a FRESH GREEN reading parks on the user. An
    unusable fact is not evidence that `main` is fine."""
    rel = release_order(store)
    if reading == "unparseable":
        central = CentralStore()
        try:
            central.set_state("base_health:proj_a", "{not json")
        finally:
            central.close()
    elif reading == "no_checked_at":
        base_health(red=False, checked_at=None)
    elif reading == "stale":
        base_health(red=False,
                    checked_at=time.time() - BASE_HEALTH_FRESH_SECONDS - 1)

    out = ops.finish(rel, "Release BLOCKED on a red main")

    assert out["status"] == "pending", reading
    wo = store.get_work_order(rel)
    assert wo["status"] == "pending"
    assert float(wo["retry_after"]) > db.now()
    assert len(events(store, rel, ops.RED_DEFER_EVENT)) == 1


# -- 3: the fresh green reading ---------------------------------------------------


def test_fresh_green_reading_parks_on_the_user(started, store):
    """The only route to the user inside the threshold: the base is buildable and the
    release still delivered nothing, so today's behaviour stands. The assumption is the
    live case's — wo-fc61d0cd recorded one and settled `needs_review`."""
    rel = release_order(store)
    store.add_assumption(rel, "shipping the older green commit is not acceptable")
    base_health(red=False)

    out = ops.finish(rel, "Release BLOCKED on a red main")

    assert out["status"] == "needs_review"
    wo = store.get_work_order(rel)
    assert wo["retry_after"] is None
    assert events(store, rel, ops.RED_DEFER_EVENT) == []


# -- 4: the green tick, and the re-dispatch that reuses the conversation ----------


def test_green_tick_clears_the_hold_and_dispatch_reruns_it(
        started, store, project, monkeypatch, fake_claude):
    """§5: the re-dispatch RESUMES — the order still carries the session that holds the
    approved gate and the reasoning about the red base, and `--session-id` on a session
    that exists is refused by the CLI."""
    from jarvis import ci

    rel = release_order(store)
    base_health()
    ops.finish(rel, "Release BLOCKED on a red main")
    session = _resumable(started, store, project, rel)

    monkeypatch.setattr(ci, "base_runs", lambda *a, **k: (
        ci.Run(run_id=2, head_sha="e080156", conclusion="success", status="completed",
               started_at=1.0, workflow="ci"),))
    spec = started.catalog.project("proj_a")
    store.hold_dispatch(rel, until=db.now() - 1)  # the hold lapsed; the tick looks again
    started.hold_red_release(spec, store)
    assert store.get_work_order(rel)["retry_after"] is None

    started.dispatch_pending(spec, store)

    assert store.get_work_order(rel)["status"] in ("dispatching", "running")
    argv = fake_claude.wait_calls(lambda c: "-p" in c["argv"])[-1]["argv"]
    assert argv[argv.index("--resume") + 1] == session
    assert "--session-id" not in argv
    assert "--worktree" not in argv, "the worktree already exists; the flag creates one"


def _resumable(daemon: Daemon, store: ProjectStore, project, wo_id: str) -> str:
    """The state a re-parked order is really in: a session on disk and a worktree."""
    from jarvis import claude_cli, worker_session

    session = "11111111-2222-4333-8444-555555555555"
    store.update_work_order(wo_id, session_id=session, worktree=wo_id)
    (project / ".claude" / "worktrees" / wo_id).mkdir(parents=True, exist_ok=True)
    row = store.get_work_order(wo_id)
    spec = daemon.catalog.project("proj_a")
    cwd = worker_session.worktree_path(spec, row) or spec.path
    path = claude_cli.session_transcript_path(cwd, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return session


# -- 5: past the long threshold ---------------------------------------------------


def test_past_the_long_threshold_it_parks_naming_the_red_run(started, store):
    """§3: six hours of red `main` and the release stops waiting. The attention line
    names the RED RUN and is re-derivable by `true_blockers`, or INV-ATTENTION-REASON
    relabels it on the next tick."""
    rel = release_order(store)
    base_health()
    ops.finish(rel, "blocked")
    backdate(store, rel, ops.RED_DEFER_EVENT, Daemon.RED_PARK_AFTER_SECONDS + 60)
    store.set_status(rel, "running")

    out = ops.finish(rel, "still blocked")

    assert out["status"] == "needs_review"
    wo = store.get_work_order(rel)
    assert len(events(store, rel, ops.RED_PARK_EVENT)) == 1
    blockers = invariants.true_blockers(store, wo)
    assert blockers[0] == wo["attention_reason"]
    assert "ci" in blockers[0] and RED_SHA[:10] in blockers[0] and RUN_URL in blockers[0]

    # Second call, nothing changed: `park_unlanded`'s episode discipline — no second
    # event and no re-flag, so nothing renotifies. A `finished` between the two would be
    # a re-DELIVERY, which is a new episode and does record.
    store.clear_attention(rel)

    assert ops.defer_red_release(store, "proj_a", wo) == "needs_review"

    assert len(events(store, rel, ops.RED_PARK_EVENT)) == 1
    assert not store.get_work_order(rel)["needs_attention"]


def test_the_threshold_runs_from_the_pending_hold_too(started, store):
    """§3: the dispatch hold (#807) and the mid-run re-park share one clock, so an order
    held red for six hours before it ever ran is not given six more."""
    rel = release_order(store, status="pending")
    store.add_event(rel, Daemon.RED_HOLD_EVENT, {"head_sha": RED_SHA, "base": "main"})
    backdate(store, rel, Daemon.RED_HOLD_EVENT, Daemon.RED_PARK_AFTER_SECONDS + 60)
    store.set_status(rel, "running")
    base_health()

    assert ops.finish(rel, "blocked")["status"] == "needs_review"
    assert len(events(store, rel, ops.RED_PARK_EVENT)) == 1


# -- the predicate ------------------------------------------------------------------


def test_a_delivered_release_is_never_re_parked(started, store, tmp_path, monkeypatch):
    """§1: ANY release effect, verified or not, counts as delivered — a claim that is
    unverified only because the restart has not happened yet must never be re-dispatched
    into a second `shipit.sh` run."""
    rel = release_order(store)
    base_health()
    marker = release.marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"wo_id": rel, "project": "proj_a",
                                  "version": "0.10.26", "tag": "jarvis-0.10.26",
                                  "staged_at": 1_758_000_000, "state": "staged"}))

    out = ops.finish(rel, "shipped jarvis-0.10.26")

    assert out["status"] != "pending"
    assert events(store, rel, ops.RED_DEFER_EVENT) == []


def test_an_ordinary_work_order_is_untouched(started, store):
    """The predicate is `release_for_issues` in the metadata and nothing else."""
    wo = ops.create_work_order("proj_a", "fix the bug")
    base_health()

    out = ops.finish(wo["id"], "done")

    assert out["status"] == "completed"
    assert events(store, wo["id"], ops.RED_DEFER_EVENT) == []


# -- 6: the tick that is the only step touching a held release --------------------


def _red_runs(monkeypatch, sha: str = RED_SHA) -> None:
    """`ci.base_runs` answering red on the base's newest head."""
    from jarvis import ci

    monkeypatch.setattr(ci, "base_runs", lambda *a, **k: (
        ci.Run(run_id=36273250195, head_sha=sha, conclusion="failure",
               status="completed", started_at=2.0, workflow="ci"),))


def test_the_daemon_tick_parks_a_release_held_red_past_the_threshold(
        started, store, monkeypatch):
    """§3: the threshold has to fire on the step that VISITS a held release.

    `defer_red_release` runs only when a worker DELIVERS, so a release re-parked to
    `pending` is touched by `hold_red_release` alone — which only ever extended the
    hold. A `main` red for a day left the release silently `pending` with no attention.
    """
    rel = release_order(store, status="pending")
    store.add_event(rel, Daemon.RED_HOLD_EVENT, {"head_sha": RED_SHA, "base": "main"})
    backdate(store, rel, Daemon.RED_HOLD_EVENT, Daemon.RED_PARK_AFTER_SECONDS + 60)
    _red_runs(monkeypatch)
    spec = started.catalog.project("proj_a")

    started.hold_red_release(spec, store)

    wo = store.get_work_order(rel)
    assert wo["status"] == "needs_review"
    assert len(events(store, rel, ops.RED_PARK_EVENT)) == 1
    assert invariants.true_blockers(store, wo)[0] == wo["attention_reason"]
    assert "ci" in wo["attention_reason"] and RED_SHA[:10] in wo["attention_reason"]

    # A parked order leaves the `pending` population, so the park cannot repeat.
    started.hold_red_release(spec, store)
    assert len(events(store, rel, ops.RED_PARK_EVENT)) == 1


def test_the_tick_under_the_threshold_only_extends_the_hold(
        started, store, monkeypatch):
    """§3's near miss: one minute short of six hours is still an ordinary red base."""
    rel = release_order(store, status="pending")
    store.add_event(rel, Daemon.RED_HOLD_EVENT, {"head_sha": RED_SHA, "base": "main"})
    backdate(store, rel, Daemon.RED_HOLD_EVENT, Daemon.RED_PARK_AFTER_SECONDS - 60)
    _red_runs(monkeypatch)

    started.hold_red_release(started.catalog.project("proj_a"), store)

    wo = store.get_work_order(rel)
    assert wo["status"] == "pending"
    assert events(store, rel, ops.RED_PARK_EVENT) == []
    assert abs(float(wo["retry_after"]) - (db.now() + Daemon.RED_HOLD_SECONDS)) < 10
    assert not wo["needs_attention"]


def test_the_first_red_tick_never_parks(started, store, monkeypatch):
    """§3's ordering: `_say_base_is_red` is what writes RED_HOLD_EVENT, so on the first
    red tick `first_red_hold` is None and there is no clock to be past."""
    rel = release_order(store, status="pending")
    _red_runs(monkeypatch)

    started.hold_red_release(started.catalog.project("proj_a"), store)

    assert store.get_work_order(rel)["status"] == "pending"
    assert events(store, rel, ops.RED_PARK_EVENT) == []
    assert len(events(store, rel, Daemon.RED_HOLD_EVENT)) == 1
