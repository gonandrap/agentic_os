"""A pull request whose head moved past its verdict re-judges itself (docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md)"""

# clock-exempt: class 3 — spin-waits on fleet.validating twice while the moved head is re-judged on a real thread
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jarvis import automerge, db, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

PR = "https://github.com/acme/proj/pull/7"
JUDGED = "aaaa1111aaaa2222aaaa3333aaaa4444aaaa5555"
#: What the conflict resolution pushed: the merge commit no seat has read
PUSHED = "bbbb1111bbbb2222bbbb3333bbbb4444bbbb5555"
AGAIN = "cccc1111cccc2222cccc3333cccc4444cccc5555"
GREEN = [{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
          "conclusion": "SUCCESS"}]
RUNNING = [{"__typename": "CheckRun", "name": "unit", "status": "IN_PROGRESS",
            "conclusion": ""}]
RED = [{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
        "conclusion": "FAILURE"}]


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def write_catalog(tmp_path: Path, project: Path, **validation) -> Path:
    """A catalog ON DISK: `ops.force_validation_refusal`"""
    data = {
        "os": {
            "defaults": {"model": "sonnet", "max_in_flight": 50},
            "notifications": {"sinks": ["log"]},
            "validation": {"enabled": True, "auto_merge": True, **validation},
        },
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def project(tmp_path, claude_json) -> Path:
    """A real repository whose `origin` matches `PR`"""
    proj = make_git_project(tmp_path, "proj_a")
    _git(proj, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    _git(proj, "remote", "add", "origin", "https://github.com/acme/proj.git")
    claude_json(proj)
    return proj


def boot(tmp_path, project, **validation) -> Daemon:
    """A started OS whose project has the panel and the automatic merge on"""
    ops.start_os(str(write_catalog(tmp_path, project, **validation)), foreground=True)
    return Daemon(load_catalog(tmp_path / "catalog.json"))


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, project):
    """ROOM IN THE ROUND BUDGET, deliberately: the shipped `max_rounds` is 3 and the OS never spends the..."""
    return boot(tmp_path, project, max_rounds=5)


class Panel:
    """The injected validator seam"""

    def __init__(self, outcome: str = "passed"):
        self.outcome = outcome
        self.rounds: list[int] = []

    def __call__(self, store, round_row, packet):
        self.rounds.append(int(round_row["round"]))
        return {"outcome": self.outcome, "reason": "",
                "seats": [{"seat": "tester", "status": "ok",
                           "verdict": "pass" if self.outcome == "passed" else "reject",
                           "model": "sonnet", "latency_ms": 3, "reply": "ok"}]}


def parked(project, *, judged: str = JUDGED, outcome: str = "passed",
           rounds: int = 1) -> tuple[ProjectStore, dict]:
    """An order parked behind its pull request with `rounds` settled rounds, the last of them on `judged`"""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=PR, evidence="ran the suite")
    for n in range(1, rounds + 1):
        row = store.latest_validation_round(wo_id=wo["id"])
        if row is None or row["outcome"]:
            row = store.open_validation_round(wo_id=wo["id"], fingerprint=f"fp{n}",
                                              round=n)
        store.set_validation_head(row["id"], judged)
        store.close_validation_round(row["id"], outcome, "")
    store.set_status(wo["id"], "waiting_pr_merge")
    return store, store.get_work_order(wo["id"])


def artifact(fake_gh, *, head_oid: str, checks=GREEN,
             merge_state: str = "CLEAN") -> None:
    """The pull request BOTH readers see — the evidence collector and the poll"""
    fake_gh.set_pr_artifact(
        PR, title="[wo-1] add feature X", body="## Summary\nit adds feature X",
        diff="diff --git a/app.py b/app.py\n@@ -0,0 +1 @@\n+feature\n",
        files=[{"path": "app.py", "additions": 1, "deletions": 0}],
        checks=checks, head_oid=head_oid)
    fake_gh.set_pr(PR, "OPEN", checks=checks, merge_state=merge_state,
                   head_oid=head_oid)


def poll(fleet, store) -> None:
    fleet.poll_pull_requests(fleet.catalog.project("proj_a"), store)


def judge(fleet, store, panel: Panel, timeout: float = 15.0) -> None:
    """Run whatever round is open to a verdict, off the daemon's tick thread"""
    fleet.validator = panel
    fleet.validation_tick(fleet.catalog.project("proj_a"), store)
    deadline = time.monotonic() + timeout
    while fleet.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.validating, "a validation round never finished"


class HeldPanel(Panel):
    """A panel stopped MID-ROUND, so the pull request can settle underneath it"""

    def __init__(self, outcome: str = "passed"):
        super().__init__(outcome)
        self.entered = threading.Event()
        self.released = threading.Event()

    def __call__(self, store, round_row, packet):
        self.entered.set()
        assert self.released.wait(15), "the held panel was never released"
        return super().__call__(store, round_row, packet)


def hold(fleet, store, panel: HeldPanel) -> None:
    """Start the open round and leave it inside the validator"""
    fleet.validator = panel
    fleet.validation_tick(fleet.catalog.project("proj_a"), store)
    assert panel.entered.wait(15), "the round never reached the panel"


def release(fleet, panel: HeldPanel, timeout: float = 15.0) -> None:
    panel.released.set()
    deadline = time.monotonic() + timeout
    while fleet.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.validating, "the released round never finished"


def rounds_of(store, wo) -> list[int]:
    return [int(r["round"]) for r in store.validation_rounds(wo_id=wo["id"])]


# -- the bug, end to end


def test_the_head_the_os_moved_is_re_judged_and_the_pull_request_then_merges(
        fleet, project, fake_gh, local_base):
    """THE WHOLE BUG"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)                      # the head moved — re-judge it

    assert store.get_work_order(wo["id"])["status"] == "validating"
    judge(fleet, store, Panel("passed"))
    row = store.get_work_order(wo["id"])
    assert row["status"] == "waiting_pr_merge"
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) == PUSHED

    poll(fleet, store)

    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    assert PUSHED in approvals[0]["command"]


def test_nobody_had_to_ask_and_no_worker_was_pretended(fleet, project, fake_gh):
    """The two halves of the record"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    before = len(store.events_of_kind(wo["id"], "finished"))

    poll(fleet, store)

    assert len(store.events_of_kind(wo["id"], "finished")) == before
    forced = store.events_of_kind(wo["id"], "validation_forced")
    payload = db.from_json(forced[-1]["payload"], {})
    assert payload["by"] == ops.REJUDGE_BY_OS
    assert payload["head_sha"] == PUSHED and payload["judged_sha"] == JUDGED
    # Both commits in the sentence stored on the round itself
    reason = store.latest_validation_round(wo_id=wo["id"])["forced_reason"]
    assert JUDGED[:10] in reason and PUSHED[:10] in reason

    from jarvis import timeline
    label, _ = timeline._describe("validation_forced", payload)
    assert "by the OS" in label and "by hand" not in label


# -- the guards


def test_one_round_per_commit_and_not_one_per_tick(fleet, project, fake_gh):
    """A parked pull request is polled every couple of minutes"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)
    judge(fleet, store, Panel("passed"))
    poll(fleet, store)
    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]


def test_a_head_that_moves_again_is_judged_again(fleet, project, fake_gh):
    """The other side of that key: the dedupe is per COMMIT"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    judge(fleet, store, Panel("passed"))

    artifact(fake_gh, head_oid=AGAIN)
    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2, 3]
    assert store.get_work_order(wo["id"])["status"] == "validating"


@pytest.mark.parametrize("checks,merge_state", [(RUNNING, "CLEAN"), (RED, "CLEAN"),
                                                (GREEN, "DIRTY")])
def test_a_pull_request_that_is_not_otherwise_ready_is_left_alone(
        fleet, project, fake_gh, checks, merge_state):
    """GUARD 2"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED, checks=checks, merge_state=merge_state)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    # ...and the hold is still recorded, so the surfaces still say what is going on
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_SHA_MOVED


def test_a_branch_somebody_is_still_typing_into_is_not_judged(fleet, project, fake_gh):
    """GUARD 4"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    store.create_turn(wo["id"], "message", "resolve the conflict")

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]


def test_a_message_already_on_its_way_holds_it_too(fleet, project, fake_gh):
    """GUARD 4, the other half: a queued message is a turn about to start"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    store.queue_message(wo["id"], "one more thing")

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]


def test_a_rejected_round_is_never_reopened_by_the_machine(fleet, project, fake_gh):
    """OUT OF SCOPE AND IT STAYS THERE (spec §6)"""
    store, wo = parked(project, outcome="rejected")
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_NOT_PASSED


def test_a_project_that_never_opted_in_is_not_re_judged_either(tmp_path, project,
                                                               jarvis_home, fake_claude,
                                                               fake_gh):
    """The panel's own switch, shared with the person's command through `ops.force_validation_refusal`: a..."""
    fleet = boot(tmp_path, project, auto_merge=False)
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    assert store.events_of_kind(wo["id"], "automerge_held") == []


# -- the last round is the user's


def test_the_machine_never_spends_the_last_round(tmp_path, project, jarvis_home,
                                                 fake_claude, fake_gh):
    """GUARD 6"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]
    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert len(declined) == 1
    payload = db.from_json(declined[0]["payload"], {})
    assert payload["head_sha"] == PUSHED and payload["next_round"] == 3
    # ...and the person still can: the round it left is theirs to spend
    ops.force_validation(wo["id"], reason="I read the merge commit myself")
    assert rounds_of(store, wo) == [1, 2, 3]


def test_the_one_stall_it_cannot_heal_is_the_one_it_reports(tmp_path, project,
                                                            jarvis_home, fake_claude,
                                                            fake_gh):
    """§4"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    row = store.get_work_order(wo["id"])
    assert invariants.true_blockers(store, row)[0] == invariants.SHA_MOVED_BLOCKER
    # The flag itself comes from INV-ATTENTION-MISSING, which is the path that honours an ack
    assert not row["needs_attention"]
    list(invariants.check_blocked_work_is_surfaced(store))
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"] and row["attention_reason"] == \
        invariants.SHA_MOVED_BLOCKER


def test_the_decline_is_recorded_once_and_an_ack_stays_down(tmp_path, project,
                                                            jarvis_home, fake_claude,
                                                            fake_gh):
    """The reconciler trap in both its shapes (kn-089de524): an event per tick buries the timeline"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)
    list(invariants.check_blocked_work_is_surfaced(store))
    ops.ack_attention(wo["id"])
    for _ in range(3):
        poll(fleet, store)
        list(invariants.check_blocked_work_is_surfaced(store))

    assert len(store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)) == 1
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_the_flag_goes_down_by_itself_when_the_branch_moves_again(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """Derived and not stored, which is what makes it BOTH self-clearing and re-askable: the blocker is..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    assert invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))

    artifact(fake_gh, head_oid=AGAIN)
    poll(fleet, store)

    assert len(store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)) == 2
    assert invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))

    # The person spends the round the machine left them
    ops.force_validation(wo["id"], reason="I read the merge commit myself")
    judge(fleet, store, Panel("passed"))
    poll(fleet, store)

    assert not invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))
    assert invariants.SHA_MOVED_BLOCKER not in invariants.true_blockers(
        store, store.get_work_order(wo["id"]))


# -- the policy, without a daemon


def test_the_hold_the_os_cannot_read_a_head_from_writes_nothing(fleet, project):
    """An empty head is `gh` failing to answer, not a commit"""
    store, wo = parked(project)
    decision = automerge._held(automerge.HELD_SHA_MOVED, "unknown",
                               judged_sha=JUDGED, head_sha="")

    out = ops.rejudge_moved_head(store, project, wo, project="proj_a",
                                 cfg=fleet.catalog.project("proj_a").validation,
                                 decision=decision)

    assert out is None
    assert rounds_of(store, wo) == [1]
    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []


def test_the_substituted_head_answers_about_the_merge_and_not_about_the_round():
    """`automerge.only_the_head_moved` is `decide` with the live head put in"""
    from jarvis.github import PullRequest

    def pull(**over):
        return PullRequest(**{"state": "OPEN", "mergeable": "MERGEABLE",
                              "merge_state": "CLEAN", "base_ref": "main",
                              "checks": tuple(GREEN), "head_oid": PUSHED, **over})

    pr = pull()
    wo = {"id": "wo-1", "status": "waiting_pr_merge", "pr_url": PR}
    cfg = type("C", (), {"enabled": True, "auto_merge": True, "max_rounds": 3})()
    row = {"id": 1, "round": 1, "outcome": "passed", "head_sha": JUDGED}

    assert automerge.only_the_head_moved(row, wo, pr, cfg)
    assert not automerge.only_the_head_moved(row, wo, pull(merge_state="DIRTY"), cfg)
    assert not automerge.only_the_head_moved(row, wo, pull(head_oid=""), cfg)


def test_raising_the_budget_is_all_the_user_has_to_do(tmp_path, project, jarvis_home,
                                                      fake_claude, fake_gh):
    """The decline suppresses the EVENT, never the remedy"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    assert rounds_of(store, wo) == [1, 2]

    poll(boot(tmp_path, project, max_rounds=5), store)

    assert rounds_of(store, wo) == [1, 2, 3]
    assert store.get_work_order(wo["id"])["status"] == "validating"


def test_a_round_the_os_opened_and_the_panel_rejected_goes_back_to_the_worker(
        fleet, project, fake_gh):
    """THE OUTCOME THIS CHANGE NEWLY AUTOMATES: the OS opened the round, and the panel refused it"""
    store, wo = parked(project)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)
    judge(fleet, store, Panel("rejected"))

    assert store.get_work_order(wo["id"])["status"] == "validating"
    spec = fleet.catalog.project("proj_a")
    fleet.deliver_envelopes(spec, store)
    queued = store.queued_messages(wo["id"])
    assert len(queued) == 1
    assert "Review feedback (round 2): rejected." in queued[0]["content"]

    fleet.deliver_messages(spec, store)
    row = store.get_work_order(wo["id"])
    assert row["status"] == "running"                    # the worker has it back
    assert not store.queued_messages(wo["id"])


def test_an_os_rejection_never_parks_the_order_silently(fleet, project, fake_gh):
    """The other half of the same worry: while the feedback is in flight the order must not read as a..."""
    store, wo = parked(project)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)
    judge(fleet, store, Panel("rejected"))
    fleet.deliver_envelopes(fleet.catalog.project("proj_a"), store)
    before = len(store.events_of_kind(wo["id"], "automerge_held"))
    poll(fleet, store)                                   # the parked-order poll, again

    row = store.get_work_order(wo["id"])
    assert row["status"] == "validating"
    assert len(store.events_of_kind(wo["id"], "automerge_held")) == before
    codes = [db.from_json(e["payload"], {}).get("code")
             for e in store.events_of_kind(wo["id"], "automerge_held")]
    assert automerge.HELD_NOT_PASSED not in codes
    envelope = store.envelopes(subject_wo_id=wo["id"])[-1]
    assert envelope["state"] == "delivered"
    assert envelope["delivered_wo_id"] == wo["id"]


# -- the pull request settles UNDER the re-judgement


def test_a_hand_merge_during_the_re_judgement_is_noticed_on_the_very_next_poll(
        fleet, project, fake_gh):
    """The gap re-judging opened"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)                      # the head moved: round 2 is open
    assert store.get_work_order(wo["id"])["status"] == "validating"
    # AND THE SEATS ARE ACTUALLY READING IT
    panel = HeldPanel("passed")
    hold(fleet, store, panel)

    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-22T10:00:00Z", head_oid=PUSHED)
    poll(fleet, store)

    assert store.get_work_order(wo["id"])["status"] == "completed"
    # VOID, not passed or rejected: nothing judged this, and nothing was left undecided
    round_2 = store.validation_rounds(wo_id=wo["id"])[-1]
    assert int(round_2["round"]) == 2 and round_2["outcome"] == "void"
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    assert store.events_of_kind(wo["id"], "validation_void")
    # The user merged it
    assert store.events_of_kind(wo["id"], "automerge_merged") == []

    # AND THE PANEL FINISHES ANYWAY
    release(fleet, panel)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "completed"
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "void"
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    assert store.events_of_kind(wo["id"], "validation_passed") == []


def test_a_hand_closure_during_the_re_judgement_reaches_the_user_the_same_way(
        fleet, project, fake_gh):
    """The other ending, under the same rule: somebody shut the pull request on purpose"""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    panel = HeldPanel("passed")
    hold(fleet, store, panel)

    fake_gh.set_pr(PR, "CLOSED", head_oid=PUSHED)
    poll(fleet, store)

    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "void"
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1

    # The mirror of the merge case: a panel settling afterwards must not pass the work order out of the...
    release(fleet, panel)

    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "void"
    assert store.events_of_kind(wo["id"], "validation_passed") == []


def test_the_widened_poll_nudges_nobody_while_a_round_is_open(fleet, project, fake_gh):
    """THE BOUND ON THE WIDENING"""
    store, wo = parked(project)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)                      # round 2 is open

    # ...and now the pull request conflicts
    fake_gh.set_pr(PR, "OPEN", checks=RED, merge_state="DIRTY", head_oid=PUSHED)
    poll(fleet, store)

    assert store.get_work_order(wo["id"])["status"] == "validating"
    assert store.pr_repair_attempts(wo["id"], ops.PR_CONFLICT) == 0
    assert store.pr_repair_attempts(wo["id"], ops.PR_CHECKS) == 0
    assert store.events_of_kind(wo["id"], "pr_conflict_nudged") == []
    assert store.events_of_kind(wo["id"], "pr_checks_nudged") == []
    # The round it is waiting on is untouched — nothing here voided it
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "pending"


def test_validating_is_the_only_status_polled_without_being_repairable():
    """The pairing `invariants.PR_REPAIR_STATUSES` documents"""
    from jarvis.daemon import PR_POLL_STATUSES

    assert set(PR_POLL_STATUSES) - set(invariants.PR_REPAIR_STATUSES) == {"validating"}
    assert not set(invariants.PR_REPAIR_STATUSES) - set(PR_POLL_STATUSES)


# -- the head that moved for no reason a seat needs to read

#: The head after `main` was merged in, and the base commit that went in with it
CAUGHT_UP = "dddd1111dddd2222dddd3333dddd4444dddd5555"
BASE = "eeee1111eeee2222eeee3333eeee4444eeee5555"


@pytest.fixture()
def local_proof(monkeypatch):
    """The local half of the carry's proof (§3.3), answered without a network"""
    from jarvis import branchproof

    state = {"id": "cafe12345678", "ids": {}}
    monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: True)
    monkeypatch.setattr(branchproof, "diff_fingerprint",
                        lambda repo, base_ref, sha: state["ids"].get(sha, state["id"]))
    monkeypatch.setattr(branchproof, "is_ancestor",
                        lambda repo, ancestor, descendant: True)
    return state


def test_a_catch_up_with_main_costs_no_round(fleet, project, fake_gh, local_proof):
    """§1"""
    store, wo = parked(project)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    artifact(fake_gh, head_oid=CAUGHT_UP)

    poll(fleet, store)

    assert store.events_of_kind(wo["id"], "validation_forced") == []
    assert rounds_of(store, wo) == [1]
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) == CAUGHT_UP


def test_the_carry_runs_with_no_rounds_left_and_a_decline_already_written(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof):
    """PART 3, §6 item 2, and the exact state of wo-00bd1096 and wo-8736a5c5: the budget is spent and this..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    artifact(fake_gh, head_oid=CAUGHT_UP)
    # The tick before this feature shipped: declined, on this very head
    store.add_event(wo["id"], invariants.REJUDGE_DECLINED_EVENT,
                    {"head_sha": CAUGHT_UP, "judged_sha": JUDGED, "round": 2,
                     "next_round": 3, "max_rounds": 3})

    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) == CAUGHT_UP
    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    assert CAUGHT_UP in approvals[0]["command"]


def test_the_stranded_blocker_falls_by_itself_on_the_tick_after_the_carry(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof,
        local_base):
    """§6 item 4"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=CAUGHT_UP)
    # Round 3 would be the last, so the old path declined and flagged the user
    poll(fleet, store)
    list(invariants.check_blocked_work_is_surfaced(store))
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        invariants.SHA_MOVED_BLOCKER

    # ...and now the proof can be made — the daemon reads the parents it could not before
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    poll(fleet, store)
    # INV-ATTENTION-REASON, the checker that takes a flag DOWN: the blocker stopped being derivable
    list(invariants.check_attention_reason_is_true(store))

    assert not invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))
    row = store.get_work_order(wo["id"])
    assert invariants.SHA_MOVED_BLOCKER not in invariants.true_blockers(store, row)
    assert rounds_of(store, wo) == [1, 2]

    # NOTHING WAS TYPED
    from jarvis import gates

    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    gates.apply_decision(store, approvals[0]["id"], "approved", "ok", "neo",
                         project="proj_a")
    poll(fleet, store)
    list(invariants.check_no_phantom_attention(store))

    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_an_evil_merge_still_costs_a_round_and_still_declines_at_the_last_one(
        fleet, project, fake_gh, local_proof):
    """§3.4: the fall-through is not a consolation prize"""
    store, wo = parked(project)
    local_proof["ids"][CAUGHT_UP] = "0ther00patch"
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    artifact(fake_gh, head_oid=CAUGHT_UP)

    poll(fleet, store)

    refused = store.events_of_kind(wo["id"], ops.CARRY_REFUSED_EVENT)
    assert [db.from_json(e["payload"], {})["proof"] for e in refused] == ["patch_id"]
    assert store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT) == []
    assert store.get_work_order(wo["id"])["status"] == "validating"
    assert rounds_of(store, wo) == [1, 2]
    assert db.from_json(store.events_of_kind(wo["id"], "validation_forced")[-1]
                        ["payload"], {})["by"] == ops.REJUDGE_BY_OS


def test_an_evil_merge_on_the_last_round_is_re_judged_as_a_rebind(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof):
    """THE DEFECT THIS TEST USED TO ASSERT, end to end and through the poll (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md)"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    local_proof["ids"][CAUGHT_UP] = "0ther00patch"
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    artifact(fake_gh, head_oid=CAUGHT_UP)

    poll(fleet, store)

    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []
    assert rounds_of(store, wo) == [1, 2, 3]
    row = store.latest_validation_round(wo_id=wo["id"])
    assert int(row["uncounted"]) == 1
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 2
    payload = db.from_json(
        store.events_of_kind(wo["id"], "validation_forced")[-1]["payload"], {})
    assert payload["by"] == ops.REJUDGE_BY_OS and payload["rebind"] is True
    assert not invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))


# -- the flag the carry itself puts down (spec §6 item 4)


def _stranded(tmp_path, project, fake_gh) -> tuple[object, object, dict]:
    """An order flagged `sha_moved` with its round budget spent — the state the carry inherits"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=CAUGHT_UP)
    poll(fleet, store)
    list(invariants.check_blocked_work_is_surfaced(store))
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        invariants.SHA_MOVED_BLOCKER
    return fleet, store, wo


def test_the_carry_puts_the_flag_down_itself(tmp_path, project, jarvis_home,
                                             fake_claude, fake_gh, local_proof):
    """§6 item 4"""
    fleet, store, wo = _stranded(tmp_path, project, fake_gh)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])

    poll(fleet, store)

    row = store.get_work_order(wo["id"])
    assert row["needs_attention"] == 0
    assert not row["attention_reason"]
    carried = db.from_json(store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)[-1]
                           ["payload"], {})
    assert carried["attention_cleared"] is True


def test_another_true_blocker_keeps_the_flag_up(fleet, project):
    """The carry clears the reason it made untrue, never somebody else's"""
    store, wo = parked(project)
    store.add_assumption(wo["id"], "used sqlite rather than postgres")
    store.flag_attention(wo["id"], invariants.SHA_MOVED_BLOCKER)

    carried = ops.carry_merge_chain(
        store, wo, judged=JUDGED, head=CAUGHT_UP,
        chain=((CAUGHT_UP, BASE, True),), base="main", base_sha=BASE,
        fingerprints=("cafe12345678", "cafe12345678"))

    assert carried is not None
    assert "attention_cleared" not in carried
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"] == 1
    assert row["attention_reason"] == invariants.SHA_MOVED_BLOCKER
    list(invariants.check_attention_reason_is_true(store))
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        "1 assumption pending your review"


def test_the_lowered_flag_is_not_re_raised_on_the_next_tick(tmp_path, project,
                                                            jarvis_home, fake_claude,
                                                            fake_gh, local_proof):
    """kn-089de524: attention is re-derived every tick"""
    fleet, store, wo = _stranded(tmp_path, project, fake_gh)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    poll(fleet, store)

    list(invariants.check_blocked_work_is_surfaced(store))
    list(invariants.check_attention_reason_is_true(store))
    poll(fleet, store)

    row = store.get_work_order(wo["id"])
    assert row["needs_attention"] == 0
    assert not row["attention_reason"]
    assert row["acknowledged_blockers"] is None
    assert len(store.events_of_kind(wo["id"], ops.HEAD_CARRIED_EVENT)) == 1


# -- a rebind: the round the OS's own merge costs nobody


def _cfg(fleet):
    return fleet.catalog.project("proj_a").validation


def _moved(head: str = PUSHED, *, round_n: int = 3):
    return automerge._held(automerge.HELD_SHA_MOVED, "the head moved",
                           judged_sha=JUDGED, head_sha=head, round_n=round_n)


def test_a_conflict_resolving_merge_past_the_cap_is_re_judged_without_spending_a_round(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """wo-8736a5c5 / PR #794"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)

    out = ops.rejudge_moved_head(store, project, wo, project="proj_a",
                                 cfg=_cfg(fleet), decision=_moved(), rebind=True)

    assert out is not None and not out["declined"]
    payload = db.from_json(
        store.events_of_kind(wo["id"], "validation_forced")[-1]["payload"], {})
    assert payload["by"] == ops.REJUDGE_BY_OS and payload["rebind"] is True
    row = store.latest_validation_round(wo_id=wo["id"])
    assert int(row["round"]) == 4 and int(row["uncounted"]) == 1
    assert "does not count against validation.max_rounds" in row["forced_reason"]

    store.set_validation_head(row["id"], PUSHED)
    store.close_validation_round(row["id"], "passed", "")

    assert store.counted_validation_rounds(wo_id=wo["id"]) == 3
    assert store.uncounted_validation_rounds(wo_id=wo["id"]) == 1
    assert store.numbered_validation_rounds(wo_id=wo["id"]) == 4


def test_the_rebind_arm_never_reads_the_round_budget(tmp_path, project, jarvis_home,
                                                     fake_claude, fake_gh):
    """§4.3: `cfg.max_rounds` is not consulted at all on the rebind arm"""
    fleet = boot(tmp_path, project, max_rounds=1)
    store, wo = parked(project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)

    out = ops.rejudge_moved_head(store, project, wo, project="proj_a",
                                 cfg=_cfg(fleet), decision=_moved(), rebind=True)

    assert out is not None and not out["declined"]
    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []


def test_rebind_max_bounds_the_self_repair(tmp_path, project, jarvis_home, fake_claude,
                                           fake_gh):
    """§4.3, the cap branch"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)
    for head in (PUSHED, AGAIN):
        artifact(fake_gh, head_oid=head)
        ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                               decision=_moved(head), rebind=True)
        row = store.latest_validation_round(wo_id=wo["id"])
        store.close_validation_round(row["id"], "rejected", "no")
        store.set_status(wo["id"], "waiting_pr_merge")
    assert store.uncounted_validation_rounds(wo_id=wo["id"]) == ops.REBIND_MAX

    third = "dddd1111dddd2222dddd3333dddd4444dddd5555"
    artifact(fake_gh, head_oid=third)
    out = ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                                 decision=_moved(third), rebind=True)

    assert out == {"wo_id": wo["id"], "declined": True,
                   "cause": ops.REBIND_EXHAUSTED, "head_sha": third,
                   "judged_sha": JUDGED, "rebinds": ops.REBIND_MAX,
                   "rebind_max": ops.REBIND_MAX}
    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert len(declined) == 1
    assert db.from_json(declined[0]["payload"], {})["cause"] == ops.REBIND_EXHAUSTED
    # said once per commit, not once per tick
    assert ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                                  decision=_moved(third), rebind=True) is None
    assert len(store.events_of_kind(
        wo["id"], invariants.REJUDGE_DECLINED_EVENT)) == 1


def test_a_non_merge_head_change_past_the_cap_still_goes_to_the_user(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """A worker PUSH past the cap is what `max_rounds` is FOR: no rebind, today's decline"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]
    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    payload = db.from_json(declined[-1]["payload"], {})
    assert payload["cause"] == ops.REJUDGE_BUDGET_SPENT
    assert payload["next_round"] == 3 and payload["max_rounds"] == 3
    assert invariants.true_blockers(
        store, store.get_work_order(wo["id"]))[0] == invariants.SHA_MOVED_BLOCKER


def test_a_passing_rebind_arms_the_merge(tmp_path, project, jarvis_home, fake_claude,
                                         fake_gh, local_base):
    """§5 test 2"""
    # local_base: spec 2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.2/§3.4
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=True)
    judge(fleet, store, Panel("passed"))

    row = store.latest_validation_round(wo_id=wo["id"])
    assert ProjectStore.validated_head(row) == PUSHED
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"

    poll(fleet, store)

    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    assert PUSHED in approvals[0]["command"]
    fresh = store.get_work_order(wo["id"])
    assert invariants.SHA_MOVED_BLOCKER not in invariants.true_blockers(store, fresh)
    assert not fresh["attention_reason"]


def test_a_rebind_says_so_on_the_round_listing(tmp_path, project, jarvis_home,
                                               fake_claude, fake_gh):
    """§4.3's last paragraph: round 4 under `max_rounds` 3 reads as the very bug this spec is about unless..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=True)

    rows = ops.validation_rounds(store, wo_id=wo["id"])
    assert rows[-1]["uncounted"] == 1
    assert "uncounted" in ops.round_line(rows[-1])
    assert "uncounted" not in ops.round_line(rows[0])


def test_a_rejected_rebind_goes_to_the_worker(tmp_path, project, jarvis_home,
                                              fake_claude, fake_gh):
    """§5 test 3, §4.4"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=True)
    assert int(store.latest_validation_round(wo_id=wo["id"])["round"]) == 4
    judge(fleet, store, Panel("rejected"))

    assert store.get_work_order(wo["id"])["status"] != "needs_review"
    payload = db.from_json(
        store.events_of_kind(wo["id"], "validation_rejected")[-1]["payload"], {})
    assert payload["uncounted"] is True and payload["of"] == ops.REBIND_MAX
    spec = fleet.catalog.project("proj_a")
    fleet.deliver_envelopes(spec, store)
    queued = store.queued_messages(wo["id"])
    assert len(queued) == 1
    text = queued[0]["content"]
    assert "no round spent" in text
    assert "round 4 of 3" not in text and "REVIEW FEEDBACK (round" not in text


def test_rebind_max_exhausted_goes_to_the_user(tmp_path, project, jarvis_home,
                                               fake_claude, fake_gh):
    """§5 test 4, §4.5"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    for head in (PUSHED, AGAIN):
        artifact(fake_gh, head_oid=head)
        ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                               decision=_moved(head), rebind=True)
        row = store.latest_validation_round(wo_id=wo["id"])
        store.set_validation_head(row["id"], head)
        store.close_validation_round(row["id"], "passed", "")
        store.set_status(wo["id"], "waiting_pr_merge")

    third = "dddd1111dddd2222dddd3333dddd4444dddd5555"
    artifact(fake_gh, head_oid=third)
    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(third), rebind=True)
    poll(fleet, store)          # writes the `sha_moved` hold the derivation reads

    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert db.from_json(declined[-1]["payload"], {})["cause"] == ops.REBIND_EXHAUSTED
    blockers = invariants.true_blockers(store, store.get_work_order(wo["id"]))
    assert invariants.REBIND_EXHAUSTED_BLOCKER in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers

    wider = boot(tmp_path, project, max_rounds=10)
    before = rounds_of(store, wo)
    assert ops.rejudge_moved_head(store, project, store.get_work_order(wo["id"]),
                                  project="proj_a", cfg=_cfg(wider),
                                  decision=_moved(third), rebind=True) is None
    assert rounds_of(store, wo) == before


def test_an_ordinary_round_after_a_rebind_is_budgeted_by_position(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """§4.4"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=1)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=True)
    judge(fleet, store, Panel("passed"))                 # the rebind, row 2

    assert ops.submit_for_validation(store, project, store.get_work_order(wo["id"]),
                                     declared="re-ran the suite after the merge",
                                     cfg=_cfg(fleet)) is not None
    assert int(store.latest_validation_round(wo_id=wo["id"])["round"]) == 3
    judge(fleet, store, Panel("rejected"))

    assert store.get_work_order(wo["id"])["status"] != "needs_review"
    payload = db.from_json(
        store.events_of_kind(wo["id"], "validation_rejected")[-1]["payload"], {})
    assert payload["round"] == 3 and payload["budget_round"] == 2
    fleet.deliver_envelopes(fleet.catalog.project("proj_a"), store)
    text = store.queued_messages(wo["id"])[-1]["content"]
    assert "round 2 of 3" in text and "round 3 of 3" not in text


def test_a_fix_after_a_rejected_rebind_is_itself_a_rebind(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """§4.4"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=1)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=True)
    judge(fleet, store, Panel("rejected"))               # rebind 1, row 2
    counted = store.counted_validation_rounds(wo_id=wo["id"])

    row = ops.submit_for_validation(store, project, store.get_work_order(wo["id"]),
                                    declared="fixed it", cfg=_cfg(fleet))
    assert row is not None and int(row["uncounted"]) == 1
    assert store.counted_validation_rounds(wo_id=wo["id"]) == counted

    judge(fleet, store, Panel("rejected"))               # rebind 2 — the last one
    assert store.uncounted_validation_rounds(wo_id=wo["id"]) == ops.REBIND_MAX
    assert store.get_work_order(wo["id"])["status"] == "needs_review"


# -- a rework the USER asked for: the second uncounted cause


def _user_rejected(store, wo, feedback: str = "not what I asked for") -> None:
    """What `ops.review_work_order(accept=False)` leaves on the record"""
    store.add_event(wo["id"], "reviewed",
                    {"accepted": False, "count": 1, "feedback": feedback})


def test_a_rework_the_user_asked_for_is_judged_past_the_cap(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """THE LIVE CASE, wo-299daf2e"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)

    out = ops.rejudge_moved_head(store, project, wo, project="proj_a",
                                 cfg=_cfg(fleet), decision=_moved(), rebind=False)

    assert out is not None and not out["declined"]
    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []
    row = store.latest_validation_round(wo_id=wo["id"])
    assert int(row["uncounted"]) == 1
    assert row["uncounted_cause"] == ops.USER_REWORK_CAUSE
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 3


def test_one_free_round_per_user_rejection_and_no_more(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """The predicate self-terminates: once the user-rework round has SETTLED it is the newest settled..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)
    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=False)
    row = store.latest_validation_round(wo_id=wo["id"])
    store.set_validation_head(row["id"], PUSHED)
    store.close_validation_round(row["id"], "passed", "")
    store.set_status(wo["id"], "waiting_pr_merge")

    artifact(fake_gh, head_oid=AGAIN)
    out = ops.rejudge_moved_head(store, project, store.get_work_order(wo["id"]),
                                 project="proj_a", cfg=_cfg(fleet),
                                 decision=_moved(AGAIN), rebind=False)

    assert out is not None and out["declined"]
    assert out["cause"] == ops.REJUDGE_BUDGET_SPENT
    assert store.uncounted_validation_rounds(
        wo_id=wo["id"], cause=ops.USER_REWORK_CAUSE) == 1


def test_the_two_uncounted_causes_never_spend_each_other(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """`REBIND_MAX` bounds the OS's own merge re-judgements and NOTHING else: a work order that has spent..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    for head in (PUSHED, AGAIN):
        artifact(fake_gh, head_oid=head)
        ops.rejudge_moved_head(store, project, store.get_work_order(wo["id"]),
                               project="proj_a", cfg=_cfg(fleet),
                               decision=_moved(head), rebind=True)
        row = store.latest_validation_round(wo_id=wo["id"])
        store.set_validation_head(row["id"], head)
        store.close_validation_round(row["id"], "passed", "")
        store.set_status(wo["id"], "waiting_pr_merge")
    assert store.uncounted_validation_rounds(
        wo_id=wo["id"], cause=ops.REBIND_CAUSE) == ops.REBIND_MAX

    _user_rejected(store, wo)
    third = "dddd1111dddd2222dddd3333dddd4444dddd5555"
    artifact(fake_gh, head_oid=third)
    out = ops.rejudge_moved_head(store, project, store.get_work_order(wo["id"]),
                                 project="proj_a", cfg=_cfg(fleet),
                                 decision=_moved(third), rebind=True)

    assert out is not None and not out["declined"]
    row = store.latest_validation_round(wo_id=wo["id"])
    assert row["uncounted_cause"] == ops.USER_REWORK_CAUSE
    assert store.uncounted_validation_rounds(
        wo_id=wo["id"], cause=ops.REBIND_CAUSE) == ops.REBIND_MAX
    line = ops.round_line(ops.validation_rounds(store, wo_id=wo["id"])[-1])
    assert "uncounted" in line and ops.USER_REWORK_CAUSE in line


def test_a_worker_re_delivery_of_a_user_rework_costs_no_round(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """The worker coming back through `jarvis wo finish` gets the same free round: the rework is the..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)

    row = ops.submit_for_validation(store, project, store.get_work_order(wo["id"]),
                                    declared="did the rework", cfg=_cfg(fleet))

    assert row is not None and int(row["uncounted"]) == 1
    assert row["uncounted_cause"] == ops.USER_REWORK_CAUSE
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 3


def test_a_fix_after_a_rejected_user_rework_round_is_an_ordinary_round(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """UNLIKE a rebind, whose fix is itself a rebind: the bound here is ONE round per user rejection"""
    fleet = boot(tmp_path, project, max_rounds=5)
    store, wo = parked(project, rounds=1)
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)
    row = ops.submit_for_validation(store, project, store.get_work_order(wo["id"]),
                                    declared="did the rework", cfg=_cfg(fleet))
    assert row["uncounted_cause"] == ops.USER_REWORK_CAUSE
    store.close_validation_round(row["id"], "rejected", "still no")
    counted = store.counted_validation_rounds(wo_id=wo["id"])

    again = ops.submit_for_validation(store, project, store.get_work_order(wo["id"]),
                                      declared="fixed what the panel said",
                                      cfg=_cfg(fleet))

    assert again is not None and int(again["uncounted"]) == 0
    assert again["uncounted_cause"] == ""
    assert int(again["round"]) == int(row["round"]) + 1
    store.close_validation_round(again["id"], "passed", "")
    assert store.counted_validation_rounds(wo_id=wo["id"]) == counted + 1


def test_a_passing_user_rework_round_arms_the_merge(tmp_path, project, jarvis_home,
                                                    fake_claude, fake_gh, local_base):
    """The point of the exemption is a pull request that MERGES: the rework the user asked for is judged..."""
    # local_base: spec 2026-09-28-a-merge-checks-the-base-it-lands-on.md §3.2/§3.4
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    _user_rejected(store, wo)
    # The worker answered it: `ops.refusal_answered` is the other half of the user's gate
    store.add_event(wo["id"], "finished", {"summary": "did the rework"})
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=False)
    judge(fleet, store, Panel("passed"))

    row = store.latest_validation_round(wo_id=wo["id"])
    assert ProjectStore.validated_head(row) == PUSHED
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"

    poll(fleet, store)

    approvals = store.list_approvals(wo["id"])
    assert [a["kind"] for a in approvals] == ["auto_merge"]
    assert PUSHED in approvals[0]["command"]
    assert not store.get_work_order(wo["id"])["attention_reason"]


def test_a_rejected_user_rework_round_goes_back_to_the_worker(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """The counted budget still has room, so the worker gets the feedback"""
    fleet = boot(tmp_path, project, max_rounds=5)
    store, wo = parked(project, rounds=2)
    store.update_work_order(wo["id"], session_id="sess-1")
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=False)
    judge(fleet, store, Panel("rejected"))

    assert store.get_work_order(wo["id"])["status"] != "needs_review"
    payload = db.from_json(
        store.events_of_kind(wo["id"], "validation_rejected")[-1]["payload"], {})
    assert payload["uncounted"] is True
    assert payload["uncounted_cause"] == ops.USER_REWORK_CAUSE
    fleet.deliver_envelopes(fleet.catalog.project("proj_a"), store)
    text = store.queued_messages(wo["id"])[0]["content"]
    assert "no round spent" in text
    assert "re-judgement" not in text and "merge the base branch" not in text
    assert "REVIEW FEEDBACK (round" not in text


def test_a_rejected_user_rework_round_with_no_budget_left_goes_to_the_user(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """One free round per user rejection and no more: the counted budget is spent"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)

    ops.rejudge_moved_head(store, project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(), rebind=False)
    judge(fleet, store, Panel("rejected"))

    row = store.get_work_order(wo["id"])
    assert row["status"] == "needs_review"
    blockers = invariants.true_blockers(store, row)
    assert invariants.USER_REWORK_REFUSED_BLOCKER in blockers
    assert invariants.VALIDATION_STUCK_BLOCKER not in blockers
    assert invariants.REBIND_EXHAUSTED_BLOCKER not in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers


# -- a user-rework round NOBODY judged, GitHub issue 1106


def _escalated_user_rework(fleet, store, wo, project, *, cause: str) -> dict:
    """An uncounted user-rework round escalated through the real op with `cause`"""
    _user_rejected(store, wo)
    row = ops.submit_for_validation(store, project, store.get_work_order(wo["id"]),
                                    declared="did the rework", cfg=_cfg(fleet))
    assert row is not None and row["uncounted_cause"] == ops.USER_REWORK_CAUSE
    ops.escalate_validation_round(store, store.get_work_order(wo["id"]), int(row["id"]),
                                  int(row["round"]), "nothing new to judge", cause=cause)
    return store.get_work_order(wo["id"])


def test_an_unreviewed_user_rework_round_does_not_claim_the_panel_refused_it(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """Issue 1106"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    row = _escalated_user_rework(fleet, store, wo, project,
                                 cause=ops.ESCALATION_REPEAT_SUBMISSION)

    blockers = invariants.true_blockers(store, row)

    assert invariants.USER_REWORK_UNREVIEWED_BLOCKER in blockers
    assert invariants.USER_REWORK_REFUSED_BLOCKER not in blockers
    assert invariants.VALIDATION_STUCK_BLOCKER not in blockers


def test_a_user_rework_round_with_no_recorded_cause_keeps_the_refused_sentence(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """Issue 1106 prescribes ONE split and `not_reviewed` is it: a legacy event with no cause keeps the..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    row = _escalated_user_rework(fleet, store, wo, project,
                                 cause=ops.ESCALATION_CAUSE_UNRECORDED)

    blockers = invariants.true_blockers(store, row)

    assert invariants.USER_REWORK_REFUSED_BLOCKER in blockers
    assert invariants.USER_REWORK_UNREVIEWED_BLOCKER not in blockers
    assert invariants.VALIDATION_STUCK_BLOCKER not in blockers


def test_an_unjudged_user_rework_round_keeps_the_refused_sentence(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """Same for family `unjudged` (`no_verdict`): only `not_reviewed` moves"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    assert ops.ESCALATION_FAMILY[ops.ESCALATION_NO_VERDICT] == "unjudged"
    row = _escalated_user_rework(fleet, store, wo, project,
                                 cause=ops.ESCALATION_NO_VERDICT)

    blockers = invariants.true_blockers(store, row)

    assert invariants.USER_REWORK_REFUSED_BLOCKER in blockers
    assert invariants.USER_REWORK_UNREVIEWED_BLOCKER not in blockers
    assert invariants.VALIDATION_STUCK_BLOCKER not in blockers


def test_the_unreviewed_sentence_names_the_two_routes_and_nothing_that_moves():
    """`ack_attention` stores it verbatim and INV-ATTENTION-REASON compares it, so no clock and no sha"""
    text = invariants.USER_REWORK_UNREVIEWED_BLOCKER

    assert not any(ch.isdigit() for ch in text)
    assert "max_rounds" not in text
    assert 'jarvis wo review --reject --feedback "<what to change>"' in text
    assert 'jarvis wo review --feedback "<why>"' in text
    assert "refused" not in text and "judged and" not in text


def test_the_unreviewed_blocker_can_be_acknowledged(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """The round trip the wording rule exists for: what `true_blockers` derives is what `ack_attention`..."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=3)
    row = _escalated_user_rework(fleet, store, wo, project,
                                 cause=ops.ESCALATION_NOTHING_TO_JUDGE)
    store.ack_attention(row["id"], invariants.true_blockers(store, row))

    list(invariants.check_attention_reason_is_true(store))
    list(invariants.check_blocked_work_is_surfaced(store))

    assert not store.get_work_order(wo["id"])["needs_attention"]


# -- a verdict bound to NO commit, spec 2026-10-16


def unrecorded(project, *, rounds: int = 1, outcome: str = "passed"):
    """Parked with its latest round `passed` and `head_sha=''` — what a worktree packet leaves behind"""
    return parked(project, judged="", rounds=rounds, outcome=outcome)


def forced_payloads(store, wo) -> list[dict]:
    return [db.from_json(e["payload"], {})
            for e in store.events_of_kind(wo["id"], "validation_forced")]


def test_a_round_that_recorded_no_commit_re_judges_itself(fleet, project, fake_gh):
    """THE HEAL"""
    store, wo = unrecorded(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert store.get_work_order(wo["id"])["status"] == "validating"
    payloads = forced_payloads(store, wo)
    assert len(payloads) == 1
    assert payloads[0]["by"] == ops.REJUDGE_BY_OS
    assert payloads[0]["head_sha"] == PUSHED and payloads[0]["judged_sha"] == ""
    # COUNTED: nothing was judged to rebind and the user asked for nothing
    row = store.latest_validation_round(wo_id=wo["id"])
    assert not row["uncounted_cause"] and not row["uncounted"]


def test_the_unrecorded_head_costs_one_round_per_commit(fleet, project, fake_gh):
    """`ops.rejudged_heads`' dedupe, on this code too"""
    store, wo = unrecorded(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    judge(fleet, store, Panel("passed"))
    # The pull request was unreadable again: round 2 bound no commit either
    store.set_validation_head(store.latest_validation_round(wo_id=wo["id"])["id"], "")

    poll(fleet, store)
    poll(fleet, store)

    assert len(forced_payloads(store, wo)) == 1
    assert not store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert rounds_of(store, wo) == [1, 2]
    row = store.get_work_order(wo["id"])
    assert invariants.sha_unrecorded_rejudge_spent(store, row)
    blockers = invariants.true_blockers(store, row)
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER in blockers
    # Not the budget sentence: nothing was declined and the budget is untouched
    assert invariants.SHA_UNRECORDED_BLOCKER not in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers
    assert invariants.REBIND_EXHAUSTED_BLOCKER not in blockers


def test_the_os_round_in_flight_on_an_unrecorded_head_blocks_nobody(
        fleet, project, fake_gh):
    """The round the OS just opened may yet bind the commit"""
    store, wo = unrecorded(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "validating"
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER not in invariants.true_blockers(
        store, row)


def test_a_round_that_binds_the_unrecorded_head_clears_the_blocker(
        fleet, project, fake_gh):
    """The tail clause: the OS's own round read the pull request after all and recorded the commit"""
    store, wo = unrecorded(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    judge(fleet, store, Panel("passed"))

    row = store.get_work_order(wo["id"])
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) == PUSHED
    assert not invariants.sha_unrecorded_rejudge_spent(store, row)
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER not in invariants.true_blockers(
        store, row)


def test_a_head_that_moved_past_the_spent_one_is_re_judged_not_flagged(
        fleet, project, fake_gh):
    """The branch moved to a commit the OS has not judged"""
    store, wo = unrecorded(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    judge(fleet, store, Panel("passed"))
    store.set_validation_head(store.latest_validation_round(wo_id=wo["id"])["id"], "")
    artifact(fake_gh, head_oid=AGAIN)

    poll(fleet, store)

    payloads = forced_payloads(store, wo)
    assert [p["head_sha"] for p in payloads] == [PUSHED, AGAIN]
    row = store.get_work_order(wo["id"])
    assert row["status"] == "validating"
    assert invariants.SHA_UNRECORDED_SPENT_BLOCKER not in invariants.true_blockers(
        store, row)


@pytest.mark.parametrize("checks,assumption", [(RED, False), (GREEN, True)])
def test_an_unrecorded_head_on_a_pull_request_that_is_not_ready_is_left_alone(
        fleet, project, fake_gh, checks, assumption):
    """`automerge.only_the_head_moved` is reused unchanged"""
    store, wo = unrecorded(project)
    if assumption:
        store.add_assumption(wo["id"], "the exporter writes CSV")
    artifact(fake_gh, head_oid=PUSHED, checks=checks)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    assert not store.events_of_kind(wo["id"], "validation_forced")
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_the_unrecorded_decline_reaches_the_user_with_its_own_sentence(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """The second defect in the chain"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = unrecorded(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert len(declined) == 1
    assert db.from_json(declined[0]["payload"], {})["cause"] == ops.REJUDGE_BUDGET_SPENT
    row = store.get_work_order(wo["id"])
    assert invariants.sha_unrecorded_exhausted(store, row)
    blockers = invariants.true_blockers(store, row)
    assert invariants.SHA_UNRECORDED_BLOCKER in blockers
    assert invariants.SHA_MOVED_BLOCKER not in blockers
    assert invariants.REBIND_EXHAUSTED_BLOCKER not in blockers

    # ...and it self-clears: the user spends the round the machine left them
    ops.force_validation(wo["id"], reason="I read the pull request myself")
    judge(fleet, store, Panel("passed"))
    poll(fleet, store)

    row = store.get_work_order(wo["id"])
    assert not invariants.sha_unrecorded_exhausted(store, row)
    assert invariants.SHA_UNRECORDED_BLOCKER not in invariants.true_blockers(store, row)


def test_the_unrecorded_reason_never_claims_the_head_moved(fleet, project, fake_gh):
    """`REJUDGE_FORCED_REASON` renders "round 1 passed on"""
    store, wo = unrecorded(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    stored = store.latest_validation_round(wo_id=wo["id"])["forced_reason"]
    payload = forced_payloads(store, wo)[0]
    for text in (stored, payload["reason"]):
        assert "moved" not in text and "older" not in text
        assert "passed on" not in text and "judged  " not in text
        assert PUSHED[:10] in text


@pytest.mark.parametrize("outcome", ["rejected", "failed", "pending"])
def test_a_round_that_never_passed_is_still_never_re_judged(fleet, project, fake_gh,
                                                            outcome):
    """REGRESSION LOCK on the exclusion"""
    store, wo = unrecorded(project, outcome=outcome)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    assert not store.events_of_kind(wo["id"], "validation_forced")
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_NOT_PASSED


# -- #1141: a head moved by merges alone gets the rebind with the counted rounds spent


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD").strip()


def _double_merge(project: Path) -> tuple[str, str]:
    """wo-87c1f30b's shape: main merged into a stale local branch, then the judged catch-up merged on top"""
    _git(project, "checkout", "-qb", "feature")
    worked = _commit(project, "app.py", "feature\n")
    _git(project, "checkout", "-q", "main")
    _commit(project, "base1.py", "one\n")
    _git(project, "checkout", "-q", "feature")
    _git(project, "merge", "-q", "--no-edit", "main")
    judged = _git(project, "rev-parse", "HEAD").strip()
    _git(project, "checkout", "-q", "main")
    _commit(project, "base2.py", "two\n")
    _git(project, "update-ref", "refs/remotes/origin/main", "main")
    _git(project, "checkout", "-qb", "local", worked)
    _git(project, "merge", "-q", "--no-edit", "main")
    _git(project, "merge", "-q", "--no-edit", judged)
    return judged, _git(project, "rev-parse", "HEAD").strip()


def test_merges_only_follows_every_parent_not_just_the_first(project):
    from jarvis import branchproof

    judged, head = _double_merge(project)

    assert branchproof.merges_only(project, judged, head, "main")


def test_a_commit_the_worker_authored_is_not_merges_only(project):
    from jarvis import branchproof

    judged, _head = _double_merge(project)
    edited = _commit(project, "app.py", "feature, changed\n")

    assert not branchproof.merges_only(project, judged, edited, "main")


@pytest.fixture()
def merges(monkeypatch):
    """`branchproof.merges_only`'s answer, set by the test"""
    from jarvis import branchproof

    state = {"only": True}
    monkeypatch.setattr(branchproof, "merges_only",
                        lambda repo, judged, head, base_ref: state["only"])
    return state


@pytest.mark.parametrize("patch", ["0ther00patch", None])
def test_rounds_spent_and_merges_only_rebinds_without_asking(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof, merges,
        patch):
    """Content resolved or not: a merge-only head the first-parent walk cannot read is a rebind"""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    if patch:
        local_proof["ids"][CAUGHT_UP] = patch
    artifact(fake_gh, head_oid=CAUGHT_UP)          # no parents: the walk fails

    poll(fleet, store)

    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []
    assert rounds_of(store, wo) == [1, 2, 3]
    assert int(store.latest_validation_round(wo_id=wo["id"])["uncounted"]) == 1
    payload = db.from_json(
        store.events_of_kind(wo["id"], "validation_forced")[-1]["payload"], {})
    assert payload["rebind"] is True


def test_rounds_spent_and_a_worker_commit_still_asks_the_user(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof, merges):
    merges["only"] = False
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    local_proof["ids"][PUSHED] = "0ther00patch"
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]
    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert db.from_json(declined[-1]["payload"], {})["cause"] == ops.REJUDGE_BUDGET_SPENT
    assert invariants.true_blockers(
        store, store.get_work_order(wo["id"]))[0] == invariants.SHA_MOVED_BLOCKER


def test_rounds_spent_and_rebinds_spent_asks_the_user(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof, merges):
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    for n in range(3, 3 + ops.REBIND_MAX):
        row = store.open_validation_round(wo_id=wo["id"], fingerprint=f"fp{n}",
                                          round=n, uncounted=True,
                                          uncounted_cause=ops.REBIND_CAUSE)
        store.set_validation_head(row["id"], JUDGED)
        store.close_validation_round(row["id"], "passed", "")
    local_proof["ids"][CAUGHT_UP] = "0ther00patch"
    artifact(fake_gh, head_oid=CAUGHT_UP)

    poll(fleet, store)

    assert rounds_of(store, wo) == list(range(1, 3 + ops.REBIND_MAX))
    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert invariants.true_blockers(
        store, store.get_work_order(wo["id"]))[0] == invariants.SHA_MOVED_BLOCKER


def test_a_stale_behind_reading_does_not_skip_the_carry(
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof, monkeypatch):
    """wo-3511ecc4: the poll read the base as unreached, the fetch said otherwise, and the carry never ran"""
    from jarvis import branchproof

    fetched = {"yet": False}

    def fetch(repo, *refs):
        fetched["yet"] = True
        return True

    monkeypatch.setattr(branchproof, "fetch", fetch)
    monkeypatch.setattr(branchproof, "is_ancestor",
                        lambda repo, ancestor, descendant: fetched["yet"])
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    artifact(fake_gh, head_oid=CAUGHT_UP)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=CAUGHT_UP,
                   base_oid=BASE)

    poll(fleet, store)

    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []
    assert rounds_of(store, wo) == [1, 2]
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) == CAUGHT_UP
