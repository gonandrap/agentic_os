"""An order that wrote code must carry its pull request (INV-PR-RECORDED).

`tests/test_work_lands.py` proves the two landings that already refuse. This file proves
the other half of the user's 2026-09-18 ruling: EVERY route that settles a work order
either records the pull request, records a decision not to land, or refuses — and what
the order authored goes on the record so an invariant can still ask months later, when
the worktree is long deleted.

THE FIXTURE SHAPES ARE THE THREE PRODUCTION ORDERS the ruling was made on, and each test
names the one it is standing in for:

* `wo-5eedc84d` — `pr_url` NULL in the record while PR #42 existed the whole time and
  merged. The record simply never learned about it.
* `wo-cd73c537` — `pr_url` records #81, which merged, while #116 is open on the same
  branch with three unmerged commits. The record is STALE, which this predicate
  deliberately does not judge.
* `wo-5a6b2d6d` — a planner that completed over a WIP commit on `rescue/wo-5a6b2d6d`
  with no pull request, ever. Nothing refused it and nothing has reported it since.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from jarvis import invariants, landing, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _feature(salt: str, n: int = 30) -> str:
    return "".join(f"def {salt}_helper_{i}(argument):  # {salt} feature line {i}\n"
                   f"    return compute_{salt}_result({i}, argument)\n"
                   for i in range(n))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """A real repository with a default branch — `test_work_lands.py`'s fixture.

    `make_git_project` leaves an EMPTY repository, and with no default branch
    `landing.authored` answers `unreadable`: nothing is refused and nothing is recorded,
    so every assertion here would pass vacuously.
    """
    _git(project, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (project / "app.py").write_text(_feature("base", 10))
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    origin = project.parent / "acme" / "proj.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(project.parent, "init", "--bare", "-q", str(origin))
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "origin", "trunk")
    _git(project, "remote", "set-head", "origin", "trunk")
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _order(project: Path, title: str = "add feature X", *, code: str | None = None,
           kind: str = "worker", branch: str | None = None) -> dict:
    """A work order with a real worktree, optionally carrying one commit."""
    wo = ops.create_work_order("proj_a", title)
    wt = project / ".claude" / "worktrees" / wo["id"]
    _git(project, "worktree", "add", "-q", "-b", branch or f"worktree-{wo['id']}",
         str(wt), "trunk")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], worktree=wo["id"])
        if kind != "worker":
            store.conn.execute("UPDATE work_orders SET kind=? WHERE id=?",
                               (kind, wo["id"]))
            store.conn.commit()
    finally:
        store.close()
    if code:
        (wt / f"{code}.py").write_text(_feature(code))
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"add {code}")
    return {**wo, "worktree_path": wt}


def _row(project: Path, wo_id: str) -> dict:
    store = ProjectStore(project)
    try:
        return store.get_work_order(wo_id)
    finally:
        store.close()


def _payload(project: Path, wo_id: str, kind: str) -> dict:
    store = ProjectStore(project)
    try:
        return json.loads(store.events_of_kind(wo_id, kind)[-1]["payload"])
    finally:
        store.close()


def _violations(project: Path) -> list[invariants.Violation]:
    store = ProjectStore(project)
    try:
        return [v for v in invariants.check_project(store, repair=True)
                if v.invariant == "INV-PR-RECORDED"]
    finally:
        store.close()


def _complete(project: Path, wo_id: str) -> None:
    """Put an order into `completed` the way a pre-fix OS would have: straight in."""
    store = ProjectStore(project)
    try:
        store.set_status(wo_id, "completed")
    finally:
        store.close()


# -- what every settlement now writes down --------------------------------------------


def test_finishing_behind_a_pull_request_still_records_what_was_authored(started,
                                                                        project):
    """wo-5eedc84d's shape, caught at the moment it could still be caught.

    `unlanded_work` answers "nothing" for an order carrying a pull request WITHOUT
    looking at the worktree — right for the refusal, useless for the record. This is the
    only moment the worktree exists to say the order wrote code at all.
    """
    wo = _order(project, code="launcher")

    assert ops.finish(wo["id"], "opened a PR", pr_url=PR)["status"] == "waiting_pr_merge"

    authored = _payload(project, wo["id"], "finished")["authored"]
    assert authored["commits"] == 1
    assert authored["branch"] == f"worktree-{wo['id']}"


def test_an_order_that_wrote_nothing_records_that_it_wrote_nothing(started, project):
    """`commits: 0` and "nobody looked" must stay distinguishable, or an unreadable
    settling reads afterwards as an exoneration."""
    wo = _order(project, "answer a question")

    assert ops.finish(wo["id"], "no code needed")["status"] == "completed"

    authored = _payload(project, wo["id"], "finished")["authored"]
    assert authored["commits"] == 0 and authored["dirty"] == []
    assert landing.authored_in({"authored": authored}).produced is False


def test_a_settling_that_could_not_read_the_worktree_records_no_claim(started, project):
    """The worktree is deleted when a branch is reclaimed. "I cannot look" is not
    "there is nothing there", and writing a zero here would exonerate the order."""
    wo = _order(project, code="launcher")
    _git(project, "worktree", "remove", "--force", str(wo["worktree_path"]))

    ops.finish(wo["id"], "done", pr_url=PR)

    assert "authored" not in _payload(project, wo["id"], "finished")


# -- one test per settlement route: commits + no pull request never settles silently ---


def test_route_finish_refuses(started, project):
    """ROUTE 1 — `ops.finish`, the worker reporting its own result. It RAISES: the
    worker is listening and an exception is the only thing it reads."""
    wo = _order(project, code="launcher")

    with pytest.raises(ops.OpsError, match="--abandon"):
        ops.finish(wo["id"], "done")

    assert _row(project, wo["id"])["status"] != "completed"


def test_route_finish_with_abandon_is_the_way_through(started, project):
    """The pairing every refusal needs. The decision is what is enforced, not the
    outcome — and the abandonment carries the evidence the refusal saw."""
    wo = _order(project, code="spike")

    out = ops.finish(wo["id"], "spiked it", abandon="the approach does not work")

    assert out["status"] == "completed"
    assert _payload(project, wo["id"], "abandoned")["commits"] == 1
    assert _violations(project) == []


def test_route_assumption_review_parks(started, project):
    """ROUTE 2 — `ops.review_work_order`, the user accepting the assumptions. It PARKS
    rather than raising: an exception would break the user's command, and
    `park_unlanded` writes down what was there instead."""
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    assert ops.review_work_order(wo["id"], accept=True)["status"] == "needs_review"
    assert _payload(project, wo["id"], "work_unlanded")["commits"] == 1


def test_route_neo_accepting_an_assumption_parks_too(started, project):
    """ROUTE 3 — `ops.accept_assumption`, the OS settling an assumption on the user's
    behalf (`validation.auto_review`). It shares `_land_after_acceptance` with the
    user's route precisely so the two cannot disagree about this."""
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
        [pending] = store.pending_assumptions(wo["id"])
        out = ops.accept_assumption(
            store, project, store.get_work_order(wo["id"]), pending,
            reason="routine", model="sonnet", question_id=1, cfg=None)
        assert out["status"] == "needs_review"
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()

    assert _payload(project, wo["id"], "work_unlanded")["commits"] == 1


def test_route_the_reconciler_settling_a_turn_no_longer_walks_past_the_guard(started,
                                                                            project):
    """ROUTE 4 — THE HOLE. `Daemon.settle_work_order` completed any order whose turn
    ended with a `result_summary` and no `pr_url`, straight to `completed`, without ever
    asking `land_finished`. wo-5a6b2d6d's shape: a WIP commit, no pull request, ever.

    It also UNPARKED: `park_unlanded` leaves `needs_review` behind and this ran a tick
    later over the same done turn, completing the order it had just held.

    **AND THE SECOND TICK IS THE HALF THAT IS EASY TO GET WRONG.** This branch re-derives
    from the LATEST turn on EVERY tick, which is exactly why it unparked — so a park that
    is not idempotent trades "unparks and completes" for "re-parks, re-flags and writes a
    fresh `work_unlanded` every tick", the renotify-on-every-restart shape the user called
    out. Ticked five times: still `needs_review`, still ONE record, still one flag.
    """
    wo = _order(project, code="scratch", branch=f"rescue/{_order.__name__}-wip")
    store = ProjectStore(project)
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="wrote the design doc")
        store.set_status(wo["id"], "running")
        for _ in range(5):
            started.settle_work_order(started.catalog.projects[0], store,
                                      store.get_work_order(wo["id"]))
            assert store.get_work_order(wo["id"])["status"] == "needs_review"
        events = store.events_of_kind(wo["id"], "work_unlanded")
        # The flag is raised once and stays; a second `attention` event would be the
        # same defect arriving through the notifier instead of the timeline.
        flags = store.events_of_kind(wo["id"], "attention")
    finally:
        store.close()

    assert len(events) == 1, f"the park re-fired: {len(events)} work_unlanded events"
    assert len(flags) == 1, f"the flag re-fired: {len(flags)} attention events"
    assert _payload(project, wo["id"], "work_unlanded")["commits"] == 1


def test_an_acked_park_is_not_re_flagged_by_the_next_tick(started, project):
    """The same defect wearing the column instead of the event. `jarvis wo ack` puts the
    flag down for good, and the quiet path in `park_unlanded` must not raise it again —
    which is why that path asserts the status and leaves the flag to `true_blockers`,
    the only derivation that honours `acknowledged_blockers`."""
    wo = _order(project, code="scratch")
    store = ProjectStore(project)
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="wrote it")
        store.set_status(wo["id"], "running")
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
        assert store.get_work_order(wo["id"])["needs_attention"] == 1
    finally:
        store.close()

    ops.ack_attention(wo["id"])

    store = ProjectStore(project)
    try:
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
        assert store.get_work_order(wo["id"])["needs_attention"] == 0
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


def test_a_re_delivery_opens_a_new_episode_and_the_next_park_does_record(started,
                                                                        project):
    """THE PAIRING FOR THE DEDUPE ABOVE, and the reason it is keyed on the EPISODE rather
    than on "has this order ever been parked". A quiet second park would be the same
    class of defect one step along: the order silently stops being recorded as unlanded
    the moment it has been once. `work_unlanded_open` lapses on any `finished`,
    `abandoned` or `pr_merged` since the park, so a work order sent back and delivered
    again is judged afresh.
    """
    wo = _order(project, code="scratch")
    store = ProjectStore(project)
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="first go")
        store.set_status(wo["id"], "running")
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
    finally:
        store.close()

    ops.finish(wo["id"], "delivered behind a PR after all", pr_url=PR)  # ends the episode
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url="")   # ...and the record loses it again
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
        assert len(store.events_of_kind(wo["id"], "work_unlanded")) == 2
    finally:
        store.close()


def test_route_the_reconciler_still_completes_an_order_that_wrote_nothing(started,
                                                                         project):
    """The pairing, and the one that decides whether route 4's fix ships: the ~60
    planners, investigations and knowledge-base writes must settle exactly as before."""
    wo = _order(project, "answer a question")
    store = ProjectStore(project)
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="answered it")
        store.set_status(wo["id"], "running")
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
        assert store.get_work_order(wo["id"])["status"] == "completed"
    finally:
        store.close()


def test_route_wo_done_records_rather_than_refusing(started, project):
    """ROUTE 5 — `jarvis wo done`. It RECORDS and proceeds, and that is not silence: the
    user typing it IS the decision, and the documented exit for a pull request that will
    never merge (Neo question 430). Refusing would leave them no way to close the order.
    """
    wo = _order(project, code="stranded")

    assert ops.mark_done(wo["id"])["status"] == "completed"

    assert _payload(project, wo["id"], "work_unlanded")["closed_by"] == "marked_done"
    assert _violations(project) == []


def test_route_a_feature_manager_records_what_it_authored(started, project):
    """ROUTE 6 — `Daemon._close_feature_manager`. A manager is told it writes no product
    code and opens no pull request; one that did anyway used to complete with nothing
    written down. It still completes — parking it would flag a settled feature — but the
    evidence goes on the record, and the invariant is what reports it."""
    fo = ops.create_feature_order("proj_a", "a feature", description="do the thing")
    store = ProjectStore(project)
    try:
        manager = store.create_manager_order(fo["id"])
        wt = project / ".claude" / "worktrees" / manager["id"]
        store.update_work_order(manager["id"], worktree=manager["id"])
    finally:
        store.close()
    _git(project, "worktree", "add", "-q", "-b", f"worktree-{manager['id']}", str(wt),
         "trunk")
    (wt / "oops.py").write_text(_feature("oops"))
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "the manager wrote code")

    store = ProjectStore(project)
    try:
        started._close_feature_manager(store, fo["id"])
        assert store.get_work_order(manager["id"])["status"] == "completed"
    finally:
        store.close()

    assert _payload(project, manager["id"], "feature_settled")["authored"]["commits"] == 1
    assert [v.wo_id for v in _violations(project)] == [manager["id"]]


# -- the invariant ---------------------------------------------------------------------


def test_the_invariant_reports_a_settled_order_that_wrote_code_with_no_pull_request(
        started, project):
    """wo-5a6b2d6d's shape. The remedy names both ways out, because both are legitimate
    and the OS is not entitled to choose between them."""
    wo = _order(project, code="design_doc")
    ops.finish(wo["id"], "wrote it", pr_url=PR)
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url="")   # the record loses its PR
        store.set_status(wo["id"], "completed")
    finally:
        store.close()

    [violation] = _violations(project)
    assert violation.wo_id == wo["id"]
    assert "--pr <url>" in violation.detail and "--abandon" in violation.detail
    assert violation.repaired is False
    assert violation.context["commits"] == 1


def test_the_invariant_is_silent_when_the_pull_request_is_recorded(started, project):
    """wo-cd73c537's shape, and the scope line: #81 is recorded and merged while #116 is
    open on the same branch. This predicate asks whether an identifier is PRESENT, never
    whether it is CURRENT — that is a `gh` round trip and belongs to discovery."""
    wo = _order(project, code="console")
    ops.finish(wo["id"], "opened it", pr_url=PR)
    _complete(project, wo["id"])

    assert _violations(project) == []


def test_the_invariant_is_silent_on_an_order_that_produced_no_code(started, project):
    """THE NEGATIVE CONTROL THAT DECIDES WHETHER THIS SHIPS — 60 of the audit's 89
    candidates. A planner, an investigation, a knowledge-base write."""
    wo = _order(project, "decide something")
    ops.finish(wo["id"], "decided")

    assert _row(project, wo["id"])["status"] == "completed"
    assert _violations(project) == []


def test_an_abandonment_excuses_that_settling_and_not_the_next_one(started, project):
    """An abandonment excuses the settling it was written with, and no more. Put
    `work_abandoned` inside the predicate and an order excused once is excused for ever;
    the test that catches it is "abandon, then finish again ordinarily"."""
    wo = _order(project, code="spike")
    ops.finish(wo["id"], "spiked", abandon="not worth landing")
    assert _violations(project) == []

    time.sleep(0.01)  # `work_abandoned` compares timestamps, not row order
    with pytest.raises(ops.OpsError, match="--abandon"):
        ops.finish(wo["id"], "delivered after all")


def test_the_invariant_is_silent_on_an_order_nothing_ever_looked_at(started, project):
    """wo-5eedc84d and wo-5a6b2d6d as they sit in production TODAY: settled long before
    anything recorded what they authored. Structurally silent, and stated as a known gap
    rather than an exoneration — the user accepted it, and these are being closed by
    hand. Without this, a fleet's whole settled backlog would light up on upgrade."""
    wo = _order(project, code="launcher")
    _complete(project, wo["id"])

    assert _violations(project) == []


def test_the_invariant_costs_no_subprocess(started, project, monkeypatch):
    """It is in `INVARIANTS`, not `SLOW_INVARIANTS`, and runs on every reconcile tick.
    That is only defensible because it reads the record and never the repository."""
    wo = _order(project, code="launcher")
    ops.finish(wo["id"], "done", pr_url=PR)
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url="")
        store.set_status(wo["id"], "completed")
    finally:
        store.close()

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: pytest.fail("the invariant shelled out"))
    store = ProjectStore(project)
    try:
        found = [v for v in invariants.check_pull_request_recorded(store)]
    finally:
        store.close()
    assert [v.wo_id for v in found] == [wo["id"]]


# -- the record reader -----------------------------------------------------------------


def test_both_payload_shapes_are_read_and_a_bare_one_is_not_invented():
    """`ops.finish` nests the record under `authored`; `park_unlanded` and `--abandon`
    spread it at the top level beside their own keys. One reader serves both."""
    record = {"branch": "b", "base": "origin/trunk", "commits": 2, "dirty": ["x.py"]}

    assert landing.authored_in({"authored": record}).commits == 2
    assert landing.authored_in({"reason": "dropped", **record}).dirty == ("x.py",)
    assert landing.authored_in({"summary": "no record here"}) is None


def test_the_newest_settlement_record_wins():
    """A work order sent back and re-delivered authored something different by the end,
    and the record of what it produced has to move with it."""
    old = {"branch": "b", "base": "origin/trunk", "commits": 1, "dirty": []}
    new = {"branch": "b", "base": "origin/trunk", "commits": 4, "dirty": []}

    assert landing.latest_authorship([(2.0, {"authored": new}),
                                      (1.0, {"authored": old})]).commits == 4
    assert landing.latest_authorship([(1.0, {"summary": "x"})]) is None
    assert landing.latest_authorship([]) is None
