"""The work-order round machine: `finish` opens a round, the daemon judges it off the
tick thread, the reconciler keeps its hands off.

THE VALIDATOR IS INJECTED. This work order builds the loop and defines the seam; the
panel that fills it is a later one, so every test here hands `Daemon.validator` a fake
callable and asserts on STORED ROWS AND CALL COUNTS. That discipline is not stylistic:
validation ships disabled, and a test that reaches this code through the daemon without
enabling it exercises the OLD path and still gets a perfectly good ending. "A verdict
came back" proves nothing about which machine produced it.

Most tests here are PAIRS for the same reason. "It did not escalate" and "the reconciler
did nothing this tick" are the same observation unless something beside it moved.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jarvis import claude_cli, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.invariants import VALIDATION_STUCK_BLOCKER, true_blockers
from jarvis.project_store import VALIDATOR_SEATS, ProjectStore
from jarvis.testing import make_git_project


# -- the fixture: a real repository, a real worktree, a fake panel --------------------


def _git(cwd: Path, *args: str) -> str:
    """git with a pinned identity and no user or system config in sight."""
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


class Validator:
    """The injected seam, driven by the test.

    Records every entry — the call count is what most of these tests actually assert —
    and answers with the queued outcomes in order, repeating the last one for ever
    after. An outcome that is an exception is raised instead of returned, which is how
    a transport outage is staged.
    """

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
        # A round is up to five headless calls; holding here is what lets a test watch
        # the tick thread carry on without it.
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
    """What a panel hands back per seat — named, so the opinions are real rows."""
    return [{"seat": seat, "status": "ok", "verdict": verdict, "model": "sonnet",
             "latency_ms": 12,
             "reply": f"the {seat} seat says: {verdict} — no test covers the change"}
            for seat in ("tester", "security")]


class Fleet:
    """One booted OS, plus the git plumbing these tests need."""

    def __init__(self, tmp_path, project, catalog_path):
        self.tmp_path = tmp_path
        self.project = project
        self.catalog_path = catalog_path
        self.daemon = Daemon(load_catalog(catalog_path))

    # -- configuration ---------------------------------------------------------
    def reconfigure(self, **validation) -> None:
        """Rewrite the catalog on disk AND reload the daemon's copy.

        Both halves matter: `ops.finish` reads the catalog from disk (it runs in the
        worker's process, not the daemon's), and the daemon reads its own loaded copy.
        """
        write_catalog(self.tmp_path, self.project, **validation)
        self.daemon.catalog = load_catalog(self.catalog_path)

    @property
    def spec(self):
        return self.daemon.catalog.projects[0]

    def store(self) -> ProjectStore:
        return ProjectStore(self.project)

    # -- work orders -----------------------------------------------------------
    def dispatch(self, title: str = "task", **kw) -> dict:
        """Create a work order and give it a session, the way a real one gets one."""
        wo = ops.create_work_order("proj_a", title, **kw)
        self.daemon.tick()
        store = self.store()
        try:
            assert _settle(store, wo["id"])
        finally:
            store.close()
        return wo

    def worktree(self, wo_id: str) -> Path:
        """The worktree the real `claude --worktree` would have cut. The fake CLI does
        not, so the tests do it themselves — with real git, because the evidence
        collector is a reading of what git says."""
        path = self.project / ".claude" / "worktrees" / wo_id
        if not path.is_dir():
            _git(self.project, "worktree", "add", "-q", "-b", f"b-{wo_id}",
                 str(path), "main")
        return path

    def change(self, wo_id: str, text: str, name: str = "app.py") -> Path:
        """A committed change in the work order's worktree — evidence to judge."""
        tree = self.worktree(wo_id)
        (tree / name).write_text(text)
        _git(tree, "add", "-A")
        _git(tree, "commit", "-qm", f"work {len(text)}")
        return tree

    # -- the daemon ------------------------------------------------------------
    def tick(self) -> None:
        self.daemon.tick_count = 0
        self.daemon.tick()

    def drain(self, timeout: float = 15.0) -> None:
        """One tick, then wait for whatever it started off-thread to finish."""
        self.tick()
        deadline = time.monotonic() + timeout
        while self.daemon.validating and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not self.daemon.validating, "a validation round never finished"


def _settle(store: ProjectStore, wo_id: str, timeout: float = 15.0) -> bool:
    """Wait for this work order's turn to end — a turn is a detached process."""
    from jarvis import worker_session

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        list(worker_session.poll(store))
        turn = store.latest_turn(wo_id)
        if turn is not None and turn["state"] != "running":
            return True
        time.sleep(0.02)
    return False


def write_catalog(tmp_path: Path, project: Path, **validation) -> Path:
    data = {
        "os": {
            "defaults": {"model": "sonnet"},
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
def fleet(tmp_path, jarvis_home, fake_claude, claude_json):
    """A booted OS over a REAL git repository, with validation enabled.

    The repository is real and its default branch is named explicitly: the collector
    resolves the merge base through a ladder that ends at `main`, and a repo whose
    `git init` happened to name the branch something else would collect an empty diff
    and make every escalation test pass for the wrong reason.
    """
    project = make_git_project(tmp_path, "proj_a")
    _git(project, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    claude_json(project)
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    return Fleet(tmp_path, project, catalog_path)


def finish(fleet: Fleet, wo_id: str, summary: str = "done", pr: str | None = None,
           evidence: str = "ran `pytest -q`: 412 passed") -> dict:
    return ops.finish(wo_id, summary, pr_url=pr, evidence=evidence)


# -- submission ----------------------------------------------------------------------


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
        # `validating` is the system working, not a decision anyone owes.
        assert fresh["needs_attention"] == 0
        assert [e["kind"] for e in store.list_events(wo["id"])].count(
            "validation_submitted") == 1
    finally:
        store.close()


def test_a_worker_that_omits_evidence_still_finishes(fleet):
    """`--evidence` is optional on purpose: every worker in flight across the release
    that adds the flag predates it, and an empty declaration is an ordinary submission
    rather than a thin one."""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")

    assert ops.finish(wo["id"], "done")["status"] == "validating"

    store = fleet.store()
    try:
        round_row = store.latest_validation_round(wo_id=wo["id"])
        assert round_row["evidence"] == ""
        assert round_row["fingerprint"]
        fleet.daemon.validator = Validator(passed())
        fleet.drain()
        assert store.get_work_order(wo["id"])["status"] == "completed"
    finally:
        store.close()


# -- the reconciler stands back ------------------------------------------------------


def test_the_reconciler_does_not_settle_a_validating_work_order(fleet):
    """THE SHARPEST TRAP IN THE FEATURE, and it needs the pairing.

    `settle_work_order` re-derives the outcome from the latest turn on EVERY tick: a
    done turn with a `result_summary` and a `pr_url` means `waiting_pr_merge`, no
    questions asked. The second work order below is that same row in every respect
    except that it is not validating, and it takes exactly that path on the same tick —
    without it, "still validating" is indistinguishable from "the reconciler did nothing
    this tick".
    """
    held = Validator(passed())
    held.block()  # the round stays open across both ticks
    fleet.daemon.validator = held

    guarded = fleet.dispatch("guarded")
    fleet.change(guarded["id"], "print('guarded')\n")
    finish(fleet, guarded["id"], pr="https://github.com/x/y/pull/1")

    loose = fleet.dispatch("loose")
    store = fleet.store()
    try:
        # The same row, minus the round: finished, with a pull request, still `running`.
        store.update_work_order(loose["id"], result_summary="done",
                                pr_url="https://github.com/x/y/pull/2")

        fleet.tick()
        assert held.entered.wait(timeout=15), "the validator never started"
        fleet.tick()

        assert store.get_work_order(guarded["id"])["status"] == "validating"
        assert store.get_work_order(loose["id"])["status"] == "waiting_pr_merge"

        # And handed in directly, because the status filter in `settle_turns` is only
        # the FIRST guard. The early return is the second, and it is the one that holds
        # the day a caller passes a validating work order in — which is precisely how
        # this bug would come back.
        fleet.daemon.settle_work_order(fleet.spec, store,
                                       store.get_work_order(guarded["id"]))
        assert store.get_work_order(guarded["id"])["status"] == "validating"
    finally:
        held.release.set()
        store.close()


def test_a_validating_work_order_is_not_polled_and_keeps_its_pull_request(
        fleet, fake_gh):
    """`poll_pull_requests` looks only at `waiting_pr_merge`. Both halves asserted: a
    validating work order must not be polled, and it must not lose its url on the way
    through the round machine either."""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    pr = "https://github.com/x/y/pull/9"
    finish(fleet, wo["id"], pr=pr)
    fake_gh.set_pr(pr, "MERGED", merged_at="2026-08-21T00:00:00Z")

    store = fleet.store()
    try:
        fleet.daemon.poll_pull_requests(fleet.spec, store)
        assert fake_gh.calls == [], "a validating work order was polled"
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "validating"
        assert fresh["pr_url"] == pr, "the round machine dropped the pull request"
    finally:
        held.release.set()
        store.close()


def test_wo_done_closes_a_validating_work_order(fleet):
    """The user's escape hatch has to work from inside a review, or a wedged validation
    is a support incident with no way out."""
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


# -- off the tick thread -------------------------------------------------------------


def test_the_validator_runs_off_the_tick_thread_with_its_own_store(fleet):
    """Five seats at a 300-second timeout would freeze every project in the catalog if
    they ran inline, so the tick must return while the round is still in flight.

    AND the pool thread must open its OWN store: `db.connect` does not pass
    `check_same_thread=False`, so a round that reused the daemon's connection would
    raise `ProgrammingError` on its first write. Adding that flag would silently stop
    this proving anything — the identity assertion below is what keeps the rule
    readable after it stopped being enforced by an exception.
    """
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
    """Opened on the submitting thread, before anything fans out, so a crash
    mid-validation leaves a `pending` round something can later find rather than a work
    order sitting in `validating` with no trace of why."""
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


# -- a pass ---------------------------------------------------------------------------


def test_a_pass_settles_exactly_where_finish_would(fleet):
    """`waiting_pr_merge` with a pull request, `completed` without one — the same two
    endings `ops.finish` reaches with validation switched off, reached by the same
    function so they cannot drift."""
    fleet.daemon.validator = Validator(passed())

    with_pr = fleet.dispatch("with pr")
    fleet.change(with_pr["id"], "print('pr')\n")
    finish(fleet, with_pr["id"], pr="https://github.com/x/y/pull/3")

    without = fleet.dispatch("without pr")
    fleet.change(without["id"], "print('no pr')\n")
    finish(fleet, without["id"])

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
    """The trap `ops.finish` carries: it closes the backlog item only when the resulting
    status is `completed`, so routing through `validating` would silently stop backlog
    items closing at all."""
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

    finish(fleet, wo["id"])
    central = CentralStore()
    try:
        assert central.get_backlog(item["id"])["status"] == "open", (
            "closed before anything judged it")
        fleet.drain()
        assert central.get_backlog(item["id"])["status"] == "done"
    finally:
        central.close()


# -- a rejection ----------------------------------------------------------------------


def _reject_one(fleet, title: str = "task", **kw) -> tuple[dict, Validator]:
    validator = Validator(rejected(), passed())
    fleet.daemon.validator = validator
    wo = fleet.dispatch(title)
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"], **kw)
    fleet.drain()
    return wo, validator


def test_a_rejection_reaches_the_worker_as_ordinary_review_feedback(fleet):
    """AND ONLY VIA THE BUS, AND SAYING NOTHING ABOUT WHO JUDGED IT.

    The implementing side is never told a panel exists — principle 3 of the design: each
    judging entity sees input and produces output and knows nothing else. That is a
    "this must not appear" assertion, which is worthless alone, so it is paired in this
    same test with the place the seats DO appear: the `validation_opinions` rows, which
    name every seat and carry its reply verbatim.
    """
    wo, _ = _reject_one(fleet)
    store = fleet.store()
    try:
        envelopes = store.envelopes(subject_wo_id=wo["id"])
        assert len(envelopes) == 1
        assert envelopes[0]["to_role"] == "implementor"
        assert envelopes[0]["from_role"] == "reviewer"
        assert envelopes[0]["kind"] == "review_feedback"

        fleet.tick()  # the router delivers it; the worker's next turn goes out
        # `list_messages` carries both directions; the worker's own final output is
        # the other one. Only what was sent INTO the session is under test here.
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

        # …and here is where the seats DO live, for the same round.
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
    """What pins the decoupling: with `queue_message` booby-trapped, a rejection must
    still post its envelope. The round machine names a ROLE and forgets — it does not
    know a work order is on the other end, and the day the recipient is a feature
    order's manager instead, nothing here changes."""
    fleet.daemon.validator = Validator(rejected())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    def boom(*a, **kw):
        raise AssertionError("the round machine queued a message directly")

    # Armed only around the round itself: reaping a worker turn records the worker's own
    # final reply through the same method, and trapping that would prove nothing.
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


# -- round accounting -----------------------------------------------------------------


def test_opening_a_round_twice_yields_one_row_but_a_real_resubmission_advances(fleet):
    """Idempotent per (work order, round), enforced by the partial unique index rather
    than by a SELECT before the INSERT — that check-then-insert is a race with no lock.

    Paired with a genuine resubmission, or the test would be satisfied by a machine that
    never advances a round at all.
    """
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
    """A `ClaudeCliError` is the panel being unreachable, not a verdict on the work.

    The pairing is the whole test: an outage that left the round `failed` but still
    advanced the counter would be indistinguishable from this one until the third bad
    night on the network gave up on a work order nothing had ever judged.
    """
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
    """Rounds are COUNTED, never inferred from the row count. Two outages and a
    rejection is round ONE rejected — round three would mean two bad nights on the
    network had spent two thirds of this work order's budget."""
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
        # The last round sends nothing back: there is no round left to fix in.
        assert len(store.envelopes(subject_wo_id=wo["id"])) == 1
    finally:
        store.close()


# -- the two escalations that never call the validator --------------------------------


def test_an_empty_diff_escalates_and_a_real_one_does_not(fleet):
    """A validator handed an empty diff will approve it, and that one silent pass would
    make the whole feature theatre. Paired with the identical work order that DID change
    something, so "the validator was not called" is a fact about the diff."""
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
        assert store.get_work_order(real["id"])["status"] == "completed"
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

    # Resubmitted with nothing changed: same diff, same declared evidence.
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

    # The pairing: one byte of new work, and the same machine runs the validator again.
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
    """Compared against the IMMEDIATELY PRECEDING round only. A submitter told to go
    back to a shape it already tried is answering the feedback, not repeating itself —
    and nothing else distinguishes "the previous round" from "any previous round"."""
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


# -- the kill switch ------------------------------------------------------------------


def test_the_kill_switch_drains_open_rounds_instead_of_stranding_them(fleet):
    """THE FLAG IS A SAFE STOP, NOT A TRAPDOOR.

    `os.validation.enabled` gates OPENING a round and never settling one. A user who
    turns the panel off at three in the morning because it is misbehaving must not
    thereby strand every unit already inside it — so the daemon still judges and settles
    what is open, and only `ops.finish` reverts to today's path immediately. Both halves
    are asserted here because nothing else covers either.
    """
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    open_round = fleet.dispatch("already inside")
    fleet.change(open_round["id"], "print('inside')\n")
    finish(fleet, open_round["id"])

    fleet.reconfigure(enabled=False)

    after = fleet.dispatch("after the switch")
    fleet.change(after["id"], "print('after')\n")
    assert finish(fleet, after["id"])["status"] == "completed", (
        "a fresh finish under the disabled flag did not take today's path")

    held.release.set()
    fleet.drain()
    fleet.drain()

    store = fleet.store()
    try:
        assert store.validation_rounds(wo_id=after["id"]) == [], (
            "the disabled flag still opened a round")
        assert store.get_work_order(open_round["id"])["status"] == "completed", (
            "turning the feature off stranded a unit already in validating")
        assert store.latest_validation_round(wo_id=open_round["id"])["outcome"] \
            == "passed"
        assert len(held.calls) == 1, "the daemon refused to judge an open round"
    finally:
        store.close()


def test_with_no_validator_wired_an_open_round_settles_unjudged(fleet):
    """The shipped default: this work order defines the seam and a later one fills it.

    Closed `failed` and never `passed` — a round nobody judged must not read as a
    verdict on any surface — and the work order lands exactly where it lands with
    validation switched off, so an unwired seam costs nothing and hides nothing.
    """
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


def test_pending_assumptions_still_outrank_validation(fleet):
    """A decision the OS is waiting on is not something a reviewer can settle. The work
    order goes to `needs_review` exactly as it does today, and no round is opened —
    validation is about the work, and this is about a question the user has not answered.
    """
    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    ops.assume(wo["id"], "assumed the exporter writes UTF-8")

    assert finish(fleet, wo["id"])["status"] == "needs_review"

    store = fleet.store()
    try:
        assert store.validation_rounds(wo_id=wo["id"]) == []
        assert store.get_work_order(wo["id"])["attention_reason"] == \
            "assumptions pending review"
        fleet.drain()
        assert fleet.daemon.validator.calls == []
    finally:
        store.close()


# -- the second route into done ------------------------------------------------------
#
# `ops.review_work_order` reaches `waiting_pr_merge`/`completed` without ever touching
# `ops.finish`, so a work order that filed assumptions used to arrive at the merge queue
# with nothing having judged it.


def _parked_on_assumptions(fleet: Fleet, *, evidence: str = "ran `pytest -q`: 412 passed",
                           pr: str = "https://github.com/x/y/pull/9") -> dict:
    """A work order in the state `finish` leaves one that filed an assumption."""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('reviewed')\n")
    ops.assume(wo["id"], "assumed the exporter writes UTF-8")
    assert ops.finish(wo["id"], "done", pr_url=pr,
                      evidence=evidence)["status"] == "needs_review"
    return wo


def test_accepting_assumptions_validates_only_when_the_feature_is_on(fleet):
    """THE PAIRING IS THE TEST. Validation ships disabled, so an accept that lands in
    `waiting_pr_merge` proves nothing on its own — that is where it lands today and
    where it landed before this file existed. The same review is run twice, once each
    side of the flag, and the two endings have to differ."""
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
    """PAIRED with a never-validated one in the same test. "No new round" is satisfied
    perfectly by an implementation that opens no rounds at all on this route, so the
    control has to open one.

    The rule: a round already on record means the loop has run. An acceptance after
    that is the USER overruling the machine, and the machine does not get a second
    vote — re-submitting would hand the work order straight back to the reviewer that
    had already given up on it.
    """
    fleet.reconfigure(max_rounds=1)  # one rejection is the whole budget
    fleet.daemon.validator = Validator(rejected("no test covers the change"))

    # -- judged already: the round ran, the reviewer gave up, and it is in front of the
    # user for exactly that reason. They say ship it anyway.
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

    # -- never judged: the same accept opens round 1
    virgin = _parked_on_assumptions(fleet, pr="https://github.com/x/y/pull/11")
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
    """`finish` drops a work order with pending assumptions into `needs_review` before
    it ever reaches the validation branch, so without this the evidence its worker
    passed would be lost and round 1 would open empty — on exactly the work orders that
    filed assumptions. It is recovered from the `finished` event's payload.

    Paired with a worker that declared nothing, which must still finish: `--evidence` is
    optional, and an empty declaration is an ordinary submission rather than a thin one.
    """
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


def test_rejecting_assumptions_opens_no_round(fleet):
    """A rejection is not a route into done. The work order goes back to its worker with
    the user's reasoning, and there is nothing finished for anyone to judge."""
    fleet.daemon.validator = Validator(passed())
    wo = _parked_on_assumptions(fleet)

    out = ops.review_work_order(wo["id"], accept=False, feedback="use UTF-16 here")

    assert out["status"] == "needs_review"
    store = fleet.store()
    try:
        assert store.validation_rounds(wo_id=wo["id"]) == []
        assert store.get_work_order(wo["id"])["status"] != "validating"
        fleet.drain()
        assert fleet.daemon.validator.calls == []
    finally:
        store.close()
