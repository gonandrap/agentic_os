"""`jarvis validation force`: a person opens a round, and no worker is pretended.

docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md.

THE BUG THIS EXISTS FOR is a round that recorded NO judged commit. Every round judged
before jarvis-0.10.0 carries `head_sha=''`, so `automerge.decide`'s condition 4 holds
those work orders on `sha_unrecorded` for ever. So every test here that is about the
mechanism starts from a round with `head_sha=''` — a fixture that armed a round with a
commit already on it would pass while fixing nothing.

The other half is the RECORD. The old route (re-running `jarvis wo finish` on a parked
work order) worked and lied: a `finished` event with no worker behind it, a summary the
operator had to author, and nothing afterwards saying a human forced a re-judgement. The
assertions about what is NOT written are as load-bearing as the ones about what is.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

PR = "https://github.com/acme/proj/pull/7"
#: What round 1 recorded on every work order judged before `head_sha` shipped.
UNRECORDED = ""
LIVE_HEAD = "c0ffee11c0ffee22c0ffee33c0ffee44c0ffee55"
GREEN = [{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
          "conclusion": "SUCCESS"}]


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def write_catalog(tmp_path: Path, project: Path, **validation) -> Path:
    """A catalog ON DISK with the panel on.

    On disk and not in memory because `ops.force_validation` runs in the OPERATOR's
    process, not the daemon's: it resolves its `ValidationConfig` through
    `ops.validation_config`, which re-reads the catalog file. A test that only flipped
    `daemon.catalog…validation.enabled` would exercise a switch this command never reads.
    """
    data = {
        "os": {
            "defaults": {"model": "sonnet", "max_in_flight": 50},
            "notifications": {"sinks": ["log"]},
            "validation": {"enabled": True, **validation},
        },
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def project(tmp_path, claude_json) -> Path:
    """A real repository whose `origin` matches `PR` — the collector reads both.

    The branch is named explicitly: the evidence collector resolves its base through a
    ladder ending at `main`, and a repository whose `git init` happened to name the
    branch something else would collect an empty packet and escalate every round here for
    a reason that has nothing to do with this feature (kn-4b6f18f5).
    """
    proj = make_git_project(tmp_path, "proj_a")
    _git(proj, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    _git(proj, "remote", "add", "origin", "https://github.com/acme/proj.git")
    claude_json(proj)
    return proj


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, project):
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    return Daemon(load_catalog(catalog_path))


class Panel:
    """The injected validator seam, driven by the test.

    Records the ROUND NUMBER it was handed, because "it was judged again" and "it was
    judged again as a new round" are different claims and several tests here want the
    second one.
    """

    def __init__(self, outcome: str = "passed"):
        self.outcome = outcome
        self.rounds: list[int] = []

    def __call__(self, store, round_row, packet):
        self.rounds.append(int(round_row["round"]))
        return {"outcome": self.outcome, "reason": "",
                "seats": [{"seat": "tester", "status": "ok",
                           "verdict": "pass" if self.outcome == "passed" else "reject",
                           "model": "sonnet", "latency_ms": 3, "reply": "ok"}]}


def parked(fleet, project, *, judged: str = UNRECORDED, outcome: str = "passed",
           pr_url: str | None = PR) -> tuple[ProjectStore, dict]:
    """A work order parked in `waiting_pr_merge` with ONE settled round on `judged`.

    `judged=''` by default: that IS the production population. It reaches the parking
    through the real `ops.finish`, so the `finished` event these tests count is a genuine
    one and "no SECOND finished event" is the claim it supports.
    """
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=pr_url, evidence="ran the suite")
    # `finish` opened round 1 itself (the panel is on). Settle it the way the pre-0.10.0
    # daemon did: a verdict, and no commit recorded beside it.
    round_row = store.latest_validation_round(wo_id=wo["id"])
    assert round_row is not None and int(round_row["round"]) == 1
    store.set_validation_head(round_row["id"], judged)
    if outcome != "pending":
        store.close_validation_round(round_row["id"], outcome, "")
    store.set_status(wo["id"], "waiting_pr_merge")
    return store, store.get_work_order(wo["id"])


def judge(fleet, store, panel: Panel, timeout: float = 15.0) -> None:
    """Run whatever round is open to a verdict, off the daemon's tick thread."""
    fleet.validator = panel
    fleet.validation_tick(fleet.catalog.project("proj_a"), store)
    deadline = time.monotonic() + timeout
    while fleet.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.validating, "a validation round never finished"


def artifact(fake_gh, *, head_oid: str = LIVE_HEAD) -> None:
    """Register the pull request BOTH readers see — the collector and the poll."""
    fake_gh.set_pr_artifact(
        PR, title="[wo-1] add feature X", body="## Summary\nit adds feature X",
        diff="diff --git a/app.py b/app.py\n@@ -0,0 +1 @@\n+feature\n",
        files=[{"path": "app.py", "additions": 1, "deletions": 0}],
        checks=GREEN, head_oid=head_oid)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=head_oid)


# -- the bug: a round that recorded no commit gets one -------------------------------


def test_a_forced_round_records_the_commit_the_round_before_it_could_not(
        fleet, project, fake_gh):
    """THE WHOLE BUG, start to finish. Round 1 passed and bound the verdict to nothing;
    the forced round reads the CURRENT pull request and records the CURRENT head, which
    is the fact `automerge.decide` needs and could never obtain."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) is None

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("passed"))

    rounds = store.validation_rounds(wo_id=wo["id"])
    assert [r["round"] for r in rounds] == [1, 2]
    assert rounds[0]["head_sha"] == ""        # untouched — history is not rewritten
    assert rounds[1]["head_sha"] == LIVE_HEAD
    assert ProjectStore.validated_head(rounds[1]) == LIVE_HEAD


def test_the_forced_round_is_what_finally_lets_the_pull_request_merge_itself(
        fleet, project, fake_gh):
    """The motivating outcome, and why stopping at the column would prove too little: the
    point is not that a commit was written down, it is that condition 4 can now be
    satisfied and the OS asks to merge."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    spec = fleet.catalog.project("proj_a")
    spec.validation.enabled = spec.validation.auto_merge = True

    fleet.poll_pull_requests(spec, store)
    assert store.list_approvals(wo["id"]) == []  # held: the panel named no commit

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("passed"))
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"

    fleet.poll_pull_requests(spec, store)
    assert [a["kind"] for a in store.list_approvals(wo["id"])] == ["auto_merge"]


def test_a_forced_round_re_reads_the_pull_request_rather_than_the_old_packet(
        fleet, project, fake_gh):
    """The evidence is collected FRESH, so a head that moved since the last round is the
    head this one records. Re-using a stored packet would record a commit that is no
    longer there and hold the merge on `sha_moved` instead."""
    store, wo = parked(fleet, project)
    artifact(fake_gh, head_oid="aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")
    ops.force_validation(wo["id"], reason="first attempt")
    judge(fleet, store, Panel("passed"))

    artifact(fake_gh, head_oid=LIVE_HEAD)  # the worker pushed
    ops.force_validation(wo["id"], reason="it moved under us")
    judge(fleet, store, Panel("passed"))

    rounds = store.validation_rounds(wo_id=wo["id"])
    assert rounds[-1]["round"] == 3 and rounds[-1]["head_sha"] == LIVE_HEAD


# -- the record: no worker finished, and the reason survives --------------------------


def test_forcing_a_round_never_claims_a_worker_reported_a_result(fleet, project,
                                                                fake_gh):
    """The defect in the route this replaces. `ops.finish` writes a `finished` event and
    overwrites `result_summary`, so re-running it to get a round made the timeline say a
    worker had delivered again. Exactly one `finished` event is on this record, and it is
    the worker's own."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    before = store.events_of_kind(wo["id"], "finished")

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("passed"))

    assert store.events_of_kind(wo["id"], "finished") == before
    assert len(before) == 1
    assert store.get_work_order(wo["id"])["result_summary"] == "opened a PR"


def test_the_reason_is_recoverable_afterwards_from_the_round_and_the_timeline(
        fleet, project, fake_gh):
    """"Forced, and why" has to outlive the terminal it was typed in — a re-judgement
    that reads afterwards like a worker's own re-delivery is the defect this removes."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")

    forced = store.latest_validation_round(wo_id=wo["id"])
    assert forced is not None
    assert forced["forced_reason"] == "round 1 predates head_sha"
    payload = json.loads(
        store.events_of_kind(wo["id"], "validation_forced")[-1]["payload"])
    assert payload["reason"] == "round 1 predates head_sha"
    assert payload["round"] == 2 and payload["was"] == "waiting_pr_merge"


def test_every_surface_that_prints_a_round_says_it_was_forced(fleet, project, fake_gh):
    """One formatter, so `wo show`, `fo show` and both dashboard pages cannot word it
    differently — and the projection they read has to carry the column at all."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    ops.force_validation(wo["id"], reason="round 1 predates head_sha")

    rounds = ops.validation_rounds(store, wo_id=wo["id"])
    assert "forced: round 1 predates head_sha" in ops.round_line(rounds[-1])
    # ...and an ordinary round says nothing: a line announcing "not forced" on every
    # round in the fleet would spend attention on the absence of an event.
    assert "forced" not in ops.round_line(rounds[0])


def test_the_round_is_numbered_and_counted_like_any_other(fleet, project, fake_gh):
    """Neo, question 300. A forced round SPENDS a number — `counted_validation_rounds`
    both counts rounds and numbers them, so a round exempted from the count would reuse a
    taken number, hit the idempotent insert and hand back an already-closed round while
    the work order parked in `validating` for ever."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    ops.force_validation(wo["id"], reason="once")
    judge(fleet, store, Panel("passed"))
    ops.force_validation(wo["id"], reason="twice")
    judge(fleet, store, Panel("passed"))

    assert [r["round"] for r in store.validation_rounds(wo_id=wo["id"])] == [1, 2, 3]
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 3


def test_a_rejection_past_the_budget_comes_to_the_user_and_not_to_a_worker(
        fleet, project, fake_gh):
    """The stated `max_rounds` behaviour (spec §4), and why it is the right one: the work
    order this command is for has no worker left to send feedback to."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    fleet.catalog.project("proj_a").validation.max_rounds = 1

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("rejected"))

    assert store.events_of_kind(wo["id"], "validation_escalated")
    assert store.events_of_kind(wo["id"], "validation_rejected") == []
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review" and fresh["attention_reason"]


# -- what it refuses, and each refusal bites ------------------------------------------


def test_a_blank_reason_is_refused(fleet, project, fake_gh):
    store, wo = parked(fleet, project)
    with pytest.raises(ops.OpsError, match="reason"):
        ops.force_validation(wo["id"], reason="   ")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


def test_a_work_order_with_no_pull_request_is_refused(fleet, project, fake_gh):
    """A fresh round would read the worktree and record `''` — the very state this
    command exists to get out of, so opening one would spend a round for nothing."""
    store, wo = parked(fleet, project, pr_url=None)
    with pytest.raises(ops.OpsError, match="no pull request"):
        ops.force_validation(wo["id"], reason="try anyway")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


@pytest.mark.parametrize("outcome", ["pending", "failed"])
def test_a_round_the_machine_still_owns_is_refused(fleet, project, fake_gh, outcome):
    """`ProjectStore.round_machine_owns`, the round machine's own predicate rather than a
    second copy of the rule: a round opened underneath one about to run takes the
    MAX-round slot out from under it. `failed` counts — it is an outage the machine
    retries, not a verdict."""
    store, wo = parked(fleet, project, outcome=outcome)
    with pytest.raises(ops.OpsError, match=f"is {outcome} — the panel has not finished"):
        ops.force_validation(wo["id"], reason="impatient")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


@pytest.mark.parametrize("outcome", ["pending", "failed", "passed", "rejected",
                                     "escalated"])
def test_the_predicate_and_the_sentence_come_from_one_read(fleet, project, outcome):
    """kn-08f2ff9b: the panel opens rounds on another thread, so a refusal that asked SQL
    for the predicate and then re-fetched the row for the wording could refuse while
    naming a round that had since settled. `round_machine_owns` takes the ROW — it
    cannot re-fetch — and `validation_round_open` is derived from it, so the two can
    never answer differently."""
    store, _ = parked(fleet, project, outcome=outcome)
    wo = ops.create_work_order("proj_a", "another")
    round_row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp")
    if outcome != "pending":
        store.close_validation_round(round_row["id"], outcome, "")

    row = store.latest_validation_round(wo_id=wo["id"])
    assert ProjectStore.round_machine_owns(row) == store.validation_round_open(wo["id"])
    # ...and a work order with no round at all is owned by nobody, both ways.
    assert ProjectStore.round_machine_owns(None) is False


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_a_settled_work_order_is_refused(fleet, project, fake_gh, status):
    """Opening a round would put it back in `validating` — reopening work the user or the
    OS has closed, on the strength of a verdict that could change nothing."""
    store, wo = parked(fleet, project)
    store.set_status(wo["id"], status)
    with pytest.raises(ops.OpsError, match=status):
        ops.force_validation(wo["id"], reason="re-judge it")
    assert store.get_work_order(wo["id"])["status"] == status


def test_a_work_order_that_does_not_exist_is_refused(fleet, project):
    with pytest.raises(ops.OpsError, match="not found"):
        ops.force_validation("wo-deleted", reason="re-judge it")


def test_the_panel_being_off_refuses_rather_than_opening_a_round_nothing_judges(
        tmp_path, jarvis_home, fake_claude, project, fake_gh):
    """`os.validation.enabled` is read at the SUBMISSION SITES ONLY, and this is a new
    submission site. Settling is deliberately NOT gated on it (a switch flipped at three
    in the morning must not strand open rounds), so a round opened here with the panel
    off would be judged by nothing and park the work order for ever."""
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    store, wo = parked(daemon, project)

    data = json.loads(catalog_path.read_text())
    data["os"]["validation"]["enabled"] = False
    catalog_path.write_text(json.dumps(data))

    with pytest.raises(ops.OpsError, match="validation panel is off"):
        ops.force_validation(wo["id"], reason="re-judge it")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1
