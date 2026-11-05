"""An empty pull request whose work reached the base settles itself (issue 1592)"""

# clock-exempt: class 3 — spin-waits on fleet.validating while a round runs on a real thread
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from jarvis import invariants, issues, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import VALIDATION_OUTAGE_LIMIT, Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

PR = "https://github.com/acme/proj/pull/7"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                           capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def project(tmp_path, claude_json) -> Path:
    proj = make_git_project(tmp_path, "proj_a")
    _git(proj, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    _git(proj, "remote", "add", "origin", "https://github.com/acme/proj.git")
    claude_json(proj)
    return proj


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, project) -> Daemon:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet", "max_in_flight": 50},
               "notifications": {"sinks": ["log"]},
               "validation": {"enabled": True, "max_rounds": 5}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}]}))
    ops.start_os(str(catalog), foreground=True)
    return Daemon(load_catalog(catalog))


def no_panel(store, round_row, packet):
    pytest.fail("a seat was asked about an empty pull request")


def overtaken(project, fake_gh, *, outcome="passed", base_moved=True,
              state="OPEN") -> tuple[ProjectStore, str]:
    """Round 1 settled on a real head; the pull request now reads empty; round 2 is open"""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "repair the tests")
    ops.finish(wo["id"], "opened a PR", pr_url=PR, evidence="ran the suite")
    _git(project, "checkout", "-qb", "work")
    head = _commit(project, "app.py", "mine\n", "the order's repair")
    _git(project, "checkout", "-q", "main")
    if base_moved:
        _commit(project, "app.py", "theirs\n", "the same repair (#1589)")
    _git(project, "update-ref", "refs/remotes/origin/main", "HEAD")

    first = store.latest_validation_round(wo_id=wo["id"])
    if first is None or first["outcome"]:
        first = store.open_validation_round(wo_id=wo["id"], fingerprint="fp1", round=1)
    store.set_validation_head(first["id"], head)
    store.set_validation_file_shas(first["id"], [("app.py", "abc")])
    store.close_validation_round(first["id"], outcome, "")
    store.open_validation_round(wo_id=wo["id"], fingerprint="fp2", round=2)
    store.set_status(wo["id"], "validating")
    fake_gh.set_pr_artifact(PR, title="[wo-1] repair", body="## Summary\nrepair", diff="",
                            files=[], checks=[], head_oid="b" * 40)
    fake_gh.set_pr(PR, state, checks=[], merge_state="CLEAN", head_oid="b" * 40)
    return store, wo["id"]


def run_round(fleet, store) -> None:
    fleet.validator = no_panel
    fleet.validation_tick(fleet.catalog.project("proj_a"), store)
    deadline = time.monotonic() + 15
    while fleet.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.validating, "a validation round never finished"


def closes(fake_gh) -> list[list[str]]:
    return [c["argv"][:3] for c in fake_gh.calls if c["argv"][:2] == ["pr", "close"]]


def escalated_generically(store, wo_id) -> bool:
    row = store.latest_validation_round(wo_id=wo_id)
    return (row["outcome"] == "escalated" and bool(store.get_work_order(wo_id)["needs_attention"])
            and store.get_work_order(wo_id)["status"] == "needs_review")


def test_the_order_settles_on_its_passed_round_with_nobody_paged(fleet, project, fake_gh):
    store, wo_id = overtaken(project, fake_gh)

    run_round(fleet, store)

    wo = store.get_work_order(wo_id)
    assert wo["status"] == "completed" and not wo["needs_attention"]
    assert wo["pr_state"] == "CLOSED"
    assert closes(fake_gh) == [["pr", "close", PR]]
    rounds = store.validation_rounds(wo_id=wo_id)
    assert [r["outcome"] for r in rounds] == ["passed", "void"]
    assert "(#1589)" in rounds[1]["reason"], "the record names what landed it"
    assert store.events_of_kind(wo_id, "pr_superseded_settled")
    # Nothing keeps asking about a pull request that will never merge
    assert not [v for check in (invariants.check_work_lands,
                                invariants.check_pull_request_recorded)
                for v in check(store) if v.wo_id == wo_id]
    assert issues.desired_state(store, wo) == issues.CLOSED


def test_a_poll_seeing_the_oss_own_close_does_not_call_it_refused(fleet, project, fake_gh):
    """The poll reads a row older than the close: the event is what it has to believe"""
    store, wo_id = overtaken(project, fake_gh)
    store.add_event(wo_id, "pr_superseded", {"pr_url": PR, "reason": "r"})
    fake_gh.set_pr(PR, "CLOSED", checks=[], merge_state="CLEAN", head_oid="b" * 40)

    fleet.poll_pull_requests(fleet.catalog.project("proj_a"), store)

    wo = store.get_work_order(wo_id)
    assert wo["status"] == "validating" and not wo["needs_attention"]
    assert not store.events_of_kind(wo_id, "pr_closed")


def test_a_branch_emptied_with_nothing_new_on_the_base_is_not_superseded(fleet, project, fake_gh):
    """A worker that reverted its own branch: closing it would lose the passed work"""
    store, wo_id = overtaken(project, fake_gh, base_moved=False)

    run_round(fleet, store)

    assert escalated_generically(store, wo_id)
    assert closes(fake_gh) == []


def test_no_passed_verdict_means_nothing_to_settle_on(fleet, project, fake_gh):
    store, wo_id = overtaken(project, fake_gh, outcome="rejected")

    run_round(fleet, store)

    assert escalated_generically(store, wo_id)
    assert closes(fake_gh) == []


def test_a_red_base_holds_the_settlement_back(fleet, project, fake_gh, monkeypatch):
    store, wo_id = overtaken(project, fake_gh)
    monkeypatch.setattr("jarvis.daemon.read_base_red", lambda *a: object())

    run_round(fleet, store)

    assert escalated_generically(store, wo_id)
    assert closes(fake_gh) == []


def test_a_refused_close_retries_then_settles(fleet, project, fake_gh, monkeypatch):
    store, wo_id = overtaken(project, fake_gh)
    monkeypatch.setenv("FAKE_GH_FAIL_PR_CLOSE", "HTTP 403: read-only token")

    run_round(fleet, store)

    wo = store.get_work_order(wo_id)
    assert wo["status"] == "validating" and not wo["needs_attention"]
    assert store.latest_validation_round(wo_id=wo_id)["outcome"] == "failed"

    monkeypatch.delenv("FAKE_GH_FAIL_PR_CLOSE")
    run_round(fleet, store)

    assert store.get_work_order(wo_id)["status"] == "completed"
    assert store.latest_validation_round(wo_id=wo_id)["outcome"] == "void"


def test_a_close_that_keeps_failing_names_the_real_cause(fleet, project, fake_gh, monkeypatch):
    store, wo_id = overtaken(project, fake_gh)
    monkeypatch.setenv("FAKE_GH_FAIL_PR_CLOSE", "HTTP 403: read-only token")

    for _ in range(VALIDATION_OUTAGE_LIMIT):
        run_round(fleet, store)

    assert store.get_work_order(wo_id)["status"] == "needs_review"
    assert "superseded" in store.latest_validation_round(wo_id=wo_id)["reason"]
    assert "HTTP 403" in store.latest_validation_round(wo_id=wo_id)["reason"]


def test_a_close_that_landed_before_a_crash_still_settles(fleet, project, fake_gh):
    """The event says the closure was ours, so the retry does not ask GitHub to close it twice"""
    store, wo_id = overtaken(project, fake_gh, state="CLOSED")
    store.add_event(wo_id, "pr_superseded", {"pr_url": PR, "reason": "r"})

    run_round(fleet, store)

    assert store.get_work_order(wo_id)["status"] == "completed"
    assert closes(fake_gh) == []
