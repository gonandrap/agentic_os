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


class HeldPanel(Panel):
    """A panel stopped MID-ROUND, so the pull request can settle underneath it.

    The five seats take minutes on a real round; every test above settles one in a
    microsecond, which is the one window the tests below are about."""

    def __init__(self, outcome: str = "passed"):
        super().__init__(outcome)
        self.entered = threading.Event()
        self.released = threading.Event()

    def __call__(self, store, round_row, packet):
        self.entered.set()
        assert self.released.wait(15), "the held panel was never released"
        return super().__call__(store, round_row, packet)


def hold(fleet, store, panel: HeldPanel) -> None:
    """Start the open round and leave it inside the validator."""
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


def test_a_round_the_os_opened_and_the_panel_rejected_goes_back_to_the_worker(
        fleet, project, fake_gh):
    """THE OUTCOME THIS CHANGE NEWLY AUTOMATES: the OS opened the round, and the panel
    refused it. A rejection below `max_rounds` must land where every other rejection
    lands — feedback in the worker's session — and never in the silent park of
    wo-7e08ac40, which is `waiting_pr_merge` over a `not_passed` hold with nobody left
    to act and no flag.

    The session stamp is what `parked` leaves out and every real parked order has:
    `waiting_pr_merge` is reached through `jarvis wo finish`, which only a dispatched
    worker can call.
    """
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
    """The other half of the same worry: while the feedback is in flight the order must
    not read as a settled park. It stays `validating` — a state the round machine owns
    and `invariants.MESSAGE_STUCK_STATUSES` deliberately leaves to it — the poll writes
    no hold at all because a parked order is the only thing it looks at, and the
    envelope names the work order itself as the implementor, so the feedback reached
    somebody rather than nobody."""
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


# -- the pull request settles UNDER the re-judgement ------------------------------------


def test_a_hand_merge_during_the_re_judgement_is_noticed_on_the_very_next_poll(
        fleet, project, fake_gh):
    """The gap re-judging opened. It moves the order into `validating`, which used to be
    unpolled — so the user merging the pull request themselves, while five seats read a
    diff that had just landed, went unnoticed until the round settled and parked the order
    back in `waiting_pr_merge`. Noticing that merge is what the poll is for, so
    `validating` is polled and the round is closed `void` first.
    """
    store, wo = parked(project)
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)                      # the head moved: round 2 is open
    assert store.get_work_order(wo["id"])["status"] == "validating"
    # AND THE SEATS ARE ACTUALLY READING IT. The merge lands mid-round, which is the
    # only shape of this that can happen: five seats take minutes.
    panel = HeldPanel("passed")
    hold(fleet, store, panel)

    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-22T10:00:00Z", head_oid=PUSHED)
    poll(fleet, store)

    assert store.get_work_order(wo["id"])["status"] == "completed"
    # VOID, not passed or rejected: nothing judged this, and nothing was left undecided.
    # It costs the submitter no round, so a later re-delivery is not short of budget.
    round_2 = store.validation_rounds(wo_id=wo["id"])[-1]
    assert int(round_2["round"]) == 2 and round_2["outcome"] == "void"
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    assert store.events_of_kind(wo["id"], "validation_void")
    # The user merged it. The record must not claim the OS did.
    assert store.events_of_kind(wo["id"], "automerge_merged") == []

    # AND THE PANEL FINISHES ANYWAY. Its verdict is about a question that stopped
    # mattering, so it must not overwrite the `void` — nor re-land the work order it
    # read before the merge, which `ops.land_when_cleared` would park back in
    # `waiting_pr_merge` on a pass.
    release(fleet, panel)

    row = store.get_work_order(wo["id"])
    assert row["status"] == "completed"
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "void"
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    assert store.events_of_kind(wo["id"], "validation_passed") == []


def test_a_hand_closure_during_the_re_judgement_reaches_the_user_the_same_way(
        fleet, project, fake_gh):
    """The other ending, under the same rule: somebody shut the pull request on purpose,
    so the panel's verdict decides nothing and the work order is the user's."""
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

    # The mirror of the merge case: a panel settling afterwards must not pass the work
    # order out of the user's hands and back behind a pull request nobody will merge.
    release(fleet, panel)

    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "void"
    assert store.events_of_kind(wo["id"], "validation_passed") == []


def test_the_widened_poll_nudges_nobody_while_a_round_is_open(fleet, project, fake_gh):
    """THE BOUND ON THE WIDENING. `validating` is polled but is NOT in
    `PR_REPAIR_STATUSES`, and it must not be: `invariants.true_blockers` derives the two
    repair give-ups only there, so a nudge sent here would raise a flag nothing can
    re-derive and INV-ATTENTION-REASON would relabel it on the next tick.
    """
    store, wo = parked(project)
    store.update_work_order(wo["id"], session_id="sess-1")
    artifact(fake_gh, head_oid=PUSHED)
    poll(fleet, store)                      # round 2 is open

    # ...and now the pull request conflicts, which in any repair status sends the worker
    # a nudge and opens a repair episode.
    fake_gh.set_pr(PR, "OPEN", checks=RED, merge_state="DIRTY", head_oid=PUSHED)
    poll(fleet, store)

    assert store.get_work_order(wo["id"])["status"] == "validating"
    assert store.pr_repair_attempts(wo["id"], ops.PR_CONFLICT) == 0
    assert store.pr_repair_attempts(wo["id"], ops.PR_CHECKS) == 0
    assert store.events_of_kind(wo["id"], "pr_conflict_nudged") == []
    assert store.events_of_kind(wo["id"], "pr_checks_nudged") == []
    # The round it is waiting on is untouched — nothing here voided it.
    assert store.validation_rounds(wo_id=wo["id"])[-1]["outcome"] == "pending"


def test_validating_is_the_only_status_polled_without_being_repairable():
    """The pairing `invariants.PR_REPAIR_STATUSES` documents, asserted rather than
    described: every other polled status may derive a repair blocker."""
    from jarvis.daemon import PR_POLL_STATUSES

    assert set(PR_POLL_STATUSES) - set(invariants.PR_REPAIR_STATUSES) == {"validating"}
    assert not set(invariants.PR_REPAIR_STATUSES) - set(PR_POLL_STATUSES)


# -- the head that moved for no reason a seat needs to read -----------------------------
# docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md: a catch-up
# with `main` is proved, not re-judged, and the round machine below must not see it.

#: The head after `main` was merged in, and the base commit that went in with it.
CAUGHT_UP = "dddd1111dddd2222dddd3333dddd4444dddd5555"
BASE = "eeee1111eeee2222eeee3333eeee4444eeee5555"


@pytest.fixture()
def local_proof(monkeypatch):
    """The local half of the carry's proof (§3.3), answered without a network.

    This project's `origin` is `github.com/acme/proj`, which no fetch can reach; the git
    is proved against a real clone in `tests/test_base_heal.py`. `ids` makes one commit's
    patch-id differ, which is what an evil merge looks like here.
    """
    from jarvis import branchproof

    state = {"id": "cafe12345678", "ids": {}}
    monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: True)
    monkeypatch.setattr(branchproof, "diff_fingerprint",
                        lambda repo, base_ref, sha: state["ids"].get(sha, state["id"]))
    monkeypatch.setattr(branchproof, "is_ancestor",
                        lambda repo, ancestor, descendant: True)
    return state


def test_a_catch_up_with_main_costs_no_round(fleet, project, fake_gh, local_proof):
    """§1. The head moved because `main` was merged in — which the OS's own gate reviewer
    demands — and the round machine must never see it. No `validation_forced`, no round 2,
    and the verdict now covers the commit that is there."""
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
    """PART 3, §6 item 2, and the exact state of wo-00bd1096 and wo-8736a5c5: the budget
    is spent and this head was already declined. The carry consults neither
    `max_rounds` nor the declined-heads dedupe — structurally, because
    `ops.carry_merge_chain` never reads `cfg` — so a stranded order still recovers."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    artifact(fake_gh, head_oid=CAUGHT_UP)
    # The tick before this feature shipped: declined, on this very head.
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
        tmp_path, project, jarvis_home, fake_claude, fake_gh, local_proof):
    """§6 item 4. Attention is re-derived every tick, so nothing has to be acked and
    nothing has to be forced: the carry binds a verdict to the head, `rejudge_exhausted`
    stops being true at its last clause, and the flag goes down on its own."""
    fleet = boot(tmp_path, project, max_rounds=3)
    store, wo = parked(project, rounds=2)
    artifact(fake_gh, head_oid=CAUGHT_UP)
    # Round 3 would be the last, so the old path declined and flagged the user.
    poll(fleet, store)
    list(invariants.check_blocked_work_is_surfaced(store))
    assert store.get_work_order(wo["id"])["attention_reason"] == \
        invariants.SHA_MOVED_BLOCKER

    # ...and now the proof can be made — the daemon reads the parents it could not before.
    fake_gh.set_parents(CAUGHT_UP, [JUDGED, BASE])
    poll(fleet, store)
    # INV-ATTENTION-REASON, the checker that takes a flag DOWN: the blocker stopped being
    # derivable, so nothing had to be acked (kn-089de524).
    list(invariants.check_attention_reason_is_true(store))

    assert not invariants.rejudge_exhausted(store, store.get_work_order(wo["id"]))
    row = store.get_work_order(wo["id"])
    assert invariants.SHA_MOVED_BLOCKER not in invariants.true_blockers(store, row)
    assert rounds_of(store, wo) == [1, 2]

    # NOTHING WAS TYPED. The blocker stopped deriving, so the flag is no longer re-raised
    # and the order goes on down the merge path — the gate is Neo's to answer, and the flag
    # itself comes down with the work order (INV-ATTENTION-PHANTOM).
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
    """§3.4: the fall-through is not a consolation prize. A merge whose resolution edited
    the branch's own files differs in patch-id, and that resolution is authored content no
    seat has read — precisely what a round exists to judge. So the refusal is recorded,
    naming the proof, and the round machine takes over unchanged."""
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
    """THE DEFECT THIS TEST USED TO ASSERT, end to end and through the poll. At
    `max_rounds` the decline stranded wo-8736a5c5 on the user for a merge the OS itself
    demanded; proof (a) holds and proof (b) does not, so the round that reads the
    resolution is a rebind and no budget is spent (spec
    docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md)."""
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


# -- the flag the carry itself puts down (spec §6 item 4) ------------------------------


def _stranded(tmp_path, project, fake_gh) -> tuple[object, object, dict]:
    """An order flagged `sha_moved` with its round budget spent — the state the carry
    inherits. Parents unreadable on the first poll, so the carry refuses and the round
    machine declines and flags."""
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
    """§6 item 4. `sha_moved` was the only blocker, so the carry that made it untrue takes
    the flag down there and then — the user does not keep an attention item until the
    pull request merges."""
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
    """The carry clears the reason it made untrue, never somebody else's. A pending
    assumption is still the user's to decide, so the flag stays up and says so.

    Asked of `ops.carry_merge_chain` directly: with an assumption pending, `decide` holds
    on `assumptions` rather than `sha_moved`, so the daemon's carry is never reached and
    the rule has to be proved where it lives."""
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
    """kn-089de524: attention is re-derived every tick, so a flag lowered on this path
    must stay down without an ack and the carry must not write a second one."""
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


# -- a rebind: the round the OS's own merge costs nobody -------------------------------
# spec docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md


def _cfg(fleet):
    return fleet.catalog.project("proj_a").validation


def _moved(head: str = PUSHED, *, round_n: int = 3):
    return automerge._held(automerge.HELD_SHA_MOVED, "the head moved",
                           judged_sha=JUDGED, head_sha=head, round_n=round_n)


def test_a_conflict_resolving_merge_past_the_cap_is_re_judged_without_spending_a_round(
        tmp_path, project, jarvis_home, fake_claude, fake_gh):
    """wo-8736a5c5 / PR #794. The OS's own conflict poll made the worker merge `main`;
    the resolution is content no seat read, so the carry correctly refuses — and the
    re-judge that follows must not be charged to the worker's rework budget."""
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
    """§4.3: `cfg.max_rounds` is not consulted at all on the rebind arm — the live
    order's recovery must not depend on any round accounting reaching this path."""
    fleet = boot(tmp_path, project, max_rounds=1)
    store, wo = parked(project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)

    out = ops.rejudge_moved_head(store, project, wo, project="proj_a",
                                 cfg=_cfg(fleet), decision=_moved(), rebind=True)

    assert out is not None and not out["declined"]
    assert store.events_of_kind(wo["id"], invariants.REJUDGE_DECLINED_EVENT) == []


def test_rebind_max_bounds_the_self_repair(tmp_path, project, jarvis_home, fake_claude,
                                           fake_gh):
    """§4.3, the cap branch. Past `REBIND_MAX` the decline names its own cause, so a
    reader can tell it from a spent round budget — and it is said once per commit."""
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
    """A worker PUSH past the cap is what `max_rounds` is FOR: no rebind, today's
    decline — now naming its cause — and today's blocker, byte for byte."""
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
                                         fake_gh):
    """§5 test 2. The point of the exemption is a pull request that MERGES: the rebind
    binds a verdict to the head the OS's own merge produced, so `decide` arms on it and
    nobody was asked for anything."""
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
    """§4.3's last paragraph: round 4 under `max_rounds` 3 reads as the very bug this
    spec is about unless the line says the round was not charged to anyone."""
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
    """§5 test 3, §4.4. The row is numbered past `max_rounds`, and routing on that
    would send it to the user — the rebind budget is what decides, and the feedback may
    not tell the worker it has spent a round it has not."""
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
    """§5 test 4, §4.5. `SHA_MOVED_BLOCKER` would offer `validation.max_rounds` as the
    remedy, and the rebind arm never reads it — so the sentence has to be the other
    one, and raising the round budget must not restart anything."""
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
