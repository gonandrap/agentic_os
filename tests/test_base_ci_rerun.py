"""A default-branch run that judged nothing is re-run by the OS, once (#1193)"""

from __future__ import annotations

import json

import pytest

from jarvis import ci, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import with_origin

REPO = "acme/proj"
HEAD = "954ea22e000000000000000000000000000000aa"
RUN_ID = 39842661725


def row(conclusion: str, status: str = "completed", run_id: int = RUN_ID,
        sha: str = HEAD, workflow: str = "ci", started: str = "2026-10-22T19:44:05Z"):
    """One `gh run list` row"""
    return {"databaseId": run_id, "headSha": sha, "conclusion": conclusion,
            "status": status, "startedAt": started, "workflowName": workflow}


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    with_origin(project, REPO)
    return Daemon(load_catalog(catalog_file))


def poll(daemon, store):
    daemon.poll_default_branch(daemon.catalog.project("proj_a"), store)


def fact() -> dict:
    return json.loads(CentralStore().get_state("base_health:proj_a") or "{}")


def main_runs(fake_gh, rows):
    fake_gh.set_default_branch(REPO, "main")
    fake_gh.set_runs("main", list(rows))


def warnings(store) -> list[dict]:
    return [n for n in store.unrouted_notifications()
            if n["level"] == "warning" and "no CI verdict" in n["title"]]


def test_a_cancelled_run_on_main_is_re_run_once_and_is_not_red(started, project,
                                                               fake_gh):
    """The #1193 shape: a budget cancellation says nothing about the code, so the OS asks again"""
    main_runs(fake_gh, [row("cancelled")])
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == [f"run rerun {RUN_ID} --failed"]
    held = fact()
    assert held["red"] is False
    assert held["ci_reruns"] == {str(RUN_ID): "rerun"}
    assert warnings(store) == []


def test_the_re_run_in_flight_is_not_re_run_again(started, project, fake_gh):
    """`ci.latest` skips an unfinished run, so the state is what stops a second attempt"""
    main_runs(fake_gh, [row("cancelled")])
    store = ProjectStore(project)
    poll(started, store)

    main_runs(fake_gh, [row("", status="in_progress")])
    poll(started, store)
    main_runs(fake_gh, [row("success")])
    poll(started, store)

    assert fake_gh.reruns == [f"run rerun {RUN_ID} --failed"]
    # The run is green, so nothing is held against it any more
    assert fact()["ci_reruns"] == {}
    assert warnings(store) == []


def test_cancelled_again_after_the_one_re_run_asks_the_user_once(started, project,
                                                                 fake_gh):
    """The OS stops at one attempt and names the command rather than re-running for ever"""
    main_runs(fake_gh, [row("cancelled")])
    store = ProjectStore(project)

    for _ in range(3):
        poll(started, store)

    assert fake_gh.reruns == [f"run rerun {RUN_ID} --failed"]
    assert fact()["ci_reruns"] == {str(RUN_ID): "stuck"}
    told = warnings(store)
    assert len(told) == 1
    assert f"run rerun {RUN_ID}" in told[0]["body"]
    assert ci.run_url("acme", "proj", RUN_ID) in told[0]["body"]


def test_a_refused_re_run_spends_no_attempt(started, project, fake_gh):
    """Unread is not unrecoverable: the next tick asks again"""
    main_runs(fake_gh, [row("cancelled")])
    fake_gh.refuse_rerun()
    store = ProjectStore(project)

    poll(started, store)

    assert fact()["ci_reruns"] == {}
    assert warnings(store) == []


def test_a_green_or_failed_run_is_never_re_run(started, project, fake_gh):
    """Only a run that JUDGED NOTHING is re-run; a failure is a verdict on the code"""
    main_runs(fake_gh, [row("failure", workflow="ci"),
                        row("success", run_id=RUN_ID + 1, workflow="browser")])
    store = ProjectStore(project)

    poll(started, store)

    assert fake_gh.reruns == []
    assert fact()["red"] is True and fact()["ci_reruns"] == {}
