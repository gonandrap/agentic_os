"""The feature round machine: the integrated diff, the rounds, and the manager's loop.

A work order's panel judges one worktree. A FEATURE order's panel judges what the
children's work adds up to on the default branch — which is the only place two children
that are each individually correct can be seen to be jointly wrong. That is the whole
marginal value of this loop, and everything below protects one of the four things that
make it work:

* **The base.** `feature_orders.base_sha` is written when the plan is released, and the
  diff is everything between it and the default branch now. A feature with no base gets
  no diff at all rather than a guessed one.
* **The counter.** A rejection sends the feature back to `executing` so its manager can
  file remediation work orders, and it comes back for round N+1 against the SAME budget.
  Nothing else in the suite stops that loop running for ever.
* **The addressee.** A feature order has no session, so the rejection is posted to the
  ROLE `manager` and the router finds the work order that fills it. The manager reads
  prose; it never learns that a panel of seats exists.
* **The switches.** `os.validation.enabled` and `os.validation.feature_units` gate
  OPENING a round and never settling one, so either can be turned off mid-flight without
  stranding a feature — or its manager, waiting for a message that would never come.

THE VALIDATOR IS INJECTED, and every test asserts on STORED ROWS AND CALL COUNTS. The
feature ships disabled: a test that reaches this code without enabling it exercises the
old path and still gets a perfectly good ending, so "an outcome came back" proves nothing
about which machine produced it. Tests are PAIRED wherever a passing assertion could also
be explained by the machine never having run.
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
from jarvis.daemon import Daemon
from jarvis.invariants import VALIDATION_STUCK_BLOCKER
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC, make_git_project

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")

SEATS = ("tester", "security")


# -- the fixture: a real repository, a released plan, a fake panel -------------------


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
    and answers with the queued outcomes in order, repeating the last one for ever after.
    An outcome that is an exception is raised instead of returned, which is how a
    transport outage is staged.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or [passed()]
        self.calls: list[dict] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()  # not blocking unless a test says so

    def __call__(self, store, round_row, packet):
        self.calls.append({"round": round_row["round"], "packet": packet,
                           "fingerprint": round_row["fingerprint"]})
        self.entered.set()
        assert self.release.wait(timeout=15), "a blocked validator was never released"
        outcome = (self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def block(self) -> None:
        self.release.clear()
        self.entered.clear()

    @property
    def subjects(self) -> list[str]:
        return [c["packet"].subject_id for c in self.calls]


def passed(reason: str = "") -> dict:
    return {"outcome": "passed", "reason": reason, "seats": _seats("pass")}


def rejected(reason: str = "the exporter and its caller disagree about the header row"
             ) -> dict:
    return {"outcome": "rejected", "reason": reason, "seats": _seats("reject")}


def _seats(verdict: str) -> list[dict]:
    """What a panel hands back per seat — named, so the opinions are real rows."""
    return [{"seat": seat, "status": "ok", "verdict": verdict, "model": "sonnet",
             "latency_ms": 12,
             "reply": f"the {seat} seat says: {verdict} — the two halves disagree"}
            for seat in SEATS]


def a_child(key: str) -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} half of the exporter: add the module, wire it into the "
            f"command that calls it, and cover both paths with tests in the existing "
            f"suite. Do not change the public interface of the caller."
        ),
        "needs": [],
    }


def a_plan(*keys: str) -> dict:
    return {"summary": "an exporter", "design_doc": FIXTURE_DESIGN_DOC,
            "children": [a_child(k) for k in keys]}


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


class Fleet:
    """One booted OS over a real git repository, plus the plumbing a feature needs."""

    def __init__(self, tmp_path, project, catalog_path):
        self.tmp_path = tmp_path
        self.project = project
        self.catalog_path = catalog_path
        self.daemon = Daemon(load_catalog(catalog_path))

    # -- configuration ---------------------------------------------------------
    def reconfigure(self, **validation) -> None:
        """Rewrite the catalog on disk AND reload the daemon's copy.

        Both halves matter: `ops.submit_feature` and `ops.review_plan` read the catalog
        from disk (they run in the manager's process, not the daemon's), and the round
        machine reads the daemon's own loaded copy.
        """
        write_catalog(self.tmp_path, self.project, **validation)
        self.daemon.catalog = load_catalog(self.catalog_path)

    @property
    def spec(self):
        return self.daemon.catalog.projects[0]

    def store(self) -> ProjectStore:
        return ProjectStore(self.project)

    # -- features --------------------------------------------------------------
    def release(self, title: str, *keys: str) -> str:
        """A feature order whose plan has been submitted and released. Returns its id.

        The tick opens the planner and moves the feature to `planning`; the plan is then
        submitted by hand rather than by that planner, because what these tests are about
        is what happens AFTER release.
        """
        fo = ops.create_feature_order("proj_a", title, description=ASK)
        self.daemon.tick()
        ops.submit_plan(fo["id"], a_plan(*keys))
        ops.review_plan(fo["id"], accept=True, decided_by="user")
        return str(fo["id"])

    def merge(self, name: str, text: str) -> str:
        """A change landing on the default branch — a child's pull request, merged.

        Committed in the PROJECT ROOT rather than in a worktree, because that is exactly
        what the feature's diff reads: `base_sha...<default branch head>`.
        """
        (self.project / name).write_text(text)
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-qm", f"merge {name}")
        return _git(self.project, "rev-parse", "HEAD").strip()

    def land_children(self, fo_id: str, store: ProjectStore) -> None:
        """Every child of this feature completed, with a result the packet can carry."""
        for child in store.feature_children(fo_id):
            store.update_work_order(child["id"],
                                    result_summary=f"built {child['title']}")
            store.set_status(child["id"], "completed")

    def file_child(self, fo_id: str, store: ProjectStore, title: str) -> dict:
        """What a manager does with review feedback: one work order under the feature."""
        return ops.create_work_order("proj_a", title, description="the review asked",
                                     parent_id=fo_id)

    # -- the daemon ------------------------------------------------------------
    def tick(self) -> None:
        self.daemon.tick_count = 0
        self.daemon.tick()

    def drain(self, ticks: int = 2, timeout: float = 15.0) -> None:
        """Tick, then wait for whatever it started off-thread to finish.

        Twice by default: `settle_features` routes a finished feature to `validating`
        AFTER `feature_validation_tick` has already run on that tick, so the first tick
        opens the round and the second one judges it.
        """
        for _ in range(ticks):
            self.tick()
            deadline = time.monotonic() + timeout
            while self.daemon.validating and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not self.daemon.validating, "a validation round never finished"


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, claude_json):
    """A booted OS over a REAL git repository, with feature validation enabled.

    The default branch is named explicitly: the base ladder ends at `main`, and a repo
    whose `git init` happened to name the branch something else would record no
    `base_sha`, collect an empty diff, and make every escalation test pass for the wrong
    reason.
    """
    project = make_git_project(tmp_path, "proj_a")
    _git(project, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    claude_json(project)
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    return Fleet(tmp_path, project, catalog_path)


def envelopes(store: ProjectStore) -> list[dict]:
    return [dict(r) for r in
            store.conn.execute("SELECT * FROM envelopes ORDER BY id").fetchall()]


def messages(store: ProjectStore, wo_id: str) -> list[dict]:
    """What this work order has been SENT. `user_to_agent` only: the manager's own turns
    land in the same table in the other direction, and counting those would make "it
    received one message" true of a session that received none."""
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM wo_messages WHERE wo_id=? AND direction='user_to_agent' "
        "ORDER BY id", (wo_id,)).fetchall()]


# -- 1. the switches ------------------------------------------------------------------


def test_with_validation_off_a_finished_feature_completes_exactly_as_today(fleet):
    """THE BYTE-IDENTICAL PIN. Everything else in this file is a behaviour change, and
    this is the assertion that it is opt-in: no round, no envelope, no manager, and the
    feature settles on its children the moment they land."""
    fleet.reconfigure(enabled=False)
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one", "two")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.tick()

        assert store.get_feature_order(fo_id)["status"] == "completed"
        assert store.validation_rounds(fo_id=fo_id) == []
        assert envelopes(store) == []
        assert store.manager_work_order(fo_id) is None
        assert fleet.daemon.validator.calls == []
    finally:
        store.close()


def test_with_validation_on_the_same_feature_waits_for_a_pass(fleet):
    """The pair to the test above: same plan, same children, same merges — and it stops
    in `validating` until something judges it."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one", "two")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.daemon.settle_features(fleet.spec, store)

        assert store.get_feature_order(fo_id)["status"] == "validating"
        rounds = store.validation_rounds(fo_id=fo_id)
        assert [(r["round"], r["outcome"]) for r in rounds] == [(1, "pending")]
        assert rounds[0]["fingerprint"], "a round with no fingerprint proves nothing"
        assert validator.calls == [], "the tick thread judged the round itself"

        fleet.drain()

        assert store.get_feature_order(fo_id)["status"] == "completed"
        assert store.latest_validation_round(fo_id=fo_id)["outcome"] == "passed"
        assert len(validator.calls) == 1
        # A pass settles the feature, and settling it closes the manager: left open it
        # would be a live addressee for messages about work that is over.
        assert store.get_work_order(
            store.manager_work_order(fo_id)["id"])["status"] == "completed"
    finally:
        store.close()


def _config_version(fleet, **validation) -> str:
    """One row in the config ledger: this fleet's catalog with `os.validation` changed,
    stored the way the console will store it. The catalog ON DISK is left alone, which is
    what makes "judged under the stamp" distinguishable from "judged under the live
    catalog"."""
    from jarvis import catalog as catalog_mod, config_version as cv
    from jarvis.central_store import CentralStore

    document = json.loads(fleet.catalog_path.read_text())
    document["os"]["validation"] = {**document["os"].get("validation", {}), **validation}
    central = CentralStore()
    try:
        return central.add_config_version(
            document, cv.resolve(catalog_mod.parse_catalog(document)),
            actor="user")["id"]
    finally:
        central.close()


def test_a_feature_round_is_stamped_with_the_configuration_it_was_opened_under(fleet):
    """No column on `feature_orders`: a feature's rounds live in the same polymorphic
    table as a work order's, so the round stamp already covers both (config-console
    design §5)."""
    version = _config_version(fleet)
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.daemon.settle_features(fleet.spec, store)

        assert [r["config_version"] for r in store.validation_rounds(fo_id=fo_id)] == [
            version]
    finally:
        store.close()


def test_a_feature_round_is_judged_under_its_stamp_and_not_the_live_catalog(fleet):
    """The pair, and the reason the stamp is written at all (Neo, question 176): the
    settle side must follow the version the round was OPENED under, or a catalog edit
    mid-flight decides a round nobody judged under those terms.

    Stamped `max_rounds=1`, live catalog 3: one rejection is the last round, so the
    feature gives up instead of going back to its manager. Reading the live catalog
    produces an ordinary rejection here, which is what makes the two distinguishable.
    """
    _config_version(fleet, max_rounds=1)
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain()

        assert store.latest_validation_round(fo_id=fo_id)["outcome"] == "escalated"
        assert store.get_feature_order(fo_id)["needs_attention"] == 1
        assert envelopes(store) == [], "a last round must not ask for another attempt"
    finally:
        store.close()


def test_feature_units_off_validates_work_orders_and_not_features(fleet):
    """THE PAIRING IS THE TEST. With `feature_units` false and `enabled` true the switch
    is indistinguishable from validation being off unless a WORK order is seen to
    validate on the same catalog, in the same test."""
    fleet.reconfigure(feature_units=False)
    validator = Validator(passed())
    validator.block()  # nothing must settle behind our backs
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        loose = ops.create_work_order("proj_a", "an ordinary work order",
                                      description="unrelated to the feature")
        assert ops.finish(loose["id"], "done", evidence="ran the suite")["status"] \
            == "validating", "the work-order loop went off with feature_units false"

        fleet.daemon.settle_features(fleet.spec, store)

        assert store.get_feature_order(fo_id)["status"] == "completed"
        assert store.validation_rounds(fo_id=fo_id) == []
        assert store.latest_validation_round(wo_id=loose["id"])["round"] == 1
    finally:
        validator.release.set()
        store.close()


# -- 2. the base and the integrated diff ----------------------------------------------


def test_the_base_is_recorded_at_release_and_bounds_the_diff(fleet):
    """`base_sha` is written when the plan is released, and everything between it and the
    default branch now IS the feature.

    Both halves asserted, because either alone is satisfied by a broken base: a commit
    made BEFORE release must not appear, and one made after must."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        before = fleet.merge("before.py", "# already on main when the plan was released\n")

        fo_id = fleet.release("CSV export", "one", "two")
        assert store.get_feature_order(fo_id)["base_sha"] == before, (
            "the feature's base is not where the default branch was at release")

        fleet.merge("after.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()

        packet = validator.calls[0]["packet"]
        assert packet.unit == "feature"
        assert packet.subject_id == fo_id
        assert packet.base == before
        assert "after.py" in packet.files, "a merged child's change is missing"
        assert "before.py" not in packet.files, (
            "the diff reaches back past the feature's own base")
        assert "def export" in packet.diff
        # Each child's own account rides along: the integration question is asked against
        # what the children claimed, one level down.
        assert [c["title"] for c in packet.children] == ["Build one", "Build two"]
        assert all(c["summary"] for c in packet.children)
    finally:
        store.close()


def test_a_feature_with_no_base_escalates_without_calling_the_panel(fleet):
    """A guessed base produces a confidently wrong diff, so a feature order that predates
    the column gets none. PAIRED with a feature that has one, on the same tick and the
    same validator, so "zero calls" cannot mean "the machine never ran"."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        blind = fleet.release("no base", "one")
        seeing = fleet.release("with a base", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(blind, store)
        fleet.land_children(seeing, store)
        store.update_feature_order(blind, base_sha=None)

        fleet.drain()

        assert validator.subjects == [seeing], "the panel was handed a baseless feature"
        blind_round = store.latest_validation_round(fo_id=blind)
        assert blind_round["outcome"] == "escalated"
        assert "no recorded base commit" in blind_round["reason"]
        assert store.get_feature_order(blind)["needs_attention"] == 1
        assert store.get_feature_order(seeing)["status"] == "completed"
    finally:
        store.close()


def test_a_feature_whose_children_merged_nothing_escalates(fleet):
    """An empty diff never reaches the panel: a reviewer handed nothing to review will
    approve it, and that one silent pass would make the whole feature theatre."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.land_children(fo_id, store)  # nothing merged on main

        fleet.drain()

        assert validator.calls == []
        rnd = store.latest_validation_round(fo_id=fo_id)
        assert rnd["outcome"] == "escalated"
        assert "nothing to review" in rnd["reason"]
        assert store.get_feature_order(fo_id)["status"] == "validating"
    finally:
        store.close()


# -- 3. the rejection, and who reads it -----------------------------------------------


def test_a_rejection_reaches_the_manager_as_prose_and_names_no_seat(fleet):
    """THE PAIRING IS THE TEST, and both halves are the design's first principle.

    The manager is told what has to change and never learns that a panel exists — while
    the round it came from records every seat by name. Asserting only the first half
    passes just as well if the seats were never recorded; asserting only the second
    passes if the message quoted them verbatim.
    """
    validator = Validator(rejected())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one", "two")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain()

        manager = store.manager_work_order(fo_id)
        rnd = store.latest_validation_round(fo_id=fo_id)
        assert rnd["outcome"] == "rejected"
        # Back to `executing`, so the manager's remediation work orders can be claimed.
        assert store.get_feature_order(fo_id)["status"] == "executing"
        assert store.get_feature_order(fo_id)["needs_attention"] == 0

        env = envelopes(store)
        assert len(env) == 1
        assert env[0]["to_role"] == "manager"
        assert env[0]["from_role"] == "reviewer"
        assert env[0]["kind"] == "review_feedback"
        assert env[0]["subject_fo_id"] == fo_id
        assert env[0]["subject_wo_id"] is None

        fleet.tick()  # the bus's own tick turns the envelope into a message

        assert envelopes(store)[0]["state"] == "delivered"
        got = messages(store, manager["id"])
        assert len(got) == 1
        text = got[0]["content"]
        assert "header row" in text, "the reviewer's actual ask did not travel"
        assert f"jarvis fo submit {fo_id}" in text, "the manager was not told how to reply"
        for seat in SEATS:
            assert seat not in text, f"the manager was told the {seat} seat exists"

        opinions = store.validation_opinions(rnd["id"])
        assert sorted(o["seat"] for o in opinions) == sorted(SEATS)
        assert all(o["verdict"] == "reject" for o in opinions)
    finally:
        store.close()


def test_a_rejected_feature_is_not_resubmitted_by_the_reconciler(fleet):
    """`settle_features` opens round 1 and only round 1.

    Its condition is "every child is completed", and after a rejection that is true again
    the instant the children are looked at — so a reconciler that resubmitted would open
    round 2 with the identical fingerprint on the very next tick, escalate on the repeat,
    and cut the manager out of its own loop. Resubmission is the manager's act.
    """
    validator = Validator(rejected())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        assert store.get_feature_order(fo_id)["status"] == "executing"

        fleet.drain(ticks=3)

        assert len(store.validation_rounds(fo_id=fo_id)) == 1
        assert len(validator.calls) == 1
        assert store.get_feature_order(fo_id)["status"] == "executing"
    finally:
        store.close()


# -- 4. the counter, and why the loop terminates --------------------------------------


def test_the_round_counter_does_not_reset_when_the_feature_goes_back_to_executing(fleet):
    """THE NON-TERMINATION GUARD, and nothing else tests it.

    Rejection sends the feature back to `executing`; its manager files a work order, that
    lands, and the feature is submitted again. That is round N+1 against the SAME budget.
    A counter reset anywhere on that path — on the status change, on the new child, on the
    resubmission — is a manager that can file work orders for ever.
    """
    fleet.reconfigure(max_rounds=3)
    validator = Validator(rejected())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        assert store.counted_validation_rounds(fo_id=fo_id) == 1

        for n in (2, 3):
            # What a manager does with feedback: file the work, let it land, resubmit.
            child = fleet.file_child(fo_id, store, f"fix round {n - 1}")
            fleet.merge(f"fix{n}.py", f"# remediation for round {n - 1}\n")
            store.update_work_order(child["id"], result_summary=f"fixed {n - 1}")
            store.set_status(child["id"], "completed")
            assert store.get_feature_order(fo_id)["status"] == "executing"
            assert ops.submit_feature(fo_id, f"round {n}",
                                      evidence=f"re-ran the suite for round {n}"
                                      )["round"] == n
            fleet.drain()

        rounds = store.validation_rounds(fo_id=fo_id)
        assert [(r["round"], r["outcome"]) for r in rounds] == [
            (1, "rejected"), (2, "rejected"), (3, "escalated")]
        assert len(validator.calls) == 3, "the panel was called past the budget"
        fo = store.get_feature_order(fo_id)
        assert fo["needs_attention"] == 1
        assert fo["attention_reason"] == VALIDATION_STUCK_BLOCKER
        # The last round gave up, so no envelope was posted for it: asking for a fourth
        # submission there is no round to judge is worse than saying nothing.
        assert len(envelopes(store)) == 2
    finally:
        store.close()


def test_a_repeated_fingerprint_escalates_without_calling_the_panel(fleet):
    """A submission identical to the one before it is not new evidence. PAIRED with a
    resubmission that genuinely changed the diff, so "the panel was not called" cannot
    mean "the panel was never wired in"; `max_rounds` is set high enough that neither
    escalation can be the budget running out."""
    fleet.reconfigure(max_rounds=9)
    validator = Validator(rejected())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        stuck = fleet.release("goes in circles", "one")
        moving = fleet.release("actually changes", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(stuck, store)
        fleet.land_children(moving, store)
        fleet.drain()
        assert len(validator.calls) == 2

        for fo_id in (stuck, moving):
            assert ops.submit_feature(fo_id, "round 2", evidence="ran the suite")[
                "round"] == 2
        fleet.drain()
        assert len(validator.calls) == 4

        # The one that changed nothing resubmits the same words over the same tree.
        assert ops.submit_feature(stuck, "round 3", evidence="ran the suite")["round"] == 3
        fleet.merge("fix.py", "# what the review actually asked for\n")
        assert ops.submit_feature(moving, "round 3", evidence="ran the suite")["round"] == 3
        fleet.drain()

        assert validator.subjects[-1:] == [moving], (
            "the panel was asked to judge a submission it had already seen")
        assert len(validator.calls) == 5
        assert store.latest_validation_round(fo_id=stuck)["outcome"] == "escalated"
        assert "identical to round 2" in \
            store.latest_validation_round(fo_id=stuck)["reason"]
        assert store.latest_validation_round(fo_id=moving)["outcome"] == "rejected"
    finally:
        store.close()


# -- 5. what the user sees ------------------------------------------------------------


def test_validating_raises_no_attention_and_an_escalated_feature_does(fleet):
    """`validating` is the system working, like `waiting_pr_merge`. Only the give-up
    transition flags anyone — and both features below are in the same status, so the flag
    is the only difference between them."""
    validator = Validator(passed())
    validator.block()
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        quiet = fleet.release("under review", "one")
        stuck = fleet.release("gave up", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(quiet, store)
        fleet.land_children(stuck, store)
        store.update_feature_order(stuck, base_sha=None)  # escalates without the panel

        fleet.tick()   # both routed to `validating`
        fleet.tick()   # both picked up; `quiet` blocks inside the validator
        assert validator.entered.wait(timeout=15)
        deadline = time.monotonic() + 15
        while store.get_feature_order(stuck)["needs_attention"] == 0 \
                and time.monotonic() < deadline:
            time.sleep(0.01)

        assert store.get_feature_order(quiet)["status"] == "validating"
        assert store.get_feature_order(quiet)["needs_attention"] == 0
        assert store.get_feature_order(stuck)["status"] == "validating"
        assert store.get_feature_order(stuck)["needs_attention"] == 1
        assert store.get_feature_order(stuck)["attention_reason"] \
            == VALIDATION_STUCK_BLOCKER
    finally:
        validator.release.set()
        store.close()


def test_jarvis_status_shows_a_feature_the_review_gave_up_on(fleet):
    """The flag is only worth raising if it reaches the one surface the user reads."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.land_children(fo_id, store)  # nothing merged: escalates on the empty diff
        fleet.drain()
        assert store.get_feature_order(fo_id)["needs_attention"] == 1
    finally:
        store.close()

    items = [a for a in ops.os_status()["attention"] if a.get("fo_id") == fo_id]
    assert len(items) == 1
    assert VALIDATION_STUCK_BLOCKER in items[0]["reason"]
    assert items[0]["status"] == "feature:validating"
    assert items[0]["decide"] == f"jarvis fo show {fo_id}"


# -- 6. what must not change ----------------------------------------------------------


def test_a_failed_child_still_fails_the_feature_with_a_manager_open(fleet):
    """`settle_features` still examines only `executing` features for the failure path —
    that is what makes "flag once" true by construction — and an open manager must not be
    mistaken for an unfinished child. No round is opened either: a feature with a dead
    child has nothing to integrate."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one", "two")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        children = store.feature_children(fo_id)
        store.set_status(children[0]["id"], "failed")
        store.set_status(children[1]["id"], "completed")
        manager = store.manager_work_order(fo_id)
        assert manager["status"] not in ("completed", "cancelled")

        fleet.daemon.settle_features(fleet.spec, store)

        fo = store.get_feature_order(fo_id)
        assert fo["status"] == "failed"
        assert fo["needs_attention"] == 1
        assert children[0]["id"] in fo["attention_reason"]
        assert store.validation_rounds(fo_id=fo_id) == []
        assert validator.calls == []
        assert store.get_work_order(manager["id"])["status"] == "completed"

        # Flag once, by construction: the feature has left `executing`, so the next tick
        # cannot see it and cannot raise the flag again.
        store.clear_feature_attention(fo_id)
        fleet.daemon.settle_features(fleet.spec, store)
        assert store.get_feature_order(fo_id)["needs_attention"] == 0
    finally:
        store.close()


def test_fo_submit_refuses_a_feature_that_is_not_executing(fleet):
    """Submitting a feature that is already under review asks for a second opinion on a
    round in flight; submitting a settled one has misread the inbox. Both are refused with
    what the feature is actually doing."""
    validator = Validator(passed())
    validator.block()
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.daemon.settle_features(fleet.spec, store)
        assert store.get_feature_order(fo_id)["status"] == "validating"

        with pytest.raises(ops.OpsError, match="is validating, not executing"):
            ops.submit_feature(fo_id, "ready again", evidence="ran the suite")
        assert len(store.validation_rounds(fo_id=fo_id)) == 1

        validator.release.set()
        fleet.drain()
        assert store.get_feature_order(fo_id)["status"] == "completed"
        with pytest.raises(ops.OpsError, match="is completed, not executing"):
            ops.submit_feature(fo_id, "ready again", evidence="ran the suite")
    finally:
        validator.release.set()
        store.close()


# -- 7. the kill switch ---------------------------------------------------------------


def test_the_kill_switch_drains_an_open_feature_round_instead_of_stranding_it(fleet):
    """THE FLAG IS A SAFE STOP, NOT A TRAPDOOR — and for a feature it strands two things,
    not one: the feature in `validating`, and its manager waiting for a message nobody
    would ever send. Both halves asserted, plus the pair that shows a fresh feature under
    the disabled flag taking today's path immediately."""
    held = Validator(passed())
    held.block()
    fleet.daemon.validator = held
    store = fleet.store()
    try:
        inside = fleet.release("already inside", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(inside, store)
        fleet.daemon.settle_features(fleet.spec, store)
        assert store.get_feature_order(inside)["status"] == "validating"

        fleet.reconfigure(enabled=False)

        after = fleet.release("after the switch", "one")
        fleet.merge("later.py", "# landed after the flag went off\n")
        fleet.land_children(after, store)
        fleet.daemon.settle_features(fleet.spec, store)
        assert store.get_feature_order(after)["status"] == "completed", (
            "a feature finishing under the disabled flag did not take today's path")
        assert store.validation_rounds(fo_id=after) == []

        held.release.set()
        fleet.drain()

        assert store.get_feature_order(inside)["status"] == "completed", (
            "turning the feature off stranded a feature already in validating")
        assert store.latest_validation_round(fo_id=inside)["outcome"] == "passed"
        assert len(held.calls) == 1, "the daemon refused to judge an open round"
        assert store.get_work_order(
            store.manager_work_order(inside)["id"])["status"] == "completed", (
            "the manager was left waiting for a message that will never come")
    finally:
        held.release.set()
        store.close()


def test_with_no_validator_wired_an_open_feature_round_settles_unjudged(fleet,
                                                                        monkeypatch):
    """A round with nothing to judge it settles its unit where the OS settles it with
    validation switched off — never `passed`, because a round nobody judged must not read
    as a verdict on any surface."""
    monkeypatch.setattr(Daemon, "_validator", staticmethod(lambda cfg: None))
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain()

        assert store.get_feature_order(fo_id)["status"] == "completed"
        rnd = store.latest_validation_round(fo_id=fo_id)
        assert rnd["outcome"] == "failed"
        assert "never judged" in rnd["reason"]
        assert store.get_work_order(
            store.manager_work_order(fo_id)["id"])["status"] == "completed"
    finally:
        store.close()


def test_a_transport_outage_costs_the_feature_no_round(fleet):
    """An outage is not a verdict. The round is retried against the same number, and the
    budget it does spend is the outage budget — three, counted from the manager's
    timeline so a daemon restart cannot hand it a fresh one."""
    fleet.reconfigure(max_rounds=1)  # so a consumed round would show up immediately
    validator = Validator(claude_cli.ClaudeCliError("no seat answered"),
                          claude_cli.ClaudeCliError("no seat answered"),
                          passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain(ticks=4)

        assert len(store.validation_rounds(fo_id=fo_id)) == 1, "an outage opened a round"
        assert store.latest_validation_round(fo_id=fo_id)["outcome"] == "passed"
        assert store.get_feature_order(fo_id)["status"] == "completed"
        assert len(validator.calls) == 3
        assert {c["round"] for c in validator.calls} == {1}
    finally:
        store.close()


def test_a_feature_with_no_manager_escalates_without_calling_the_panel(fleet):
    """A feature whose plan was released while validation was off has no manager, so a
    rejection would have no addressee and the round's own events would have no timeline
    to live on. It escalates before the panel is called — Neo's ruling on question 153 —
    and the pair is a feature that does have one, on the same tick."""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fleet.reconfigure(enabled=False)
        orphan = fleet.release("released before the flag", "one")
        assert store.manager_work_order(orphan) is None
        fleet.reconfigure(enabled=True)
        owned = fleet.release("released after it", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(orphan, store)
        fleet.land_children(owned, store)

        fleet.drain()

        assert validator.subjects == [owned]
        rnd = store.latest_validation_round(fo_id=orphan)
        assert rnd["outcome"] == "escalated"
        assert "no project manager" in rnd["reason"]
        assert store.get_feature_order(orphan)["needs_attention"] == 1
        assert store.get_feature_order(owned)["status"] == "completed"
    finally:
        store.close()
