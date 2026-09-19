"""A pull request whose head moved past its verdict re-judges itself.

docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md, issue #493.

THE BUG: the OS nudges its own worker to resolve a merge conflict, the worker pushes,
and the head is now a commit no round has read. `automerge.decide` holds on `sha_moved`
for ever, `waiting_pr_merge` carries no attention flag, and the order sits green and
mergeable until a person runs `jarvis validation force`. Measured on wo-2005a89b.

So every test here starts from a round that PASSED and recorded a commit, and then
moves the head — a fixture whose round recorded nothing is the OTHER bug
(`tests/test_forced_validation.py`), and a fix for it passes while this one stalls.
"""

from __future__ import annotations

import json
import os
import subprocess
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
#: What the conflict resolution pushed: the merge commit no seat has read.
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
    """A catalog ON DISK: `ops.force_validation_refusal` — which this feature shares with
    the person's command — resolves its config through `ops.validation_config`, which
    re-reads the file rather than the daemon's object."""
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
    """A real repository whose `origin` matches `PR` — the evidence collector reads
    both, and the branch is named explicitly for kn-4b6f18f5's reason."""
    proj = make_git_project(tmp_path, "proj_a")
    _git(proj, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    _git(proj, "remote", "add", "origin", "https://github.com/acme/proj.git")
    claude_json(proj)
    return proj


def boot(tmp_path, project, **validation) -> Daemon:
    """A started OS whose project has the panel and the automatic merge on."""
    ops.start_os(str(write_catalog(tmp_path, project, **validation)), foreground=True)
    return Daemon(load_catalog(tmp_path / "catalog.json"))


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, project):
    """ROOM IN THE ROUND BUDGET, deliberately: the shipped `max_rounds` is 3 and the OS
    never spends the last one, so a fixture at the default could only ever prove the
    refusal. The tests about the boundary boot their own fleet at 3."""
    return boot(tmp_path, project, max_rounds=5)


class Panel:
    """The injected validator seam. Records the round numbers it was handed."""

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
    """An order parked behind its pull request with `rounds` settled rounds, the last of
    them on `judged`. The real `ops.finish` puts it there, so the `finished` event these
    tests count is genuine."""
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
    """The pull request BOTH readers see — the evidence collector and the poll."""
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
    """Run whatever round is open to a verdict, off the daemon's tick thread."""
    fleet.validator = panel
    fleet.validation_tick(fleet.catalog.project("proj_a"), store)
    deadline = time.monotonic() + timeout
    while fleet.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.validating, "a validation round never finished"


def rounds_of(store, wo) -> list[int]:
    return [int(r["round"]) for r in store.validation_rounds(wo_id=wo["id"])]


# -- the bug, end to end ---------------------------------------------------------------


def test_the_head_the_os_moved_is_re_judged_and_the_pull_request_then_merges(
        fleet, project, fake_gh):
    """THE WHOLE BUG. The conflict resolution moved the head past the verdict; nothing
    used to re-open a round, so the order sat green and unmerged until a person noticed.

    Stopping at "a round was opened" would prove too little — wo-2005a89b's pull request
    was green and mergeable the whole time. The acceptance is that it MERGES: the OS asks
    for the gate, on the commit that is actually there.
    """
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
    """The two halves of the record. `jarvis validation force` had to exist because the
    only other route wrote a `finished` event nobody earned; a round the OS opens must
    not write one either, and must not read afterwards as one a person asked for."""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    before = len(store.events_of_kind(wo["id"], "finished"))

    poll(fleet, store)

    assert len(store.events_of_kind(wo["id"], "finished")) == before
    forced = store.events_of_kind(wo["id"], "validation_forced")
    payload = db.from_json(forced[-1]["payload"], {})
    assert payload["by"] == ops.REJUDGE_BY_OS
    assert payload["head_sha"] == PUSHED and payload["judged_sha"] == JUDGED
    # Both commits in the sentence stored on the round itself, where
    # `jarvis validation show` renders it beside the verdict it caused.
    reason = store.latest_validation_round(wo_id=wo["id"])["forced_reason"]
    assert JUDGED[:10] in reason and PUSHED[:10] in reason

    from jarvis import timeline
    label, _ = timeline._describe("validation_forced", payload)
    assert "by the OS" in label and "by hand" not in label


# -- the guards ------------------------------------------------------------------------


def test_one_round_per_commit_and_not_one_per_tick(fleet, project, fake_gh):
    """A parked pull request is polled every couple of minutes. A trigger that fired on
    the hold rather than on the commit would spend the whole round budget in ten
    minutes."""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)
    judge(fleet, store, Panel("passed"))
    poll(fleet, store)
    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]


def test_a_head_that_moves_again_is_judged_again(fleet, project, fake_gh):
    """The other side of that key: the dedupe is per COMMIT, so a branch that genuinely
    moves a second time is not silently left on the verdict for the first."""
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
    """GUARD 2. The re-judge is worth a round number only when the stale verdict is the
    ONLY thing left holding the merge. Judging a commit whose build is still running —
    or one the worker is about to push over, because CI went red or the branch stopped
    merging — spends a round on a diff that is about to be replaced."""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED, checks=checks, merge_state=merge_state)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    # ...and the hold is still recorded, so the surfaces still say what is going on.
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_SHA_MOVED


def test_a_branch_somebody_is_still_typing_into_is_not_judged(fleet, project, fake_gh):
    """GUARD 4. A turn in flight is a push that has not happened yet — and an open round
    owns the worker's session (kn-01a4ab27), which is how the panel's feedback ends up
    in a conversation mid-task."""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    store.create_turn(wo["id"], "message", "resolve the conflict")

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]


def test_a_message_already_on_its_way_holds_it_too(fleet, project, fake_gh):
    """GUARD 4, the other half: a queued message is a turn about to start."""
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    store.queue_message(wo["id"], "one more thing")

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]


def test_a_rejected_round_is_never_reopened_by_the_machine(fleet, project, fake_gh):
    """OUT OF SCOPE AND IT STAYS THERE (spec §6). An order parked over a REJECTED round
    holds on `not_passed`, and that verdict is one the panel meant to stand: reopening it
    would burn round numbers arguing with the reviewer. It is a different bug with a
    different cause, tracked separately."""
    store, wo = parked(project, outcome="rejected")
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    held = store.events_of_kind(wo["id"], "automerge_held")
    assert db.from_json(held[-1]["payload"], {})["code"] == automerge.HELD_NOT_PASSED


def test_a_project_that_never_opted_in_is_not_re_judged_either(tmp_path, project,
                                                               jarvis_home, fake_claude,
                                                               fake_gh):
    """The panel's own switch, shared with the person's command through
    `ops.force_validation_refusal`: a project with the panel off has nothing to judge
    with, and one with `auto_merge` off is never polled by this mechanism at all."""
    fleet = boot(tmp_path, project, auto_merge=False)
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1]
    assert store.events_of_kind(wo["id"], "automerge_held") == []


# -- the last round is the user's ------------------------------------------------------


def test_the_machine_never_spends_the_last_round(tmp_path, project, jarvis_home,
                                                 fake_claude, fake_gh):
    """GUARD 6. `max_rounds` is where a rejection stops going back to a worker and starts
    going to the user — and on a parked order there is no worker left. A machine that
    spent the final round would hand the user an escalation instead of a decision they
    could still have acted on."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    assert rounds_of(store, wo) == [1, 2]
    declined = store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)
    assert len(declined) == 1
    payload = db.from_json(declined[0]["payload"], {})
    assert payload["head_sha"] == PUSHED and payload["next_round"] == 3
    # ...and the person still can: the round it left is theirs to spend.
    ops.force_validation(wo["id"], reason="I read the merge commit myself")
    assert rounds_of(store, wo) == [1, 2, 3]


def test_the_one_stall_it_cannot_heal_is_the_one_it_reports(tmp_path, project,
                                                            jarvis_home, fake_claude,
                                                            fake_gh):
    """§4. A held auto-merge is deliberately not an attention item — but that rule was
    written when every `sha_moved` hold was ordinary. The one the OS cannot clear by
    itself is not ordinary, and silence there is the 30 minutes wo-2005a89b sat green."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)

    poll(fleet, store)

    row = store.get_work_order(wo["id"])
    assert invariants.true_blockers(store, row)[0] == invariants.SHA_MOVED_BLOCKER
    # The flag itself comes from INV-ATTENTION-MISSING, which is the path that honours
    # an ack — never from the write site (kn-089de524).
    assert not row["needs_attention"]
    list(invariants.check_blocked_work_is_surfaced(store))
    row = store.get_work_order(wo["id"])
    assert row["needs_attention"] and row["attention_reason"] == \
        invariants.SHA_MOVED_BLOCKER


def test_the_decline_is_recorded_once_and_an_ack_stays_down(tmp_path, project,
                                                            jarvis_home, fake_claude,
                                                            fake_gh):
    """The reconciler trap in both its shapes (kn-089de524): an event per tick buries the
    timeline, and a flag re-raised per tick overwrites the user's `jarvis wo ack`."""
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
    """Derived and not stored, which is what makes it BOTH self-clearing and re-askable:
    the blocker is about one commit. A head that moves again is asked afresh — one
    decline per commit, the flag still up — and it goes down by itself the moment a round
    binds a verdict to the head that is there."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    assert invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))

    artifact(fake_gh, head_oid=AGAIN)
    poll(fleet, store)

    assert len(store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT)) == 2
    assert invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))

    # The person spends the round the machine left them, and nothing has to be cleared
    # by hand: the newest hold stops being `sha_moved`, so the blocker stops deriving.
    ops.force_validation(wo["id"], reason="I read the merge commit myself")
    judge(fleet, store, Panel("passed"))
    poll(fleet, store)

    assert not invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))
    assert invariants.SHA_MOVED_BLOCKER not in invariants.true_blockers(
        store, store.get_work_order(wo["id"]))


# -- the policy, without a daemon ------------------------------------------------------


def test_the_hold_the_os_cannot_read_a_head_from_writes_nothing(fleet, project):
    """An empty head is `gh` failing to answer, not a commit. Recording a decline for it
    would poison the dedupe — the key would be `""` and would then swallow the real head
    when it arrives."""
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
    """`automerge.only_the_head_moved` is `decide` with the live head put in — so the six
    conditions stay in one table. It is meaningful ONLY after a `sha_moved` hold, which
    is why its caller checks that first: substituting the head satisfies conditions 4
    and 5 by construction, and on a rejected round that would read as a near miss."""
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
    """The decline suppresses the EVENT, never the remedy. A commit the OS judged is
    judged for ever, but one it declined was declined against a round budget — so the
    tick after the user raises that budget, the OS heals the stall it reported instead
    of waiting to be told again."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)
    assert rounds_of(store, wo) == [1, 2]

    poll(boot(tmp_path, project, max_rounds=5), store)

    assert rounds_of(store, wo) == [1, 2, 3]
    assert store.get_work_order(wo["id"])["status"] == "validating"
