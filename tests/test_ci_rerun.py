"""A cancelled check on a pull request the OS waits on is re-run once per head, then handed over (#1123)"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess as sp

import pytest

from jarvis import automerge, ci_rerun, github, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import true_blockers
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/1113"
HEAD = "a" * 40
NEW_HEAD = "b" * 40


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def parked(started, project, fake_gh):
    """Finished behind a pull request, waiting for it to merge"""
    wo = ops.create_work_order("proj_a", "section 7 stands")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    return wo


def check(name: str, conclusion: str, status: str = "COMPLETED",
          workflow: str = "ci") -> dict:
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion, "workflowName": workflow}


def run(run_id: int, sha: str, conclusion: str = "cancelled",
        workflow: str = "ci") -> dict:
    return {"databaseId": run_id, "headSha": sha, "conclusion": conclusion,
            "status": "completed", "startedAt": "2026-10-20T10:00:00Z",
            "workflowName": workflow}


#: The live case: wo-57061c15 / PR #1113
CANCELLED = [check("unit (3.13)", "CANCELLED"), check("lint", "SUCCESS")]


def pr(fake_gh, checks, head: str = HEAD, merge_state: str = "BLOCKED") -> None:
    fake_gh.set_pr(PR, "OPEN", checks=checks, merge_state=merge_state, head_oid=head)


def poll(daemon, store):
    daemon.poll_pull_requests(daemon.catalog.project("proj_a"), store)


def kinds(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


def test_a_cancelled_check_is_re_run_once_and_recorded(started, project, fake_gh,
                                                       parked):
    pr(fake_gh, CANCELLED)
    fake_gh.set_runs(HEAD, [run(555, HEAD)])
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == ["run rerun 555 --failed"]
    [event] = kinds(store, parked["id"], ci_rerun.RERUN_EVENT)
    assert event["head_sha"] == HEAD and event["run_ids"] == [555]
    assert "unit (3.13): CANCELLED" in event["checks"]
    assert true_blockers(store, store.get_work_order(parked["id"])) == []
    # The re-run is in flight: the poll waits and spends nothing more
    pr(fake_gh, [check("unit (3.13)", "", status="IN_PROGRESS"),
                 check("lint", "SUCCESS")])
    poll(started, store)
    assert len(fake_gh.reruns) == 1
    assert kinds(store, parked["id"], ci_rerun.STUCK_EVENT) == []


def test_a_re_run_cancelled_again_raises_one_attention_item_and_no_second_re_run(
        started, project, fake_gh, parked):
    pr(fake_gh, CANCELLED)
    fake_gh.set_runs(HEAD, [run(555, HEAD)])
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)     # the re-run came back CANCELLED too
    poll(started, store)

    assert len(fake_gh.reruns) == 1
    stuck = kinds(store, parked["id"], ci_rerun.STUCK_EVENT)
    assert len(stuck) == 1 and stuck[0]["cause"] == ci_rerun.CAUSE_CANCELLED_AGAIN
    blockers = true_blockers(store, store.get_work_order(parked["id"]))
    [line] = [b for b in blockers if PR in b]
    assert "`gh run rerun 555 --failed`" in line
    assert f"`gh pr update-branch {PR}`" in line


def test_a_new_head_gets_one_more_re_run_and_drops_the_old_item(started, project,
                                                                fake_gh, parked):
    pr(fake_gh, CANCELLED)
    fake_gh.set_runs(HEAD, [run(555, HEAD)])
    store = ProjectStore(project)
    poll(started, store)
    poll(started, store)
    assert store.get_work_order(parked["id"])["pr_checks_stuck_head"] == HEAD

    pr(fake_gh, CANCELLED, head=NEW_HEAD)
    fake_gh.set_runs(NEW_HEAD, [run(777, NEW_HEAD)])
    poll(started, store)

    assert fake_gh.reruns == ["run rerun 555 --failed", "run rerun 777 --failed"]
    row = store.get_work_order(parked["id"])
    assert row["pr_checks_stuck_head"] is None
    assert [e["head_sha"] for e in kinds(store, parked["id"],
                                         ci_rerun.RECOVERED_EVENT)] == [HEAD]
    assert not [b for b in true_blockers(store, row) if PR in b]


def test_a_green_pull_request_is_untouched(started, project, fake_gh, parked):
    pr(fake_gh, [check("unit (3.13)", "SUCCESS")], merge_state="CLEAN")
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == []
    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["run", "list"]]
    for kind in (ci_rerun.RERUN_EVENT, ci_rerun.STUCK_EVENT, ci_rerun.RECOVERED_EVENT):
        assert kinds(store, parked["id"], kind) == []


def test_a_failure_with_cancelled_siblings_is_never_re_run(started, project, fake_gh,
                                                           parked):
    """Fail-fast cancels the siblings of a real failure: that is the worker's to fix"""
    pr(fake_gh, [check("unit (3.11)", "FAILURE"), check("unit (3.13)", "CANCELLED")])
    fake_gh.set_runs(HEAD, [run(555, HEAD, conclusion="failure")])
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == []


def test_a_cancelled_check_github_does_not_block_on_is_left_alone(started, project,
                                                                  fake_gh, parked):
    pr(fake_gh, CANCELLED, merge_state="CLEAN")
    fake_gh.set_runs(HEAD, [run(555, HEAD)])
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == []


def test_no_run_to_re_run_goes_straight_to_the_user(started, project, fake_gh,
                                                    parked):
    pr(fake_gh, CANCELLED)
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == []
    [stuck] = kinds(store, parked["id"], ci_rerun.STUCK_EVENT)
    assert stuck["cause"] == ci_rerun.CAUSE_NO_RUN
    assert stuck["commands"] == [f"gh pr update-branch {PR}"]


def test_a_refused_re_run_is_the_one_attempt(started, project, fake_gh, parked):
    pr(fake_gh, CANCELLED)
    fake_gh.set_runs(HEAD, [run(555, HEAD)])
    fake_gh.refuse_rerun()
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    [stuck] = kinds(store, parked["id"], ci_rerun.STUCK_EVENT)
    assert stuck["cause"] == ci_rerun.CAUSE_REFUSED
    # `--failed`, then the whole run, once — and never again on this head
    assert fake_gh.reruns == ["run rerun 555 --failed", "run rerun 555"]


def test_a_required_check_that_never_reported_is_handed_over(started, project,
                                                             fake_gh, parked):
    """The branch's workflow predates main's: the required name is not on this head at all"""
    sp.run(["git", "-C", str(project), "remote", "add", "origin",
            "https://github.com/acme/proj.git"], check=True)
    fake_gh.set_protection("main", ["unit (3.13)"])
    pr(fake_gh, [check("unit", "SUCCESS")])
    store = ProjectStore(project)

    poll(started, store)
    poll(started, store)

    [stuck] = kinds(store, parked["id"], ci_rerun.STUCK_EVENT)
    assert stuck["cause"] == ci_rerun.CAUSE_NEVER_RAN
    assert "unit (3.13)" in stuck["checks"]
    assert fake_gh.reruns == []
    assert len([c for c in fake_gh.calls if "protection" in " ".join(c["argv"])]) == 1


def test_wo_why_names_a_cancelled_check_as_cancelled(started, project, fake_gh, parked):
    pr(fake_gh, CANCELLED)
    fake_gh.set_runs(HEAD, [run(555, HEAD)])
    store = ProjectStore(project)
    poll(started, store)

    actor = ops.scheduled_actor(store, store.get_work_order(parked["id"]))

    assert actor is not None
    assert "cancelled" in actor.why and "recomputing" not in actor.why
    assert "555" in actor.why


def test_the_merge_decision_names_a_cancelled_check():
    cfg = type("Cfg", (), {"enabled": True, "auto_merge": True})()
    pull = github.PullRequest(state="OPEN", mergeable="MERGEABLE", merge_state="BLOCKED",
                              head_oid=HEAD,
                              checks=github.read_checks({"statusCheckRollup": CANCELLED}))
    decision = automerge.decide({"outcome": "passed", "round": 1, "id": 1},
                                {"status": "waiting_pr_merge"}, pull, cfg,
                                validated_head=HEAD)
    assert decision.code == automerge.HELD_CHECKS_CANCELLED
    assert "unit (3.13): CANCELLED" in decision.reason


def test_this_module_builds_only_the_commands_it_declares():
    """`tests/test_base_heal.py`'s AST guard, for the one module allowed to re-run"""
    tree = ast.parse(inspect.getsource(ci_rerun))
    verbs = {tuple(e.value for e in node.elts[:2])
             for node in ast.walk(tree)
             if isinstance(node, ast.List) and len(node.elts) >= 2
             and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                     for e in node.elts[:2])}
    assert verbs <= set(ci_rerun.VERBS)
    assert set(ci_rerun.WRITE_VERBS) == {("run", "rerun")}
