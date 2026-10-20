"""The work-order round machine: `finish` opens a round"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jarvis import claude_cli, ops, validation
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import VALIDATION_REASON_CHARS, VALIDATION_REASON_CUT, Daemon
from jarvis.invariants import VALIDATION_STUCK_BLOCKER, true_blockers
from jarvis.project_store import VALIDATOR_SEATS, ProjectStore
from jarvis.testing import make_git_project


# -- the fixture: a real repository, a real worktree, a fake panel


def _git(cwd: Path, *args: str, env_extra: dict[str, str] | None = None) -> str:
    """git with a pinned identity and no user or system config in sight"""
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           **(env_extra or {})}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


class Validator:
    """The injected seam, driven by the test"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [passed()]
        self.calls: list[dict] = []
        self.stores: list[object] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()  # not blocking unless a test says so

    def __call__(self, store, round_row, packet):
        self.calls.append({"round": round_row["round"], "packet": packet,
                           "fingerprint": round_row["fingerprint"]})
        self.stores.append(store)
        self.entered.set()
        # A round is up to five headless calls
        assert self.release.wait(timeout=15), "a blocked validator was never released"
        outcome = (self.outcomes.pop(0) if len(self.outcomes) > 1
                   else self.outcomes[0])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def block(self) -> None:
        self.release.clear()
        self.entered.clear()


def passed(reason: str = "") -> dict:
    return {"outcome": "passed", "reason": reason, "seats": _seats("pass")}


def rejected(reason: str = "the tests do not touch the change") -> dict:
    return {"outcome": "rejected", "reason": reason, "seats": _seats("reject")}


def _seats(verdict: str) -> list[dict]:
    """What a panel hands back per seat — named, so the opinions are real rows"""
    return [{"seat": seat, "status": "ok", "verdict": verdict, "model": "sonnet",
             "latency_ms": 12,
             "reply": f"the {seat} seat says: {verdict} — no test covers the change"}
            for seat in ("tester", "security")]


class Fleet:
    """One booted OS, plus the git plumbing these tests need"""

    def __init__(self, tmp_path, project, catalog_path):
        self.tmp_path = tmp_path
        self.project = project
        self.catalog_path = catalog_path
        self.daemon = Daemon(load_catalog(catalog_path))

    # -- configuration
    def reconfigure(self, project_validation: dict | None = None, **validation) -> None:
        """Rewrite the catalog on disk AND reload the daemon's copy"""
        write_catalog(self.tmp_path, self.project,
                      project_validation=project_validation, **validation)
        self.daemon.catalog = load_catalog(self.catalog_path)

    @property
    def spec(self):
        return self.daemon.catalog.projects[0]

    def store(self) -> ProjectStore:
        return ProjectStore(self.project)

    # -- work orders
    def dispatch(self, title: str = "task", **kw) -> dict:
        """Create a work order and give it a session, the way a real one gets one"""
        wo = ops.create_work_order("proj_a", title, **kw)
        self.daemon.tick()
        store = self.store()
        try:
            assert _settle(store, wo["id"])
        finally:
            store.close()
        return wo

    def worktree(self, wo_id: str) -> Path:
        """The worktree the real `claude --worktree` would have cut"""
        path = self.project / ".claude" / "worktrees" / wo_id
        if not path.is_dir():
            _git(self.project, "worktree", "add", "-q", "-b", f"b-{wo_id}",
                 str(path), "main")
        return path

    def change(self, wo_id: str, text: str, name: str = "app.py") -> Path:
        """A committed change in the work order's worktree — evidence to judge"""
        tree = self.worktree(wo_id)
        (tree / name).write_text(text)
        _git(tree, "add", "-A")
        _git(tree, "commit", "-qm", f"work {len(text)}")
        return tree

    # -- the daemon
    def tick(self) -> None:
        self.daemon.tick_count = 0
        self.daemon.tick()

    def drain(self, timeout: float = 15.0) -> None:
        """One tick, then wait for whatever it started off-thread to finish"""
        self.tick()
        deadline = time.monotonic() + timeout
        while self.daemon.validating and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not self.daemon.validating, "a validation round never finished"


def _settle(store: ProjectStore, wo_id: str, timeout: float = 15.0) -> bool:
    """Wait for this work order's turn to end — a turn is a detached process"""
    from jarvis import worker_session

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        list(worker_session.poll(store))
        turn = store.latest_turn(wo_id)
        if turn is not None and turn["state"] != "running":
            return True
        time.sleep(0.02)
    return False


def write_catalog(tmp_path: Path, project: Path,
                  project_validation: dict | None = None, **validation) -> Path:
    proj: dict = {"name": "proj_a", "path": str(project), "description": "test project"}
    if project_validation is not None:
        proj["validation"] = project_validation
    data = {
        "os": {
            "defaults": {"model": "sonnet"},
            "notifications": {"sinks": ["log"]},
            "validation": {"enabled": True, **validation},
        },
        "projects": [proj],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, claude_json):
    """A booted OS over a REAL git repository, with validation enabled"""
    project = make_git_project(tmp_path, "proj_a")
    _git(project, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    claude_json(project)
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    return Fleet(tmp_path, project, catalog_path)


#: "give this work order a pull request of its own"
AUTO_PR = "<auto>"


def finish(fleet: Fleet, wo_id: str, summary: str = "done", pr: str | None = AUTO_PR,
           evidence: str = "ran `pytest -q`: 412 passed") -> dict:
    """Finish the way a worker that produced code now has to: behind a pull request (#232)"""
    if pr == AUTO_PR:
        pr = f"https://github.com/x/y/pull/{int(wo_id[-4:], 16) % 900 + 100}"
    return ops.finish(wo_id, summary, pr_url=pr, evidence=evidence)


# -- submission


def test_finish_opens_round_one_and_parks_the_work_order_in_validating(fleet):
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")

    result = finish(fleet, wo["id"], pr="https://github.com/x/y/pull/1")
    assert result["status"] == "validating"

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert len(rounds) == 1
        assert rounds[0]["round"] == 1
        assert rounds[0]["outcome"] == "pending"
        assert rounds[0]["fingerprint"], "a round with no fingerprint proves nothing"
        assert rounds[0]["evidence"] == "ran `pytest -q`: 412 passed"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "validating"
        # `validating` is the system working, not a decision anyone owes
        assert fresh["needs_attention"] == 0
        assert [e["kind"] for e in store.list_events(wo["id"])].count(
            "validation_submitted") == 1
    finally:
        store.close()


def test_a_worker_that_omits_evidence_still_finishes(fleet):
    """`--evidence` is optional on purpose: every worker in flight across the release that adds the flag..."""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")

    assert ops.finish(wo["id"], "done",
                      pr_url="https://github.com/x/y/pull/8")["status"] == "validating"

    store = fleet.store()
    try:
        round_row = store.latest_validation_round(wo_id=wo["id"])
        assert round_row["evidence"] == ""
        assert round_row["fingerprint"]
        fleet.daemon.validator = Validator(passed())
        fleet.drain()
        assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    finally:
        store.close()


# -- the reconciler stands back


def test_the_reconciler_does_not_settle_a_validating_work_order(fleet):
    """THE SHARPEST TRAP IN THE FEATURE, and it needs the pairing"""
    held = Validator(passed())
    held.block()  # the round stays open across both ticks
    fleet.daemon.validator = held

    guarded = fleet.dispatch("guarded")
    fleet.change(guarded["id"], "print('guarded')\n")
    finish(fleet, guarded["id"], pr="https://github.com/x/y/pull/1")

    loose = fleet.dispatch("loose")
    store = fleet.store()
    try:
        # The same row, minus the round: finished, with a pull request, still `running`
        store.update_work_order(loose["id"], result_summary="done",
                                pr_url="https://github.com/x/y/pull/2")

        fleet.tick()
        assert held.entered.wait(timeout=15), "the validator never started"
        fleet.tick()

        assert store.get_work_order(guarded["id"])["status"] == "validating"
        assert store.get_work_order(loose["id"])["status"] == "waiting_pr_merge"

        # And handed in directly, because the status filter in `settle_turns` is only the FIRST guard
        fleet.daemon.settle_work_order(fleet.spec, store,
                                       store.get_work_order(guarded["id"]))
        assert store.get_work_order(guarded["id"])["status"] == "validating"
    finally:
        held.release.set()
        store.close()


def test_a_validating_work_order_is_polled_and_keeps_its_pull_request(
        fleet, fake_gh):
    """A validating work order IS polled"""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    pr = "https://github.com/x/y/pull/9"
    finish(fleet, wo["id"], pr=pr)
    fake_gh.set_pr(pr, "OPEN", merge_state="CLEAN")

    store = fleet.store()
    try:
        # Measured as "a NEW gh call", not "any gh call at all": the evidence collector reads the pull...
        before = len(fake_gh.calls)
        fleet.daemon.poll_pull_requests(fleet.spec, store)
        assert fake_gh.calls[before:], "a validating work order went unpolled"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "validating", (
            "the poll moved an order the round machine still owns")
        assert fresh["pr_url"] == pr, "the round machine dropped the pull request"
    finally:
        held.release.set()
        store.close()


def test_wo_done_closes_a_validating_work_order(fleet):
    """The user's escape hatch has to work from inside a review"""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    result = ops.mark_done(wo["id"])
    assert result["was"] == "validating"
    assert result["status"] == "completed"

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "completed"
    finally:
        held.release.set()
        store.close()


# -- off the tick thread


def test_the_validator_runs_off_the_tick_thread_with_its_own_store(fleet):
    """Five seats at a 300-second timeout would freeze every project in the catalog if they ran inline"""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    started = time.monotonic()
    fleet.tick()                      # returns while the validator is still blocked
    elapsed = time.monotonic() - started
    assert held.entered.wait(timeout=15), "the validator never started"
    assert elapsed < 10, "the tick waited for the round"

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "pending"
        held.release.set()
        deadline = time.monotonic() + 15
        while fleet.daemon.validating and time.monotonic() < deadline:
            time.sleep(0.01)
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
        assert held.stores[0] is not fleet.daemon.stores["proj_a"], (
            "the round used the tick thread's store")
    finally:
        store.close()


def test_a_second_tick_does_not_start_a_second_validation(fleet):
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.tick()
    assert held.entered.wait(timeout=15)
    fleet.tick()
    fleet.tick()

    assert len(held.calls) == 1, "the round was re-entered while it was in flight"
    held.release.set()


def test_the_round_row_exists_before_the_validator_starts(fleet):
    """Opened on the submitting thread"""
    seen: dict = {}
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")

    def validator(store, round_row, packet):
        seen["rows"] = store.validation_rounds(wo_id=wo["id"])
        return passed()

    fleet.daemon.validator = validator
    finish(fleet, wo["id"])
    fleet.drain()

    assert [r["round"] for r in seen["rows"]] == [1]
    assert seen["rows"][0]["outcome"] == "pending", "the round was open while judged"


# -- a pass


def test_a_pass_settles_exactly_where_finish_would(fleet):
    """`waiting_pr_merge` with a pull request, `completed` without one (#232)"""
    fleet.daemon.validator = Validator(passed())

    with_pr = fleet.dispatch("with pr")
    fleet.change(with_pr["id"], "print('pr')\n")
    finish(fleet, with_pr["id"], pr="https://github.com/x/y/pull/3")

    without = fleet.dispatch("without pr")
    fleet.change(without["id"], "print('no pr')\n")
    ops.finish(without["id"], "done", evidence="ran the tests",
               abandon="spiked; the approach does not work")

    fleet.drain()
    fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(with_pr["id"])["status"] == "waiting_pr_merge"
        assert store.get_work_order(without["id"])["status"] == "completed"
        for wo_id in (with_pr["id"], without["id"]):
            latest = store.latest_validation_round(wo_id=wo_id)
            assert latest["outcome"] == "passed"
            assert [e["kind"] for e in store.list_events(wo_id)].count(
                "validation_passed") == 1
    finally:
        store.close()


def test_a_backlog_backed_work_order_closes_its_item_once_validation_passes(fleet):
    """The trap `ops.finish` carries: it closes the backlog item only when the resulting status is..."""
    central = CentralStore()
    try:
        item = central.add_backlog("proj_a", "the thing", "details")
    finally:
        central.close()
    wo = ops.create_work_order("proj_a", "the thing", backlog_id=item["id"])
    fleet.daemon.tick()
    store = fleet.store()
    try:
        assert _settle(store, wo["id"])
    finally:
        store.close()
    fleet.change(wo["id"], "print('one')\n")
    fleet.daemon.validator = Validator(passed())

    # Abandoned rather than behind a pull request (#232)
    ops.finish(wo["id"], "done", evidence="ran the tests",
               abandon="spiked; the approach does not work")
    central = CentralStore()
    try:
        assert central.get_backlog(item["id"])["status"] == "open", (
            "closed before anything judged it")
        fleet.drain()
        assert central.get_backlog(item["id"])["status"] == "done"
    finally:
        central.close()


# -- a rejection


def _reject_one(fleet, title: str = "task", **kw) -> tuple[dict, Validator]:
    validator = Validator(rejected(), passed())
    fleet.daemon.validator = validator
    wo = fleet.dispatch(title)
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"], **kw)
    fleet.drain()
    return wo, validator


def test_a_rejection_reaches_the_worker_as_ordinary_review_feedback(fleet):
    """AND ONLY VIA THE BUS, AND SAYING NOTHING ABOUT WHO JUDGED IT"""
    wo, _ = _reject_one(fleet)
    store = fleet.store()
    try:
        envelopes = store.envelopes(subject_wo_id=wo["id"])
        assert len(envelopes) == 1
        assert envelopes[0]["to_role"] == "implementor"
        assert envelopes[0]["from_role"] == "reviewer"
        assert envelopes[0]["kind"] == "review_feedback"

        fleet.tick()  # the router delivers it; the worker's next turn goes out
        # `list_messages` carries both directions; the worker's own final output is the other one
        texts = [m["content"] for m in store.list_messages(wo["id"])
                 if m["direction"] == "user_to_agent"]
        assert len(texts) == 1
        text = texts[0]
        assert "REVIEW FEEDBACK (round 1 of 3)" in text
        assert "the tests do not touch the change" in text
        assert f"jarvis wo finish {wo['id']}" in text
        for forbidden in (*VALIDATOR_SEATS, "panel", "seat", "validator"):
            assert forbidden.lower() not in text.lower(), (
                f"the worker was told about {forbidden}")

        # …and here is where the seats DO live, for the same round
        round_row = store.latest_validation_round(wo_id=wo["id"])
        opinions = store.validation_opinions(round_row["id"])
        assert {o["seat"] for o in opinions} == {"tester", "security"}
        assert all(o["reply"] for o in opinions)
        assert {o["verdict"] for o in opinions} == {"reject"}

        assert round_row["outcome"] == "rejected"
        assert store.get_work_order(wo["id"])["status"] == "running", (
            "the work order did not go back to its worker")
    finally:
        store.close()


def test_the_round_machine_never_queues_a_message_itself(fleet, monkeypatch):
    """What pins the decoupling: with `queue_message` booby-trapped"""
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    def boom(*a, **kw):
        raise AssertionError("the round machine queued a message directly")

    # Armed only around the round itself: reaping a worker turn records the worker's own final reply...
    monkeypatch.setattr(ProjectStore, "queue_message", boom)
    fleet.drain()

    store = fleet.store()
    try:
        envelopes = store.envelopes(subject_wo_id=wo["id"])
        assert len(envelopes) == 1 and envelopes[0]["kind"] == "review_feedback"
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "rejected"
    finally:
        store.close()


def test_a_resubmission_opens_round_two(fleet):
    wo, validator = _reject_one(fleet)
    fleet.tick()  # deliver the feedback, so the work order is running again

    fleet.change(wo["id"], "print('one')\nprint('and a test')\n")
    finish(fleet, wo["id"], summary="fixed")
    fleet.drain()

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [r["round"] for r in rounds] == [1, 2]
        assert [r["outcome"] for r in rounds] == ["rejected", "passed"]
        assert rounds[0]["fingerprint"] != rounds[1]["fingerprint"]
        assert [c["round"] for c in validator.calls] == [1, 2]
    finally:
        store.close()


def test_round_two_reads_round_one_and_round_one_reads_nothing(fleet):
    """THE OLD REPLY SHAPE, which is most of production: every opinion stored before this feature is..."""
    wo, validator = _reject_one(fleet)
    fleet.tick()  # deliver the feedback, so the work order is running again

    fleet.change(wo["id"], "print('one')\nprint('and a test')\n")
    finish(fleet, wo["id"], summary="fixed")
    fleet.drain()

    store = fleet.store()
    try:
        # FIRST, so that a failure here names its cause: an identical resubmission is escalated on its...
        assert [r["round"] for r in store.validation_rounds(wo_id=wo["id"])] == [1, 2]
    finally:
        store.close()
    assert [c["round"] for c in validator.calls] == [1, 2]

    first = validation.build_packet_prompt(validator.calls[0]["packet"])
    second = validation.build_packet_prompt(validator.calls[1]["packet"])

    assert validation.HISTORY_HEADING not in first
    assert "the tests do not touch the change" not in first
    assert "Round 1" not in first

    assert validation.HISTORY_HEADING in second
    assert "Round 1 — rejected" in second
    assert "the tests do not touch the change" in second, (
        "round 1's reason, verbatim — the fallback when no opinion parses to a finding")
    assert "so you do not raise them again" in second
    assert "What that round said had to change" not in second, (
        "a prose opinion classifies nothing, and a blocker invented for it would be this "
        "renderer putting words in a seat's mouth")


#: What the fake `claude` answers to each token, so an assertion can name the string it is about
BLOCKER_DETAIL = "what the tester seat would stop this over"
FOLLOW_UP_TITLE = "the architect follow-up"
FOLLOW_UP_DETAIL = "what the architect seat would file rather than argue"


def _panel_round(fleet, wo_id: str, code: str, summary: str) -> None:
    """One round judged by the REAL panel over the fake `claude`"""
    fleet.change(wo_id, code)
    finish(fleet, wo_id, summary=summary)
    fleet.drain()


def test_a_prior_blocker_reaches_round_two_and_a_prior_follow_up_reaches_nobody(
        fleet, fake_claude):
    """THE PIN §5.2.1 EXISTS FOR"""
    wo = fleet.dispatch()
    # TWO SEATS RATHER THAN ONE raising both
    _panel_round(fleet, wo["id"], "print('one')\n",
                 "FORCE_BLOCK_TESTER FORCE_FOLLOWUP_ARCHITECT")
    fleet.tick()  # deliver the rejection
    judged_round_one = len(fake_claude.calls)

    _panel_round(fleet, wo["id"], "print('one')\nprint('and a test')\n", "fixed")

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [r["round"] for r in rounds] == [1, 2]
        assert rounds[0]["outcome"] == "rejected"
        # Vacuous unless round 1 really raised one of each: the two assertions below are about strings
        replies = "".join(str(o["reply"])
                          for o in store.validation_opinions(int(rounds[0]["id"])))
        assert BLOCKER_DETAIL in replies and FOLLOW_UP_DETAIL in replies
    finally:
        store.close()

    seat_calls = [c for c in fake_claude.calls[judged_round_one:]
                  if "# Jarvis validation seat: " in c["argv"][c["argv"].index("-p") + 1]]
    chair = [c for c in seat_calls
             if "# Jarvis validation seat: chair" in c["argv"][c["argv"].index("-p") + 1]]
    assert len(chair) == 1, "no chair call, so the assertions below are about nothing"

    for call in seat_calls:
        prefix = call["system_prompt_seen"]
        assert BLOCKER_DETAIL in prefix
        assert FOLLOW_UP_TITLE not in prefix
        assert FOLLOW_UP_DETAIL not in prefix
    chair_prompt = chair[0]["argv"][chair[0]["argv"].index("-p") + 1]
    assert FOLLOW_UP_TITLE not in chair_prompt
    assert FOLLOW_UP_DETAIL not in chair_prompt


def test_history_lists_only_the_rounds_the_budget_counted(tmp_path):
    """A `failed` round is an outage or a panel that was never wired in"""
    store = ProjectStore(tmp_path / "proj")
    try:
        wo_id = store.create_work_order("task")["id"]
        judged = store.open_validation_round(wo_id=wo_id, fingerprint="f1", round=1)
        store.record_validation_opinion(
            int(judged["id"]), "tester", status="ok", verdict="reject",
            reply=json.dumps({"verdict": "reject", "blocking": True, "reason": "r",
                              "asks": [], "findings": [
                                  {"severity": "blocker", "title": "MUST-CHANGE",
                                   "detail": "d"},
                                  {"severity": "follow_up", "title": "NICE-TO-HAVE",
                                   "detail": "d"}]}))
        store.close_validation_round(int(judged["id"]), "rejected", "go again")
        outage = store.open_validation_round(wo_id=wo_id, fingerprint="f2", round=2)
        store.close_validation_round(int(outage["id"]), "failed",
                                     "the reviewer could not be reached")
        store.open_validation_round(wo_id=wo_id, fingerprint="f3", round=3)

        history = ops.prior_round_history(store, wo_id=wo_id, before=4)

        assert [e["round"] for e in history] == [1]
        assert history[0]["reason"] == "go again"
        assert [b["title"] for b in history[0]["blockers"]] == ["MUST-CHANGE"], (
            "a follow-up in here is a follow-up in front of the chair")
        # ...and the round being judged is never its own history
        assert ops.prior_round_history(store, wo_id=wo_id, before=1) == []
    finally:
        store.close()


# -- round accounting


def test_opening_a_round_twice_yields_one_row_but_a_real_resubmission_advances(fleet):
    """Idempotent per (work order, round)"""
    held = Validator(rejected(), passed())
    held.block()
    fleet.daemon.validator = held
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")

    finish(fleet, wo["id"])
    finish(fleet, wo["id"], summary="said it twice")  # retried, round still open

    store = fleet.store()
    try:
        assert len(store.validation_rounds(wo_id=wo["id"])) == 1
        held.release.set()
        fleet.drain()
        fleet.tick()  # deliver the rejection

        fleet.change(wo["id"], "print('one')\nprint('two')\n")
        finish(fleet, wo["id"], summary="fixed")
        assert [r["round"] for r in store.validation_rounds(wo_id=wo["id"])] == [1, 2]
    finally:
        store.close()


def test_a_transport_outage_consumes_no_round_but_a_rejection_does(fleet):
    """A `ClaudeCliError` is the panel being unreachable, not a verdict on the work"""
    validator = Validator(claude_cli.ClaudeCliError("connection reset"), rejected())
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()
    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [r["outcome"] for r in rounds] == ["failed"]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
        assert store.get_work_order(wo["id"])["status"] == "validating", (
            "an outage settled the work order")
        assert store.envelopes(subject_wo_id=wo["id"]) == [], "an outage sent feedback"

        fleet.drain()  # the next tick retries the SAME round
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [(r["round"], r["outcome"]) for r in rounds] == [(1, "rejected")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    finally:
        store.close()


def test_three_outages_escalate_rather_than_retrying_for_ever(fleet):
    fleet.daemon.validator = Validator(claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(2):
            fleet.drain()
            assert store.get_work_order(wo["id"])["status"] == "validating"
        fleet.drain()
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "needs_review"
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
        assert true_blockers(store, fresh) == [VALIDATION_STUCK_BLOCKER]
    finally:
        store.close()


def test_two_failed_rounds_then_a_rejection_land_on_round_one(fleet):
    """Rounds are COUNTED, never inferred from the row count"""
    fleet.daemon.validator = Validator(
        claude_cli.ClaudeCliError("boom"), claude_cli.ClaudeCliError("boom"),
        rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    for _ in range(3):
        fleet.drain()

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [(r["round"], r["outcome"]) for r in rounds] == [(1, "rejected")]
    finally:
        store.close()


def test_a_project_switch_decides_whether_finish_opens_a_round_at_all(fleet):
    """Both directions, because either alone passes for the wrong reason: a submission site that ignored..."""
    fleet.reconfigure(enabled=False, project_validation={"enabled": True})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    assert finish(fleet, wo["id"])["status"] == "validating"

    fleet.reconfigure(enabled=True, project_validation={"enabled": False})
    other = fleet.dispatch("second")
    fleet.change(other["id"], "print('two')\n")
    assert finish(fleet, other["id"])["status"] == "waiting_pr_merge"


def test_the_daemon_prices_a_round_from_the_projects_settings_not_the_os_block(fleet):
    """The three `self.catalog.os.validation` reads were on threads already holding a `ProjectSpec`"""
    fleet.reconfigure(max_rounds=9, project_validation={"max_rounds": 1})
    fleet.daemon.validator = Validator(rejected("still no test"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = fleet.store()
    try:
        assert [(r["round"], r["outcome"])
                for r in store.validation_rounds(wo_id=wo["id"])] == [(1, "escalated")]
    finally:
        store.close()


def test_max_rounds_rejections_escalate(fleet):
    fleet.reconfigure(max_rounds=2)
    fleet.daemon.validator = Validator(rejected("still no test"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()  # the round-1 rejection reaches the worker

    fleet.change(wo["id"], "print('one')\nprint('two')\n")
    finish(fleet, wo["id"], summary="another go")
    fleet.drain()

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [(r["round"], r["outcome"]) for r in rounds] == [
            (1, "rejected"), (2, "escalated")]
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "needs_review"
        assert fresh["attention_reason"] == VALIDATION_STUCK_BLOCKER
        assert true_blockers(store, fresh) == [VALIDATION_STUCK_BLOCKER]
        # The last round sends nothing back: there is no round left to fix in
        assert len(store.envelopes(subject_wo_id=wo["id"])) == 1
    finally:
        store.close()


# -- the two escalations that never call the validator


def test_an_empty_diff_escalates_and_a_real_one_does_not(fleet):
    """A validator handed an empty diff will approve it"""
    validator = Validator(passed())
    fleet.daemon.validator = validator

    empty = fleet.dispatch("empty")
    fleet.worktree(empty["id"])  # a worktree, and not one byte changed in it
    finish(fleet, empty["id"])

    real = fleet.dispatch("real")
    fleet.change(real["id"], "print('real')\n")
    finish(fleet, real["id"])

    fleet.drain()
    fleet.drain()

    store = fleet.store()
    try:
        empty_fresh = store.get_work_order(empty["id"])
        assert empty_fresh["status"] == "needs_review"
        assert empty_fresh["attention_reason"] == VALIDATION_STUCK_BLOCKER
        assert store.latest_validation_round(wo_id=empty["id"])["outcome"] == "escalated"
        assert store.get_work_order(real["id"])["status"] == "waiting_pr_merge"
        assert [c["round"] for c in validator.calls] == [1], (
            "the validator was handed an empty diff")
    finally:
        store.close()


def test_a_repeated_fingerprint_escalates_and_one_byte_of_change_does_not(fleet):
    validator = Validator(rejected())
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()
    assert len(validator.calls) == 1

    # Resubmitted with nothing changed: same diff, same declared evidence
    finish(fleet, wo["id"], summary="reworded, same work")
    fleet.drain()

    store = fleet.store()
    try:
        assert len(validator.calls) == 1, "the validator judged the same evidence twice"
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert rounds[-1]["outcome"] == "escalated"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "needs_review"
        assert fresh["attention_reason"] == VALIDATION_STUCK_BLOCKER
    finally:
        store.close()

    # The pairing: one byte of new work, and the same machine runs the validator again
    other = fleet.dispatch("other")
    fleet.change(other["id"], "print('one')\n")
    finish(fleet, other["id"])
    fleet.drain()
    fleet.tick()
    fleet.change(other["id"], "print('one')\nprint('and a test')\n")
    finish(fleet, other["id"], summary="fixed")
    fleet.drain()
    assert [c["round"] for c in validator.calls] == [1, 1, 2]

    store = fleet.store()
    try:
        assert [r["outcome"] for r in store.validation_rounds(wo_id=other["id"])] == [
            "rejected", "rejected"]
    finally:
        store.close()


def test_fingerprints_a_b_a_do_not_escalate(fleet):
    """Compared against the IMMEDIATELY PRECEDING round only"""
    fleet.reconfigure(max_rounds=9)
    validator = Validator(rejected())
    fleet.daemon.validator = validator
    wo = fleet.dispatch()

    for text in ("A\n", "B\n", "A\n"):
        fleet.change(wo["id"], text)
        finish(fleet, wo["id"], summary=f"shape {text.strip()}")
        fleet.drain()
        fleet.tick()

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=wo["id"])
        assert [r["outcome"] for r in rounds] == ["rejected", "rejected", "rejected"]
        assert rounds[0]["fingerprint"] == rounds[2]["fingerprint"], (
            "the pairing is real: round 3 IS round 1's evidence again")
        assert rounds[1]["fingerprint"] != rounds[0]["fingerprint"]
        assert [c["round"] for c in validator.calls] == [1, 2, 3]
    finally:
        store.close()


# -- the kill switch


def test_the_kill_switch_drains_open_rounds_instead_of_stranding_them(fleet):
    """THE FLAG IS A SAFE STOP, NOT A TRAPDOOR"""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    open_round = fleet.dispatch("already inside")
    fleet.change(open_round["id"], "print('inside')\n")
    finish(fleet, open_round["id"])

    fleet.reconfigure(enabled=False)

    after = fleet.dispatch("after the switch")
    fleet.change(after["id"], "print('after')\n")
    assert finish(fleet, after["id"])["status"] == "waiting_pr_merge", (
        "a fresh finish under the disabled flag did not take today's path")

    held.release.set()
    fleet.drain()
    fleet.drain()

    store = fleet.store()
    try:
        assert store.validation_rounds(wo_id=after["id"]) == [], (
            "the disabled flag still opened a round")
        assert store.get_work_order(open_round["id"])["status"] == "waiting_pr_merge", (
            "turning the feature off stranded a unit already in validating")
        assert store.latest_validation_round(wo_id=open_round["id"])["outcome"] \
            == "passed"
        assert len(held.calls) == 1, "the daemon refused to judge an open round"
    finally:
        store.close()


def test_with_no_validator_wired_an_open_round_settles_unjudged(fleet, monkeypatch):
    """A round with nothing to judge it settles its unit unjudged"""
    monkeypatch.setattr(Daemon, "_validator", staticmethod(lambda cfg: None))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"], pr="https://github.com/x/y/pull/4")
    assert fleet.daemon.validator is None  # nothing injected: the real seam

    fleet.drain()

    store = fleet.store()
    try:
        latest = store.latest_validation_round(wo_id=wo["id"])
        assert latest["outcome"] == "failed"
        assert "never judged" in latest["reason"]
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "waiting_pr_merge"
        assert fresh["needs_attention"] == 0
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
        assert "validation_passed" not in kinds, "an unjudged round read as a pass"
    finally:
        store.close()


def test_a_pending_assumption_no_longer_holds_the_round_back(fleet):
    """THE POINT OF ISSUE 212: the two gates open together and are joined, not chained"""
    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    ops.assume(wo["id"], "assumed the exporter writes UTF-8")

    assert finish(fleet, wo["id"])["status"] == "needs_review"

    store = fleet.store()
    try:
        assert len(store.validation_rounds(wo_id=wo["id"])) == 1, \
            "the round waited for the user"
        assert store.get_work_order(wo["id"])["attention_reason"] == \
            "assumptions pending review"
    finally:
        store.close()

    fleet.drain()  # the panel judges it while the user is still deciding

    store = fleet.store()
    try:
        assert len(fleet.daemon.validator.calls) == 1
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
        # ...and the JOIN holds it: one gate cleared is not both
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()

    assert ops.review_work_order(wo["id"],
                                 accept=True)["status"] == "waiting_pr_merge"


def test_the_panel_reads_the_assumptions_it_is_judging_around(fleet):
    """An assumption is a decision embodied in the diff, and it ships in the PR like everything else"""
    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    ops.assume(wo["id"], "assumed the exporter writes UTF-8")
    finish(fleet, wo["id"])
    fleet.drain()

    packet = fleet.daemon.validator.calls[0]["packet"]
    assert [a["content"] for a in packet.assumptions] == \
        ["assumed the exporter writes UTF-8"]
    rendered = validation.build_packet_prompt(packet)
    assert "assumed the exporter writes UTF-8" in rendered
    assert "the user is deciding this now" in rendered


def test_a_refused_assumption_blocks_the_landing_a_pass_would_otherwise_do(fleet):
    """The hole parallelism opens"""
    fleet.daemon.validator = Validator(passed())
    wo = _parked_on_assumptions(fleet)

    ops.review_work_order(wo["id"], accept=False, feedback="use UTF-16 here")
    fleet.drain()

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
        # The feedback went out as a turn in this same tick
        assert "validation_landing_deferred" in \
            [e["kind"] for e in store.list_events(wo["id"])]
        assert _settle(store, wo["id"])
        fleet.tick()
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
        assert store.pending_assumptions(wo["id"]) == []
    finally:
        store.close()

    # The worker answers the refusal, and the work order may land again
    fleet.change(wo["id"], "print('utf-16')\n")
    assert finish(fleet, wo["id"], pr="https://github.com/x/y/pull/9")["status"] == \
        "validating"


# -- the second route into done


def test_a_rejection_while_the_user_is_deciding_reaches_the_worker_and_keeps_the_park(
        fleet):
    """The decision the user was offered and took: full parallelism"""
    fleet.reconfigure(max_rounds=3)
    fleet.daemon.validator = Validator(rejected("no test covers the change"))
    wo = _parked_on_assumptions(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "rejected"
        fresh = store.get_work_order(wo["id"])
        # Not settled and not un-parked by the verdict itself: the user still owes a decision
        assert fresh["status"] == "needs_review"
        assert len(store.pending_assumptions(wo["id"])) == 1
        assert true_blockers(store, fresh)[0] == "1 assumption pending your review"
        assert store.envelopes(subject_wo_id=wo["id"]), "the rejection reached nobody"
    finally:
        store.close()

    fleet.tick()  # routes the envelope, delivers it, and reconciles behind it

    store = fleet.store()
    try:
        delivered = [m for m in store.list_messages(wo["id"])
                     if "round 1" in m["content"]]
        assert delivered, "the worker was never given the feedback"
        fresh = store.get_work_order(wo["id"])
        assert len(store.pending_assumptions(wo["id"])) == 1
        # The flag `_deliver` cleared, back on the record and still naming the assumption
        assert fresh["needs_attention"] == 1
        assert fresh["attention_reason"] == "1 assumption pending your review"
    finally:
        store.close()


def test_an_escalation_while_the_user_is_deciding_leaves_the_assumption_in_front_of_them(
        fleet):
    """The other concurrent verdict, and the one that writes an attention flag of its own"""
    fleet.reconfigure(max_rounds=1)  # one rejection exhausts the budget
    fleet.daemon.validator = Validator(rejected("no test covers the change"))
    wo = _parked_on_assumptions(fleet)

    fleet.drain()

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "needs_review"
        assert len(store.pending_assumptions(wo["id"])) == 1
        assert true_blockers(store, fresh) == ["1 assumption pending your review",
                                               VALIDATION_STUCK_BLOCKER]
    finally:
        store.close()

    fleet.tick()  # the reconciler re-derives the reason; it must not lose the assumption

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["attention_reason"] == \
            "1 assumption pending your review"
    finally:
        store.close()

    assert ops.review_work_order(wo["id"], accept=True)["status"] == "waiting_pr_merge"

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert fresh["needs_attention"] == 0
        assert len(store.validation_rounds(wo_id=wo["id"])) == 1, "a second round opened"
    finally:
        store.close()


def test_a_cancelled_work_orders_open_round_is_never_picked_up(fleet):
    """The bound the round-keyed query needs and the status-keyed one got for free"""
    fleet.daemon.validator = Validator(passed())
    live = _parked_on_assumptions(fleet)
    stopped = _parked_on_assumptions(fleet, pr="https://github.com/x/y/pull/13")
    ops.cancel(stopped["id"])

    fleet.drain()

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=live["id"])["outcome"] == "passed"
        assert store.latest_validation_round(wo_id=stopped["id"])["outcome"] == "pending"
        assert store.get_work_order(stopped["id"])["status"] == "cancelled"
    finally:
        store.close()


def _parked_on_assumptions(fleet: Fleet, *, evidence: str = "ran `pytest -q`: 412 passed",
                           pr: str = "https://github.com/x/y/pull/9") -> dict:
    """A work order in the state `finish` leaves one that filed an assumption"""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('reviewed')\n")
    ops.assume(wo["id"], "assumed the exporter writes UTF-8")
    assert ops.finish(wo["id"], "done", pr_url=pr,
                      evidence=evidence)["status"] == "needs_review"
    return wo


def test_accepting_assumptions_validates_only_when_the_feature_is_on(fleet):
    """THE PAIRING IS THE TEST"""
    fleet.daemon.validator = Validator(passed())

    # -- the flag OFF: exactly today's behaviour, and not one row in validation_rounds
    fleet.reconfigure(enabled=False)
    off = _parked_on_assumptions(fleet)
    assert ops.review_work_order(off["id"], accept=True)["status"] == "waiting_pr_merge"

    store = fleet.store()
    try:
        assert store.validation_rounds(wo_id=off["id"]) == []
        assert store.get_work_order(off["id"])["status"] == "waiting_pr_merge"
    finally:
        store.close()

    # -- the flag ON: the same accept parks it for review instead
    fleet.reconfigure(enabled=True)
    on = _parked_on_assumptions(fleet)
    assert ops.review_work_order(on["id"], accept=True)["status"] == "validating"

    store = fleet.store()
    try:
        rounds = store.validation_rounds(wo_id=on["id"])
        assert len(rounds) == 1
        assert rounds[0]["round"] == 1
        assert rounds[0]["outcome"] == "pending"
        assert rounds[0]["fingerprint"], "a round with no fingerprint proves nothing"
        fresh = store.get_work_order(on["id"])
        assert fresh["status"] == "validating"
        assert fresh["needs_attention"] == 0  # under review is the OS working
        assert [e["kind"] for e in store.list_events(on["id"])].count(
            "validation_submitted") == 1
    finally:
        store.close()


def test_a_work_order_already_judged_is_not_validated_a_second_time(fleet):
    """PAIRED with a never-validated one in the same test"""
    fleet.reconfigure(max_rounds=1)  # one rejection is the whole budget
    fleet.daemon.validator = Validator(rejected("no test covers the change"))

    # -- judged already: the round ran, the reviewer gave up, and it is in front of the
    judged = fleet.dispatch("judged")
    fleet.change(judged["id"], "print('judged')\n")
    finish(fleet, judged["id"], pr="https://github.com/x/y/pull/10")
    fleet.drain()
    store = fleet.store()
    try:
        assert store.get_work_order(judged["id"])["status"] == "needs_review"
        assert store.get_work_order(judged["id"])["attention_reason"] == (
            VALIDATION_STUCK_BLOCKER)
        before = len(store.validation_rounds(wo_id=judged["id"]))
        assert before == 1, "the control never got a round"
    finally:
        store.close()

    assert ops.review_work_order(judged["id"], accept=True)["status"] == (
        "waiting_pr_merge")

    # -- never judged: parked while the panel was off, so the accept opens round 1
    fleet.reconfigure(enabled=False, max_rounds=1)
    virgin = _parked_on_assumptions(fleet, pr="https://github.com/x/y/pull/11")
    store = fleet.store()
    try:
        assert store.validation_rounds(wo_id=virgin["id"]) == []
    finally:
        store.close()
    fleet.reconfigure(max_rounds=1)
    assert ops.review_work_order(virgin["id"], accept=True)["status"] == "validating"

    store = fleet.store()
    try:
        assert len(store.validation_rounds(wo_id=judged["id"])) == before
        assert store.get_work_order(judged["id"])["status"] == "waiting_pr_merge"
        assert store.get_work_order(judged["id"])["needs_attention"] == 0
        assert len(store.validation_rounds(wo_id=virgin["id"])) == 1
    finally:
        store.close()


def test_the_review_route_carries_the_evidence_the_worker_declared(fleet):
    """`finish` drops a work order with pending assumptions into `needs_review` before it ever reaches the..."""
    declared = "`uv run pytest -q` — 412 passed; exporter checked by hand on 3 files"
    told = _parked_on_assumptions(fleet, evidence=declared)
    silent = _parked_on_assumptions(fleet, evidence="",
                                    pr="https://github.com/x/y/pull/12")

    ops.review_work_order(told["id"], accept=True)
    ops.review_work_order(silent["id"], accept=True)

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=told["id"])["evidence"] == declared
        assert store.latest_validation_round(wo_id=silent["id"])["evidence"] == ""
        assert store.get_work_order(silent["id"])["status"] == "validating"
    finally:
        store.close()


def test_rejecting_assumptions_opens_no_round_of_its_own(fleet):
    """A rejection is not a route into done"""
    fleet.daemon.validator = Validator(passed())
    wo = _parked_on_assumptions(fleet)
    store = fleet.store()
    try:
        before = len(store.validation_rounds(wo_id=wo["id"]))
    finally:
        store.close()

    out = ops.review_work_order(wo["id"], accept=False, feedback="use UTF-16 here")

    assert out["status"] == "needs_review"
    store = fleet.store()
    try:
        assert len(store.validation_rounds(wo_id=wo["id"])) == before
        assert store.get_work_order(wo["id"])["status"] != "validating"
    finally:
        store.close()


# -- the give-up reaches the user (issue #199)


def _outbox(fleet):
    """The VALIDATION outbox rows the next tick will route"""
    store = fleet.store()
    try:
        return [r for r in store.unrouted_notifications()
                if r["source"] == "validation"]
    finally:
        store.close()


def test_a_give_up_notifies_and_a_rejection_stays_silent(fleet):
    """PAIRED, because "a notification exists" says nothing about which transition wrote it"""
    fleet.reconfigure(max_rounds=2)
    fleet.daemon.validator = Validator(rejected("no test covers the change"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert _outbox(fleet) == [], "round 1 was rejected, not given up on"
    fleet.tick()  # the round-1 rejection reaches the worker

    fleet.change(wo["id"], "print('one')\nprint('two')\n")
    finish(fleet, wo["id"], summary="another go")
    fleet.drain()

    rows = _outbox(fleet)
    assert len(rows) == 1
    # The title names the FAMILY now: spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §3
    assert rows[0]["title"] == ops.ESCALATION_TITLE.format(
        unit=wo["id"], n=2, verb=ops.ESCALATION_VERB["handover"])
    assert rows[0]["body"] == "no test covers the change", (
        "the body is the round's own reason — the only text that says why")
    assert (rows[0]["level"], rows[0]["source"], rows[0]["wo_id"]) == (
        "warning", "validation", wo["id"])


def test_the_give_up_notification_reaches_the_central_inbox(fleet):
    """The outbox row is only half the path: `route_outbox` -> central inbox -> sinks is what puts a..."""
    fleet.reconfigure(max_rounds=1)
    fleet.daemon.validator = Validator(rejected("the change is untested"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()  # routes the outbox

    assert _outbox(fleet) == [], "the row was routed, not left behind"
    central = CentralStore()
    try:
        rows = [i for i in central.unacked_inbox() if i["wo_id"] == wo["id"]]
    finally:
        central.close()
    assert len(rows) == 1
    assert rows[0]["title"] == ops.ESCALATION_TITLE.format(
        unit=wo["id"], n=1, verb=ops.ESCALATION_VERB["handover"])
    assert rows[0]["body"] == "the change is untested"
    assert (rows[0]["level"], rows[0]["project"]) == ("warning", "proj_a")


def test_the_escalations_that_never_call_the_validator_notify_too(fleet):
    """An empty diff gives up without a verdict to quote"""
    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch("empty")
    fleet.worktree(wo["id"])  # a worktree, and not one byte changed in it
    finish(fleet, wo["id"])
    fleet.drain()

    rows = _outbox(fleet)
    assert len(rows) == 1
    assert rows[0]["title"] == ops.ESCALATION_TITLE.format(
        unit=wo["id"], n=1, verb=ops.ESCALATION_VERB["not_reviewed"])
    assert "rejected" not in rows[0]["title"]
    assert "changes no files" in rows[0]["body"]


def test_three_outages_ping_the_user_once_not_once_per_attempt(fleet):
    """The outage path writes a `validation_failed` event per attempt and escalates on the third"""
    fleet.daemon.validator = Validator(claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    for _ in range(2):
        fleet.drain()
        assert _outbox(fleet) == [], "an outage is not a verdict and asks nothing"
    fleet.drain()

    rows = _outbox(fleet)
    assert len(rows) == 1
    assert "unreachable 3 times" in rows[0]["body"]


def test_a_long_reason_is_cut_and_says_that_it_was(fleet):
    """A panel reason runs to paragraphs and a sink renders it as one push"""
    long_reason = "the seats disagree about the header row. " * 40
    assert len(long_reason) > VALIDATION_REASON_CHARS, "the fixture must exceed the cut"
    fleet.reconfigure(max_rounds=1)
    fleet.daemon.validator = Validator(rejected(long_reason))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    rows = _outbox(fleet)
    assert len(rows) == 1
    assert rows[0]["body"] == (long_reason[:VALIDATION_REASON_CHARS]
                              + VALIDATION_REASON_CUT)
    assert rows[0]["body"].endswith(VALIDATION_REASON_CUT), "the cut is visible"

    store = fleet.store()
    try:
        # The cut is the NOTIFICATION's, not the record's: the round keeps the whole reason
        assert store.latest_validation_round(wo_id=wo["id"])["reason"] == long_reason
    finally:
        store.close()


def test_a_reason_that_fits_is_not_marked_as_cut(fleet):
    """The pairing for the test above: without it"""
    fleet.reconfigure(max_rounds=1)
    fleet.daemon.validator = Validator(rejected("no test covers the change"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    rows = _outbox(fleet)
    assert len(rows) == 1
    assert rows[0]["body"] == "no test covers the change"
    assert VALIDATION_REASON_CUT not in rows[0]["body"]


def test_a_passed_round_does_not_land_an_order_whose_worker_is_typing(fleet,
                                                                     fake_claude):
    """Measured on wo-83e4183c: a round opened before turn 13 came back `passed` while turn 13 ran"""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.daemon.validator = Validator(passed())
    gate = fake_claude.hold_turns()

    store = fleet.store()
    try:
        store.queue_message(wo["id"], "one more thing")
        fleet.drain()  # the message goes out, then the round is judged behind it

        round_row = store.latest_validation_round(wo_id=wo["id"])
        assert round_row["outcome"] == "passed"
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
        assert "validation_passed" in kinds
        assert "validation_landing_deferred" in kinds
        assert store.get_work_order(wo["id"])["status"] == "running", (
            "the verdict landed a work order whose worker was mid-turn")

        # The turn ends; the landing the round deferred is the settler's to finish
        gate.unlink()
        assert _settle(store, wo["id"])
        fleet.tick()

        assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
        assert [e["kind"] for e in store.list_events(wo["id"])].count(
            "validation_landed") == 1
    finally:
        store.close()


def _defer_under_a_live_turn(fleet, fake_claude, store, wo_id: str):
    """Park `wo_id` in `validating` WITH a worker turn in flight"""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    gate = fake_claude.hold_turns()

    store.queue_message(wo_id, "one more thing")
    fleet.tick()  # the turn goes out and the round starts behind it
    assert held.entered.wait(timeout=15), "the round never reached the validator"
    store.set_status(wo_id, "validating")  # the measured pair: validating + live turn
    held.release.set()
    deadline = time.monotonic() + 15
    while fleet.daemon.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.daemon.validating

    assert store.latest_validation_round(wo_id=wo_id)["outcome"] == "passed"
    kinds = [e["kind"] for e in store.list_events(wo_id)]
    assert "validation_landing_deferred" in kinds
    assert "validation_landed" not in kinds
    assert store.get_work_order(wo_id)["status"] == "validating"
    assert not store.queued_messages(wo_id)
    return gate


def test_a_deferred_landing_is_taken_when_the_order_is_still_validating(fleet,
                                                                       fake_claude):
    """The shape nothing else covers: the deferral happened while the status WAS `validating`"""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        gate = _defer_under_a_live_turn(fleet, fake_claude, store, wo["id"])

        gate.unlink()
        assert _settle(store, wo["id"])
        fleet.tick()

        assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
        assert [e["kind"] for e in store.list_events(wo["id"])].count(
            "validation_landed") == 1
    finally:
        store.close()


def test_the_deferred_landing_waits_while_the_worker_is_still_typing(fleet,
                                                                    fake_claude):
    """The pairing: the hoisted recovery must keep the deferral's own predicate"""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        gate = _defer_under_a_live_turn(fleet, fake_claude, store, wo["id"])

        fleet.tick()  # the turn is still held

        assert store.get_work_order(wo["id"])["status"] == "validating"
        assert "validation_landed" not in [e["kind"]
                                           for e in store.list_events(wo["id"])]
        gate.unlink()
        assert _settle(store, wo["id"])
    finally:
        store.close()


def test_a_pending_round_still_stops_the_settler_at_validating(fleet):
    """The return the recovery was hoisted above is not widened: a round still open owns the work order"""
    held = Validator(passed())
    held.block()  # the round stays `pending` across the tick
    fleet.daemon.validator = held

    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        fleet.tick()
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "pending"
        assert store.get_work_order(wo["id"])["status"] == "validating"
        assert "validation_landed" not in [e["kind"]
                                           for e in store.list_events(wo["id"])]
    finally:
        held.release.set()
        fleet.drain()
        store.close()


def test_a_give_up_survives_the_turn_that_follows_it(fleet, fake_claude):
    """The mirror of the test above, and the case where the landing must NOT be deferred: a give-up writes..."""
    fleet.reconfigure(max_rounds=1)  # one rejection exhausts the budget
    fleet.daemon.validator = Validator(rejected("no test covers the change"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])  # the `pr_url` the settler reacts to, written here

    fleet.drain()

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "needs_review"
        assert true_blockers(store, fresh) == [VALIDATION_STUCK_BLOCKER]

        # The turn that follows the refusal, held open across the give-up it must not undo
        gate = fake_claude.hold_turns()
        store.queue_message(wo["id"], "one more thing")
        fleet.tick()
        assert store.get_work_order(wo["id"])["status"] == "running"

        gate.unlink()
        assert _settle(store, wo["id"])
        fleet.tick()

        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] != "waiting_pr_merge", (
            "the turn ending parked a give-up on the merge queue")
        assert fresh["status"] == "needs_review"
        assert VALIDATION_STUCK_BLOCKER in true_blockers(store, fresh)
    finally:
        store.close()


def test_a_give_up_survives_a_message_turn_after_a_round_that_judged_nothing(
        fleet, fake_claude):
    """The issue's order end to end: a row that JUDGED NOTHING sits after the give-up (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md §A.)"""
    fleet.reconfigure(max_rounds=1)
    fleet.daemon.validator = Validator(rejected("no test covers the change"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "needs_review"

        void = store.open_validation_round(wo_id=wo["id"], fingerprint="fp-after")
        store.close_validation_round(void["id"], "void", "nothing to judge")
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "void"

        gate = fake_claude.hold_turns()
        store.queue_message(wo["id"], "one more thing")
        fleet.tick()
        assert store.get_work_order(wo["id"])["status"] == "running"

        gate.unlink()
        assert _settle(store, wo["id"])
        fleet.tick()

        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] != "waiting_pr_merge", (
            "a row that judged nothing dropped the give-up onto the merge queue")
        assert fresh["status"] == "needs_review"
        assert VALIDATION_STUCK_BLOCKER in true_blockers(store, fresh)
    finally:
        store.close()
