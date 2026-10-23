"""An order that wrote code must carry its pull request (INV-PR-RECORDED) (#42)"""

# clock-exempt: class 3 — one real time.sleep(0.01) so two work_abandoned timestamps differ; not a deadline loop
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
    """A real repository with a default branch — `test_work_lands.py`'s fixture"""
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
    """A work order with a real worktree, optionally carrying one commit"""
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


def _gate_records(store: ProjectStore, wo_id: str, pr_url: str) -> None:
    """Put `pr_url` on the record the way a planner's does — through an approved gate"""
    from jarvis import gates

    approval = store.add_approval(wo_id, gates.PR_MERGE, f"gh pr merge {pr_url} --squash")
    gates.apply_decision(store, approval["id"], verdict="approved",
                         reason="the plan's own docs PR", decided_by="neo")
    assert store.get_work_order(wo_id)["pr_url"] == pr_url
    # The verdict message reaches the worker before its turn settles
    for message in store.queued_messages(wo_id):
        store.mark_message(message["id"], "delivered")


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
    """Put an order into `completed` the way a pre-fix OS would have: straight in"""
    store = ProjectStore(project)
    try:
        store.set_status(wo_id, "completed")
    finally:
        store.close()


# -- what every settlement now writes down


def test_finishing_behind_a_pull_request_still_records_what_was_authored(started,
                                                                        project):
    """wo-5eedc84d's shape, caught at the moment it could still be caught"""
    wo = _order(project, code="launcher")

    assert ops.finish(wo["id"], "opened a PR", pr_url=PR)["status"] == "waiting_pr_merge"

    authored = _payload(project, wo["id"], "finished")["authored"]
    assert authored["commits"] == 1
    assert authored["branch"] == f"worktree-{wo['id']}"


def test_an_order_that_wrote_nothing_records_that_it_wrote_nothing(started, project):
    """`commits: 0` and "nobody looked" must stay distinguishable"""
    wo = _order(project, "answer a question")

    assert ops.finish(wo["id"], "no code needed")["status"] == "completed"

    authored = _payload(project, wo["id"], "finished")["authored"]
    assert authored["commits"] == 0 and authored["dirty"] == []
    assert landing.authored_in({"authored": authored}).produced is False


def test_a_settling_that_could_not_read_the_worktree_records_no_claim(started, project):
    """The worktree is deleted when a branch is reclaimed"""
    wo = _order(project, code="launcher")
    _git(project, "worktree", "remove", "--force", str(wo["worktree_path"]))

    ops.finish(wo["id"], "done", pr_url=PR)

    assert "authored" not in _payload(project, wo["id"], "finished")


# -- one test per settlement route: commits + no pull request never settles silently


def test_route_finish_refuses(started, project):
    """ROUTE 1 — `ops.finish`, the worker reporting its own result"""
    wo = _order(project, code="launcher")

    with pytest.raises(ops.OpsError, match="--abandon"):
        ops.finish(wo["id"], "done")

    assert _row(project, wo["id"])["status"] != "completed"


def test_route_finish_with_abandon_is_the_way_through(started, project):
    """The pairing every refusal needs"""
    wo = _order(project, code="spike")

    out = ops.finish(wo["id"], "spiked it", abandon="the approach does not work")

    assert out["status"] == "completed"
    assert _payload(project, wo["id"], "abandoned")["commits"] == 1
    assert _violations(project) == []


def test_route_assumption_review_parks(started, project):
    """ROUTE 2 — `ops.review_work_order`, the user accepting the assumptions"""
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
    """ROUTE 3 — `ops.accept_assumption`"""
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
    """ROUTE 4 — THE HOLE"""
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
        # The flag is raised once and stays
        flags = store.events_of_kind(wo["id"], "attention")
    finally:
        store.close()

    assert len(events) == 1, f"the park re-fired: {len(events)} work_unlanded events"
    assert len(flags) == 1, f"the flag re-fired: {len(flags)} attention events"
    assert _payload(project, wo["id"], "work_unlanded")["commits"] == 1


def test_an_acked_park_is_not_re_flagged_by_the_next_tick(started, project):
    """The same defect wearing the column instead of the event"""
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
    """THE PAIRING FOR THE DEDUPE ABOVE"""
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
    """The pairing, and the one that decides whether route 4's fix ships: the ~60 planners"""
    wo = _order(project, "answer a question")
    store = ProjectStore(project)
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="answered it")
        store.set_status(wo["id"], "running")
        store.flag_attention(wo["id"], "worker stopped without finishing")
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
        settled = store.get_work_order(wo["id"])
    finally:
        store.close()

    assert settled["status"] == "completed"
    assert settled["needs_attention"] == 0, "a settled no-code order still wants the user"
    assert settled["attention_reason"] is None


def test_the_reconciler_never_parks_a_gate_recorded_pull_request_in_the_merge_queue(
        started, project):
    """ROUTE 4's OTHER COLUMN READER"""
    wo = _order(project, "plan the feature", kind="planner")
    store = ProjectStore(project)
    try:
        _gate_records(store, wo["id"], PR)
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="submitted a plan")
        store.set_status(wo["id"], "running")
        started.settle_work_order(started.catalog.projects[0], store,
                                  store.get_work_order(wo["id"]))
        settled = store.get_work_order(wo["id"])
    finally:
        store.close()

    assert settled["status"] == "completed"
    assert settled["pr_url"] == PR      # recorded for every reader, inert for routing


def test_route_wo_done_records_rather_than_refusing(started, project):
    """ROUTE 5 — `jarvis wo done`"""
    wo = _order(project, code="stranded")

    assert ops.mark_done(wo["id"])["status"] == "completed"

    assert _payload(project, wo["id"], "work_unlanded")["closed_by"] == "marked_done"
    assert _violations(project) == []


def test_route_a_feature_manager_records_what_it_authored(started, project):
    """ROUTE 6 — `Daemon._close_feature_manager`"""
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


# -- a pushed branch with no pull request: the third value `unlanded_work` never had (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md)


def _pushed_order(project: Path, **kw) -> dict:
    """`_order`, with the branch pushed the way the worker's own `git push` leaves it."""
    wo = _order(project, **kw)
    _git(wo["worktree_path"], "push", "-q", "origin", f"worktree-{wo['id']}")
    return wo


def test_a_kind_that_does_not_land_code_is_still_refused_over_a_pushed_branch(started, project):
    """An analyst's pushed commit has no pull request route, so the refusal stands"""
    wo = _pushed_order(project, code="launcher", kind="analyst")

    with pytest.raises(ops.OpsError, match="does not land code"):
        ops.finish(wo["id"], "pushed a branch it may not land")

    store = ProjectStore(project)
    try:
        assert store.pr_pending_open(wo["id"]) is None
    finally:
        store.close()


def test_finish_no_longer_refuses_a_pushed_branch(started, project):
    """§2: no refusal, the ordinary path, and `land_finished` parks on `pr_pending`."""
    wo = _pushed_order(project, code="launcher")

    out = ops.finish(wo["id"], "pushed the branch — the gh account cannot open the PR")

    assert out["status"] == "needs_review"
    park = _payload(project, wo["id"], "pr_pending")
    assert park["branch"] == f"worktree-{wo['id']}"
    assert park["pushed"] is True and park["commits"] == 1
    assert park["was"] == "pending"      # the status it was held out of
    # Never the other episode too (§5)
    store = ProjectStore(project)
    try:
        assert store.events_of_kind(wo["id"], "work_unlanded") == []
        assert store.work_unlanded_open(wo["id"]) is False
        assert store.pr_pending_open(wo["id"]) is not None
    finally:
        store.close()


def test_finish_still_refuses_uncommitted_files(started, project):
    """§1"""
    wo = _pushed_order(project, code="launcher")
    (wo["worktree_path"] / "never_added.py").write_text(_feature("orphan"))

    with pytest.raises(ops.OpsError, match="--abandon"):
        ops.finish(wo["id"], "done")

    assert _row(project, wo["id"])["status"] != "needs_review"


def test_finish_still_refuses_a_commit_the_remote_does_not_have(started, project):
    """There is no URL that would show the user the code, so there is nothing better to say than today's refusal"""
    wo = _pushed_order(project, code="launcher")
    (wo["worktree_path"] / "second.py").write_text(_feature("second"))
    _git(wo["worktree_path"], "add", "-A")
    _git(wo["worktree_path"], "commit", "-qm", "after the push")

    with pytest.raises(ops.OpsError, match="--abandon"):
        ops.finish(wo["id"], "done")


def test_the_pr_pending_park_is_written_once_however_many_ticks_run(started, project):
    """`park_unlanded`'s once-per-episode discipline, mirrored (§2)"""
    wo = _pushed_order(project, code="scratch")
    store = ProjectStore(project)
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        store.finish_turn(turn["id"] if isinstance(turn, dict) else turn, "done")
        store.update_work_order(wo["id"], result_summary="pushed it")
        store.set_status(wo["id"], "running")
        for _ in range(5):
            started.settle_work_order(started.catalog.projects[0], store,
                                      store.get_work_order(wo["id"]))
            assert store.get_work_order(wo["id"])["status"] == "needs_review"
        parks = store.events_of_kind(wo["id"], "pr_pending")
        flags = store.events_of_kind(wo["id"], "attention")
    finally:
        store.close()

    assert len(parks) == 1, f"the park re-fired: {len(parks)} pr_pending events"
    assert len(flags) == 1, f"the flag re-fired: {len(flags)} attention events"


def test_accepting_the_assumptions_parks_on_the_specific_blocker(started, project):
    """§2's free half and §7.1"""
    wo = _pushed_order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    out = ops.review_work_order(wo["id"], accept=True)

    assert out["status"] == "needs_review"
    # §7.2
    assert out["pr_pending"]["branch"] == f"worktree-{wo['id']}"
    assert out["pr_pending"]["base"]
    store = ProjectStore(project)
    try:
        row = store.get_work_order(wo["id"])
        assert invariants.true_blockers(store, row)[0].startswith(
            f"branch `worktree-{wo['id']}` is pushed and has no pull request")
    finally:
        store.close()


def test_wo_show_renders_the_park_whatever_the_blocker_ranks(started, project):
    """§7.2"""
    from jarvis.timeline import build_timeline

    wo = _pushed_order(project, code="launcher")
    ops.finish(wo["id"], "pushed it")
    store = ProjectStore(project)
    try:
        entry = next(e for e in build_timeline(store.get_work_order(wo["id"]),
                                               store.list_events(wo["id"]), [])
                     if e["kind"] == "pr_pending")
    finally:
        store.close()

    assert entry["label"] == "Pushed, with no pull request yet"
    assert f"worktree-{wo['id']}" in entry["detail"]


# -- the invariant


def test_the_invariant_reports_a_settled_order_that_wrote_code_with_no_pull_request(
        started, project):
    """wo-5a6b2d6d's shape"""
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
    """wo-cd73c537's shape, and the scope line: #81 is recorded and merged while #116 is open on the same..."""
    wo = _order(project, code="console")
    ops.finish(wo["id"], "opened it", pr_url=PR)
    _complete(project, wo["id"])

    assert _violations(project) == []


def test_the_invariant_is_silent_on_an_order_that_produced_no_code(started, project):
    """THE NEGATIVE CONTROL THAT DECIDES WHETHER THIS SHIPS — 60 of the audit's 89 candidates"""
    wo = _order(project, "decide something")
    ops.finish(wo["id"], "decided")

    assert _row(project, wo["id"])["status"] == "completed"
    assert _violations(project) == []


def test_an_abandonment_excuses_that_settling_and_not_the_next_one(started, project):
    """An abandonment excuses the settling it was written with, and no more"""
    wo = _order(project, code="spike")
    ops.finish(wo["id"], "spiked", abandon="not worth landing")
    assert _violations(project) == []

    time.sleep(0.01)  # `work_abandoned` compares timestamps, not row order
    with pytest.raises(ops.OpsError, match="--abandon"):
        ops.finish(wo["id"], "delivered after all")


def test_the_invariant_is_silent_on_an_order_nothing_ever_looked_at(started, project):
    """wo-5eedc84d and wo-5a6b2d6d as they sit in production TODAY: settled long before anything recorded..."""
    wo = _order(project, code="launcher")
    _complete(project, wo["id"])

    assert _violations(project) == []


def test_the_invariant_costs_no_subprocess(started, project, monkeypatch):
    """It is in `INVARIANTS`, not `SLOW_INVARIANTS`, and runs on every reconcile tick"""
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


# -- the record reader


def test_both_payload_shapes_are_read_and_a_bare_one_is_not_invented():
    """`ops.finish` nests the record under `authored`"""
    record = {"branch": "b", "base": "origin/trunk", "commits": 2, "dirty": ["x.py"]}

    assert landing.authored_in({"authored": record}).commits == 2
    assert landing.authored_in({"reason": "dropped", **record}).dirty == ("x.py",)
    assert landing.authored_in({"summary": "no record here"}) is None


def test_the_newest_settlement_record_wins():
    """A work order sent back and re-delivered authored something different by the end"""
    old = {"branch": "b", "base": "origin/trunk", "commits": 1, "dirty": []}
    new = {"branch": "b", "base": "origin/trunk", "commits": 4, "dirty": []}

    assert landing.latest_authorship([(2.0, {"authored": new}),
                                      (1.0, {"authored": old})]).commits == 4
    assert landing.latest_authorship([(1.0, {"summary": "x"})]) is None
    assert landing.latest_authorship([]) is None
