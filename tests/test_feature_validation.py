"""The feature round machine: the integrated diff, the rounds, and the manager's loop"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from jarvis import branchproof, claude_cli, evidence, ops, validation
from jarvis.catalog import load_catalog
from jarvis.daemon import (FEATURE_HANDOFF_EVENT, FEATURE_MANAGER_SILENT,
                           FEATURE_MANAGER_STALLED,
                           VALIDATION_ESCALATED_TITLE, Daemon)
from jarvis.invariants import VALIDATION_STUCK_BLOCKER
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC, fixture_spec_section, make_git_project

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")

SEATS = ("tester", "security")


# -- the fixture: a real repository, a released plan, a fake panel


def _git(cwd: Path, *args: str) -> str:
    """git with a pinned identity and no user or system config in sight"""
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


class Validator:
    """The injected seam, driven by the test"""

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
    """What a panel hands back per seat — named, so the opinions are real rows"""
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
        "spec_section": fixture_spec_section(key),
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
    """One booted OS over a real git repository, plus the plumbing a feature needs"""

    def __init__(self, tmp_path, project, catalog_path):
        self.tmp_path = tmp_path
        self.project = project
        self.catalog_path = catalog_path
        self.daemon = Daemon(load_catalog(catalog_path))

    # -- configuration
    def reconfigure(self, **validation) -> None:
        """Rewrite the catalog on disk AND reload the daemon's copy"""
        write_catalog(self.tmp_path, self.project, **validation)
        self.daemon.catalog = load_catalog(self.catalog_path)

    @property
    def spec(self):
        return self.daemon.catalog.projects[0]

    def store(self) -> ProjectStore:
        return ProjectStore(self.project)

    # -- features
    def release(self, title: str, *keys: str) -> str:
        """A feature order whose plan has been submitted and released"""
        fo = ops.create_feature_order("proj_a", title, description=ASK)
        self.daemon.tick()
        ops.submit_plan(fo["id"], a_plan(*keys))
        ops.review_plan(fo["id"], accept=True, decided_by="user")
        return str(fo["id"])

    def merge(self, name: str, text: str) -> str:
        """A change landing on the default branch — a child's pull request, merged"""
        (self.project / name).write_text(text)
        _git(self.project, "add", "-A")
        _git(self.project, "commit", "-qm", f"merge {name}")
        return _git(self.project, "rev-parse", "HEAD").strip()

    def land_children(self, fo_id: str, store: ProjectStore) -> None:
        """Every child of this feature completed, with a result the packet can carry"""
        for child in store.feature_children(fo_id):
            store.update_work_order(child["id"],
                                    result_summary=f"built {child['title']}")
            store.set_status(child["id"], "completed")

    def file_child(self, fo_id: str, store: ProjectStore, title: str) -> dict:
        """What a manager does with review feedback: one work order under the feature"""
        return ops.create_work_order("proj_a", title, description="the review asked",
                                     parent_id=fo_id)

    # -- the daemon
    def tick(self) -> None:
        self.daemon.tick_count = 0
        self.daemon.tick()

    def drain(self, ticks: int = 2, timeout: float = 15.0) -> None:
        """Tick, then wait for whatever it started off-thread to finish"""
        for _ in range(ticks):
            self.tick()
            deadline = time.monotonic() + timeout
            while self.daemon.validating and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not self.daemon.validating, "a validation round never finished"


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, claude_json):
    """A booted OS over a REAL git repository, with feature validation enabled"""
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
    """What this work order has been SENT"""
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM wo_messages WHERE wo_id=? AND direction='user_to_agent' "
        "ORDER BY id", (wo_id,)).fetchall()]


# -- 1. the switches


def test_with_validation_off_a_finished_feature_completes_exactly_as_today(fleet):
    """THE BYTE-IDENTICAL PIN"""
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
    """The pair to the test above: same plan, same children, same merges"""
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
        # A pass settles the feature, and settling it closes the manager: left open it would be a live...
        assert store.get_work_order(
            store.manager_work_order(fo_id)["id"])["status"] == "completed"
    finally:
        store.close()


def _config_version(fleet, **validation) -> str:
    """One row in the config ledger: this fleet's catalog with `os.validation` changed"""
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
    """No column on `feature_orders`: a feature's rounds live in the same polymorphic table as a work..."""
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
    """The pair, and the reason the stamp is written at all (Neo"""
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
    """THE PAIRING IS THE TEST"""
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


# -- 2. the base and the integrated diff


def test_the_base_is_recorded_at_release_and_bounds_the_diff(fleet):
    """`base_sha` is written when the plan is released"""
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
        # Each child's own account rides along: the integration question is asked against what the children...
        assert [c["title"] for c in packet.children] == ["Build one", "Build two"]
        assert all(c["summary"] for c in packet.children)
    finally:
        store.close()


def test_a_feature_with_no_base_escalates_without_calling_the_panel(fleet):
    """A guessed base produces a confidently wrong diff"""
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


def test_a_side_effect_does_not_excuse_a_feature_from_having_a_base(fleet):
    """The two guards answer different questions and only one of them yields"""
    from jarvis import ops

    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("no base", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        for child in store.feature_children(fo_id):
            ops.learn_add("something durable", wo_id=str(child["id"]))
        store.update_feature_order(fo_id, base_sha=None)

        # ASSERT THE SIDE EFFECTS ACTUALLY REACH A FEATURE PACKET FIRST
        packet = ops.collect_feature_evidence(
            store, fleet.project, store.get_feature_order(fo_id),
            declared="", summary="", cfg=fleet.daemon.catalog.os.validation)
        assert packet.side_effects, "a feature packet never carries side effects"
        assert not packet.base, "the fixture did not actually remove the base"

        fleet.drain()

        assert validator.calls == [], "a baseless feature reached the panel"
        rnd = store.latest_validation_round(fo_id=fo_id)
        assert rnd["outcome"] == "escalated"
        assert "no recorded base commit" in rnd["reason"]
    finally:
        store.close()


def test_a_features_side_effects_are_the_union_of_its_childrens(fleet):
    """The wiring the guard above depends on"""
    from jarvis import ops

    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        children = [str(c["id"]) for c in store.feature_children(fo_id)]
        assert children, "the fixture produced no children to aggregate"
        for i, child in enumerate(children):
            ops.learn_add(f"child {i} learned something", wo_id=child)

        packet = ops.collect_feature_evidence(
            store, fleet.project, store.get_feature_order(fo_id),
            declared="", summary="", cfg=fleet.daemon.catalog.os.validation)

        assert {e["wo_id"] for e in packet.side_effects} == set(children)
        assert packet.side_effects_sha, "the digest is empty with side effects present"
    finally:
        store.close()


def test_a_feature_whose_children_merged_nothing_escalates(fleet):
    """An empty diff never reaches the panel: a reviewer handed nothing to review will approve it"""
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


def test_a_feature_whose_children_only_shipped_a_RELEASE_is_voided(
        fleet, tmp_path, monkeypatch):
    """The same empty packet, through the same shared helper, ending the other way (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §8.)"""
    validator = Validator(passed())
    fleet.daemon.validator = validator

    prod = tmp_path / "production" / "jarvis_os"
    prod.mkdir(parents=True)
    monkeypatch.setenv("PRODUCTION_CODE", str(tmp_path / "production"))

    store = fleet.store()
    try:
        fo_id = fleet.release("ship 0.11.0", "one")
        fleet.land_children(fo_id, store)  # a release merges nothing to the branch
        for child in store.feature_children(fo_id):
            # The approved-and-used gate the attestation leans on, granted BEFORE the tag is cut
            row = store.add_approval(str(child["id"]), "release", "ship 0.11.0",
                                     matched="shipit")
            store.decide_approval(int(row["id"]), "approved", "ready to ship", "neo")
            store.consume_grant(int(row["id"]))
            store.add_event(str(child["id"]), "release_verified",
                            {"version": "0.11.0", "tag": "jarvis-0.11.0"})

        _git(prod, "init", "-q")
        (prod / "pyproject.toml").write_text(
            '[project]\nname = "jarvis-os"\nversion = "0.11.0"\n')
        _git(prod, "add", "-A")
        _git(prod, "commit", "-qm", "Release jarvis-0.11.0")
        _git(prod, "tag", "-a", "jarvis-0.11.0", "-m", "Jarvis OS 0.11.0")
        _git(prod, "checkout", "-q", "jarvis-0.11.0")

        fleet.drain()

        assert validator.calls == [], "a reviewer was asked to judge a release"
        rnd = store.latest_validation_round(fo_id=fo_id)
        assert rnd["outcome"] == "void"
        assert "jarvis-0.11.0" in rnd["reason"]
        after = store.get_feature_order(fo_id)
        assert after["status"] == "completed"
        assert after["needs_attention"] == 0
    finally:
        store.close()


# -- 3. the rejection, and who reads it


def test_a_rejection_reaches_the_manager_as_prose_and_names_no_seat(fleet):
    """THE PAIRING IS THE TEST, and both halves are the design's first principle"""
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
        # Back to `executing`, so the manager's remediation work orders can be claimed
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
    """`settle_features` opens round 1 and only round 1"""
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


# -- 3b. and who tells the manager its answer landed (issue #480)


def _until_idle(fleet, store, fo_id, limit: int = 6) -> dict:
    """Tick until the manager has read whatever it was sent and gone idle again"""
    for _ in range(limit):
        fleet.tick()
        manager = store.manager_work_order(fo_id)
        if manager["status"] == "idle" and not store.queued_messages(manager["id"]):
            return manager
    raise AssertionError("the manager never went idle")


def _remediate(fleet, store, fo_id, title: str) -> dict:
    """What a manager does with a rejection, in the order it really happens"""
    child = fleet.file_child(fo_id, store, title)
    _until_idle(fleet, store, fo_id)
    fleet.merge(f"{title.replace(' ', '_')}.py", f"# {title}\n")
    store.update_work_order(child["id"], result_summary=f"did {title}")
    store.set_status(child["id"], "completed")
    return child


def _handoffs(store, fo_id) -> list[tuple]:
    return [(json.loads(e["payload"])["round"], json.loads(e["payload"])["action"])
            for e in ops.feature_events_of_kind(store, fo_id, FEATURE_HANDOFF_EVENT)]


def test_a_manager_is_told_when_the_children_it_filed_after_a_rejection_land(fleet):
    """THE BUG (#480)"""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        manager = store.manager_work_order(fo_id)
        child = _remediate(fleet, store, fo_id, "fix the header row")
        sent_before = len(messages(store, manager["id"]))

        fleet.tick()

        posted = [e for e in envelopes(store) if e["kind"] == "children_landed"]
        assert len(posted) == 1, "nothing told the manager its work order had landed"
        assert posted[0]["to_role"] == "manager"
        assert posted[0]["subject_fo_id"] == fo_id
        assert _handoffs(store, fo_id) == [(1, "nudged")]

        fleet.tick()  # delivery
        text = messages(store, manager["id"])[-1]["content"]
        assert len(messages(store, manager["id"])) == sent_before + 1
        assert child["id"] in text
        assert f"jarvis fo submit {fo_id}" in text
        assert store.get_feature_order(fo_id)["needs_attention"] == 0, (
            "a feature the OS just unstuck itself must not also cost the user attention")
    finally:
        store.close()


def test_the_handoff_fires_once_per_round_however_many_ticks_run(fleet):
    """A RECONCILER FIX IS NOT DONE AT ONE TICK (kn-089de524)"""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        _remediate(fleet, store, fo_id, "fix the header row")

        for _ in range(6):
            fleet.tick()

        assert len([e for e in envelopes(store) if e["kind"] == "children_landed"]) == 1
        assert _handoffs(store, fo_id).count((1, "nudged")) == 1
    finally:
        store.close()


def test_a_manager_that_answers_the_nudge_with_more_work_is_nudged_again(fleet):
    """THE NUDGE INVITES EXACTLY THIS"""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        manager = store.manager_work_order(fo_id)
        first = _remediate(fleet, store, fo_id, "fix the header row")
        fleet.tick()
        assert _handoffs(store, fo_id) == [(1, "nudged")]

        second = _remediate(fleet, store, fo_id, "quote the separator")
        fleet.tick()

        assert _handoffs(store, fo_id) == [(1, "nudged"), (1, "nudged")], (
            "the manager filed more work and was not told the new one landed")
        assert store.get_feature_order(fo_id)["needs_attention"] == 0, (
            "a manager that answered the nudge was reported as having done nothing")
        posted = [e for e in envelopes(store) if e["kind"] == "children_landed"]
        assert len(posted) == 2
        fleet.tick()  # delivery
        text = messages(store, manager["id"])[-1]["content"]
        assert second["id"] in text
        assert first["id"] not in text, (
            "the second nudge re-announced a work order the manager already knew about")
    finally:
        store.close()


def test_a_stall_after_a_fresh_nudge_still_reaches_the_user(fleet):
    """The flag is keyed on what had been nudged when it was raised, for the same reason"""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        _remediate(fleet, store, fo_id, "fix the header row")
        fleet.tick()
        _until_idle(fleet, store, fo_id)  # read the nudge, did not submit
        assert _handoffs(store, fo_id) == [(1, "nudged"), (1, "flagged")]
        store.clear_feature_attention(fo_id)

        _remediate(fleet, store, fo_id, "quote the separator")
        fleet.tick()
        assert _handoffs(store, fo_id)[-1] == (1, "nudged")
        _until_idle(fleet, store, fo_id)  # and went quiet again

        fo = store.get_feature_order(fo_id)
        assert fo["needs_attention"] == 1, (
            "the second stall was swallowed by the first stall's flag")
        assert fo["attention_reason"] == FEATURE_MANAGER_STALLED.format(n=1, fo_id=fo_id)
        assert _handoffs(store, fo_id).count((1, "flagged")) == 2
    finally:
        store.close()


def test_a_manager_that_answers_a_rejection_with_no_work_orders_flags_the_feature(fleet):
    """Neo, question 467: silence must not read as "still working\""""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        _until_idle(fleet, store, fo_id)

        fleet.tick()

        fo = store.get_feature_order(fo_id)
        assert fo["needs_attention"] == 1
        assert fo["attention_reason"] == FEATURE_MANAGER_SILENT.format(n=1)
        assert not [e for e in envelopes(store) if e["kind"] == "children_landed"], (
            "the manager was nudged about work orders it never filed")
        assert _handoffs(store, fo_id) == [(1, "flagged")]
    finally:
        store.close()


def test_the_nudge_is_not_repeated_and_the_stall_reaches_the_user(fleet):
    """The fallback the issue asks for: nudged, and it still did not submit"""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()
        _remediate(fleet, store, fo_id, "fix the header row")
        fleet.tick()
        assert _handoffs(store, fo_id) == [(1, "nudged")]

        _until_idle(fleet, store, fo_id)  # it read the nudge and did not submit

        fo = store.get_feature_order(fo_id)
        assert fo["attention_reason"] == FEATURE_MANAGER_STALLED.format(n=1, fo_id=fo_id)
        assert _handoffs(store, fo_id) == [(1, "nudged"), (1, "flagged")]

        # ...and the user acknowledging it is not undone on the next tick
        store.clear_feature_attention(fo_id)
        for _ in range(3):
            fleet.tick()
        assert store.get_feature_order(fo_id)["needs_attention"] == 0
        assert _handoffs(store, fo_id) == [(1, "nudged"), (1, "flagged")]
    finally:
        store.close()


def test_nothing_is_said_while_the_rejection_is_still_on_its_way(fleet):
    """THE RACE THE GUARD EXISTS FOR"""
    fleet.daemon.validator = Validator(rejected())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()

        manager = store.manager_work_order(fo_id)
        assert manager["status"] == "idle", "the fixture no longer stages the race"
        assert [e["state"] for e in envelopes(store)] == ["queued"]
        fleet.daemon.deliver_envelopes = lambda *a, **k: None
        fleet.tick()
        assert [e["state"] for e in envelopes(store)] == ["queued"], (
            "the feedback was delivered after all; this no longer tests the guard")

        fo = store.get_feature_order(fo_id)
        assert fo["needs_attention"] == 0, fo["attention_reason"]
        assert _handoffs(store, fo_id) == []
    finally:
        store.close()


# -- 4. the counter, and why the loop terminates


def test_the_round_counter_does_not_reset_when_the_feature_goes_back_to_executing(fleet):
    """THE NON-TERMINATION GUARD, and nothing else tests it"""
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
            # What a manager does with feedback: file the work, let it land, resubmit
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
        # The last round gave up, so no envelope was posted for it: asking for a fourth submission there is...
        assert len(envelopes(store)) == 2
    finally:
        store.close()


def test_a_features_second_round_carries_what_its_first_one_asked_for(fleet):
    """THE FEATURE CALL SITE, which the work-order tests cannot reach"""
    validator = Validator(rejected(), passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()

        fleet.merge("fix.py", "# what the review actually asked for\n")
        assert ops.submit_feature(fo_id, "round 2", evidence="ran the suite")[
            "round"] == 2
        fleet.drain()
        assert [r["round"] for r in store.validation_rounds(fo_id=fo_id)] == [1, 2]
    finally:
        store.close()

    first, second = (validation.build_packet_prompt(c["packet"])
                     for c in validator.calls[:2])
    assert validation.HISTORY_HEADING not in first
    assert "the exporter and its caller disagree about the header row" not in first
    assert validation.HISTORY_HEADING in second
    assert "the exporter and its caller disagree about the header row" in second


def test_a_repeated_fingerprint_escalates_without_calling_the_panel(fleet):
    """A submission identical to the one before it is not new evidence"""
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

        # The one that changed nothing resubmits the same words over the same tree
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


# -- 5. what the user sees


def test_validating_raises_no_attention_and_an_escalated_feature_does(fleet):
    """`validating` is the system working, like `waiting_pr_merge`"""
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
    """The flag is only worth raising if it reaches the one surface the user reads"""
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


# -- 6. what must not change


def test_a_failed_child_still_fails_the_feature_with_a_manager_open(fleet):
    """`settle_features` still examines only `executing` features for the failure path"""
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

        # Flag once, by construction: the feature has left `executing`
        store.clear_feature_attention(fo_id)
        fleet.daemon.settle_features(fleet.spec, store)
        assert store.get_feature_order(fo_id)["needs_attention"] == 0
    finally:
        store.close()


def test_fo_submit_refuses_a_feature_that_is_not_executing(fleet):
    """Submitting a feature that is already under review asks for a second opinion on a round in flight"""
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


# -- 7. the kill switch


def test_the_kill_switch_drains_an_open_feature_round_instead_of_stranding_it(fleet):
    """THE FLAG IS A SAFE STOP, NOT A TRAPDOOR"""
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
    """A round with nothing to judge it settles its unit where the OS settles it with validation switched..."""
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
    """An outage is not a verdict"""
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


def test_a_feature_round_met_by_a_usage_window_waits_rather_than_spending_a_retry(fleet):
    """The work-order twin is tests/test_validation_usage_limit.py (#235)"""
    from jarvis.claude_cli import UsageLimit, UsageLimitError

    fleet.reconfigure(max_rounds=1)
    validator = Validator(UsageLimitError(UsageLimit(
        message="You've hit your session limit · resets 11:50pm (America/Los_Angeles)",
        reset_at=time.time() + 3600)))
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain(ticks=4)

        assert len(validator.calls) == 1, "the panel walked back into a closed window"
        assert store.latest_validation_round(fo_id=fo_id)["outcome"] == "failed"
        assert store.counted_validation_rounds(fo_id=fo_id) == 0
        assert store.get_feature_order(fo_id)["status"] == "validating"
        assert VALIDATION_STUCK_BLOCKER not in str(
            store.get_feature_order(fo_id)["attention_reason"] or "")
    finally:
        store.close()


def test_a_feature_round_the_shut_window_skips_records_the_hold(fleet):
    """GitHub issue #714 on the feature side"""
    from jarvis import fleet as fleet_mod
    from jarvis.project_store import VALIDATION_HELD_CAUSE, validation_hold

    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.daemon.settle_features(fleet.spec, store)
        assert store.get_feature_order(fo_id)["status"] == "validating"

        state = fleet_mod.Fleet(3, 0, fleet_mod.Outage(
            project="proj_a", wo_id="wo-1", reopens_at=time.time() + 3600,
            message="You've hit your session limit · resets 11:50pm"))
        for _ in range(3):
            fleet.daemon.feature_validation_tick(fleet.spec, store, state)

        assert validator.calls == [], "a seat was called into a closed window"
        assert store.latest_validation_round(fo_id=fo_id)["outcome"] == "pending"
        events = ops.feature_events_of_kind(store, fo_id, "validation_failed")
        assert len(events) == 1, "one event per round per window"
        until, cause = validation_hold(events, 1)
        assert cause == VALIDATION_HELD_CAUSE
        assert until == pytest.approx(state.outage.reopens_at)
    finally:
        store.close()


def test_a_held_feature_round_carries_the_cause_on_the_ROUND(fleet):
    """GitHub issue #581 on the feature side"""
    from jarvis.claude_cli import UsageLimit, UsageLimitError
    from jarvis.project_store import VALIDATION_HELD_CAUSE, validation_standing

    fleet.daemon.validator = Validator(UsageLimitError(UsageLimit(
        message="You've hit your session limit · resets 11:50pm (America/Los_Angeles)",
        reset_at=time.time() + 3600)))
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain()

        row = store.latest_validation_round(fo_id=fo_id)
        assert row["outcome"] == "failed", "the mechanism layer must not change"
        assert row["hold_cause"] == VALIDATION_HELD_CAUSE
        assert validation_standing(row) == ("held for the usage window", "active", "◑")

        # The projection `jarvis fo show` and the feature's dashboard page read
        projected = ops.validation_rounds(store, fo_id=fo_id)[-1]
        assert projected["hold_cause"] == VALIDATION_HELD_CAUSE
        line = ops.round_line(projected)
        assert "held for the usage window" in line and "· failed ·" not in line
    finally:
        store.close()


def test_a_feature_round_whose_seats_could_not_authenticate_is_held(fleet):
    """GitHub issue #778 on the feature side"""
    from jarvis.claude_cli import AuthFailure, AuthFailureError
    from jarvis.project_store import (VALIDATION_AUTH_CAUSE, validation_hold,
                                      validation_standing)

    auth = AuthFailure(
        message="Failed to authenticate: OAuth session expired and could not be refreshed")
    validator = Validator(AuthFailureError(auth))
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain(ticks=4)

        assert len(validator.calls) == 1, "the panel went again inside the backoff"
        row = store.latest_validation_round(fo_id=fo_id)
        assert row["outcome"] == "failed"
        assert row["hold_cause"] == VALIDATION_AUTH_CAUSE
        assert validation_standing(row) == ("held for authentication", "active", "◑")
        assert store.counted_validation_rounds(fo_id=fo_id) == 0
        assert store.get_feature_order(fo_id)["status"] == "validating"

        events = ops.feature_events_of_kind(store, fo_id, "validation_failed")
        until, cause = validation_hold(events, 1)
        assert cause == VALIDATION_AUTH_CAUSE
        assert until > time.time()

        # The STREAK is counted from those events
        Daemon._feature_auth_held(fleet.daemon, store, store.get_feature_order(fo_id),
                                  int(row["id"]), 1, auth)
        attempts = [json.loads(e["payload"])["attempt"]
                    for e in ops.feature_events_of_kind(store, fo_id,
                                                        "validation_failed")]
        assert attempts == [1, 2]
    finally:
        store.close()


def test_a_feature_with_no_manager_escalates_without_calling_the_panel(fleet):
    """A feature whose plan was released while validation was off has no manager"""
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


# -- 7. the give-up reaches the user (issue #199)


def _outbox(store: ProjectStore, unit_id: str) -> list[dict]:
    """The outbox rows this unit's give-up wrote, routed or not (docs/superpowers/specs/2026-09-25-a-plan-submission-is-not-an-empty-packet.md)"""
    rows = [dict(r) for r in store.conn.execute(
        "SELECT * FROM notifications WHERE source='validation' ORDER BY id").fetchall()]
    assert all(r["title"].startswith(("wo-", "fo-")) for r in rows), (
        "a validation notification is no longer titled by its unit id "
        f"({[r['title'] for r in rows]}), so this helper's filter now excludes "
        "everything")
    planner = str(store.get_feature_order(unit_id)["plan_wo_id"] or "")
    judged = [dict(r)["outcome"] for r in store.validation_rounds(wo_id=planner)]
    assert judged == ["void"], (
        "work-order validation stopped running in this fixture: the planner's rounds are "
        f"{judged}, not one voided round")
    return [r for r in rows if r["title"].startswith(unit_id)]


def test_a_feature_give_up_notifies_and_a_rejection_stays_silent(fleet):
    """PAIRED: a rejection has an addressee"""
    fleet.reconfigure(max_rounds=2)
    fleet.daemon.validator = Validator(rejected("the exporter has no empty-set test"))
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        fleet.drain()

        assert _outbox(store, fo_id) == [], "round 1 was rejected, not given up on"
        assert store.get_feature_order(fo_id)["status"] == "executing"

        fleet.merge("test_exporter.py", "def test_empty():\n    assert True\n")
        ops.submit_feature(fo_id, "added the test", evidence="pytest")
        fleet.drain()

        rows = _outbox(store, fo_id)
        assert len(rows) == 1
        assert rows[0]["title"] == VALIDATION_ESCALATED_TITLE.format(unit=fo_id, n=2)
        assert rows[0]["body"] == "the exporter has no empty-set test"
        assert (rows[0]["level"], rows[0]["wo_id"]) == ("warning", None)
    finally:
        store.close()


def test_the_feature_give_up_notification_reaches_the_central_inbox(fleet):
    """The empty-diff give-up never calls the panel"""
    from jarvis.central_store import CentralStore

    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.land_children(fo_id, store)  # nothing merged: escalates on the empty diff
        fleet.drain()
        fleet.tick()  # `route_outbox` carries what the give-up wrote
        rows = _outbox(store, fo_id)
        assert len(rows) == 1 and rows[0]["status"] == "routed"
    finally:
        store.close()

    central = CentralStore()
    try:
        rows = [i for i in central.unacked_inbox()
                if i["title"].startswith(fo_id)]
    finally:
        central.close()
    assert len(rows) == 1
    assert rows[0]["title"] == VALIDATION_ESCALATED_TITLE.format(unit=fo_id, n=1)
    assert "nothing has changed on the default branch" in rows[0]["body"]
    assert (rows[0]["level"], rows[0]["project"]) == ("warning", "proj_a")


# -- the prompt ceiling: a feature round refused, not retried


def _feature_refusal(chars: int = 500_000) -> claude_cli.PromptTooLargeError:
    """The transport's refusal as `Daemon._validate_feature` receives it"""
    return claude_cli.PromptTooLargeError(
        f"refused a validation call: prompt {chars} chars + system prompt 0 chars "
        f"= {chars}, over the 400000-char ceiling (os.max_os_prompt_chars)",
        prompt_chars=chars, system_prompt_chars=0, ceiling=400_000)


def test_a_refused_feature_round_escalates_once_and_burns_no_outage_retry(fleet):
    """`Daemon._feature_refused`, BEHAVIOURALLY"""
    from jarvis.central_store import CentralStore

    fleet.reconfigure(max_rounds=1)
    validator = Validator(_feature_refusal())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one")
        fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)

        fleet.drain(ticks=4)

        assert len(validator.calls) == 1, "a deterministic refusal was retried"
        transport = [e for e in ops.feature_events_of_kind(store, fo_id,
                                                           "validation_failed")
                     if json.loads(e["payload"]).get("cause") == "transport"]
        assert transport == [], "a refusal spent a retryable outage attempt"
        refused = ops.feature_events_of_kind(store, fo_id, "os_prompt_refused")
        assert len(refused) == 1
    finally:
        store.close()

    central = CentralStore()
    try:
        rows = [i for i in central.unacked_inbox() if "too large" in i["title"]]
    finally:
        central.close()
    assert len(rows) == 1
    assert rows[0]["level"] == "critical"
    # The FEATURE id and no `wo_id`: naming the manager would point every sink at a session rather than...
    assert fo_id in rows[0]["title"] and rows[0]["wo_id"] is None
    assert "os.max_os_prompt_chars" in rows[0]["body"]


# -- the head a round judges must contain the children's merges


def _dangling_commit(fleet) -> str:
    """A real commit that is NOT reachable from the default branch"""
    before = _git(fleet.project, "rev-parse", "HEAD").strip()
    sha = fleet.merge("sidecar.py", "sidecar = 1\n")
    _git(fleet.project, "reset", "--hard", before)
    return sha


def _pr_merged(store: ProjectStore, child_id: str, sha: str,
               *, minutes_ago: float = 0.0) -> None:
    """What the merge poller records when a child's pull request lands"""
    store.add_event(child_id, "pr_merged", {"merge_commit": sha},
                    ts=time.time() - minutes_ago * 60)


def _route(fleet, store, fo_id: str) -> bool:
    """`_route_to_validation`'s bool: "dealt with" — a round opened OR a deferral"""
    return fleet.daemon._route_to_validation(fleet.spec, store,
                                             store.get_feature_order(fo_id))


def _landed_feature(fleet, store, *keys: str) -> str:
    """A feature whose children have all completed over a real merge on the head"""
    fo_id = fleet.release("CSV export", *(keys or ("one",)))
    fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
    fleet.land_children(fo_id, store)
    return fo_id


def test_the_default_branch_is_fetched_before_a_feature_is_collected(fleet, monkeypatch):
    """§1"""
    calls: list[tuple] = []
    real_collect = evidence.collect_feature
    monkeypatch.setattr(branchproof, "fetch",
                        lambda repo, *refs: calls.append(("fetch", repo, refs)) or True)

    def collect(*a, **kw):
        calls.append(("collect",))
        return real_collect(*a, **kw)

    monkeypatch.setattr(evidence, "collect_feature", collect)
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)

        fleet.daemon.settle_features(fleet.spec, store)

        # Two fetches: §2's ancestry check fetches first, then §1's
        assert [c[0] for c in calls] == ["fetch", "fetch", "collect"]
        assert calls[0][1] == fleet.project
        assert calls[0][2] == (evidence.base_ref(fleet.project),), (
            "the fetch guessed a branch the collector did not judge")
        assert store.get_feature_order(fo_id)["status"] == "validating"
    finally:
        store.close()


def test_a_failed_fetch_is_logged_and_the_round_still_opens(fleet, monkeypatch):
    """§1"""
    monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: False)
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)

        fleet.daemon.settle_features(fleet.spec, store)

        assert store.get_feature_order(fo_id)["status"] == "validating"
        assert [r["round"] for r in store.validation_rounds(fo_id=fo_id)] == [1]
    finally:
        store.close()


def test_a_child_whose_merge_is_not_in_the_head_defers_the_round(fleet):
    """§2"""
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        _pr_merged(store, child["id"], _dangling_commit(fleet))

        assert _route(fleet, store, fo_id) is True, "deferred, so: leave it alone"

        assert store.validation_rounds(fo_id=fo_id) == []
        fo = store.get_feature_order(fo_id)
        assert fo["status"] == "executing"
        # Waiting is the system working, and the OS's rule is that waiting never flags
        assert fo["needs_attention"] == 0
    finally:
        store.close()


def test_settle_features_does_not_complete_a_deferred_feature(fleet):
    """§2"""
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        _pr_merged(store, child["id"], _dangling_commit(fleet))

        fleet.tick()

        assert store.get_feature_order(fo_id)["status"] == "executing"
        assert store.validation_rounds(fo_id=fo_id) == []
        assert fleet.daemon.validator.calls == []
    finally:
        store.close()


def test_a_deferred_feature_clears_itself_once_the_fetch_finds_the_merge(fleet,
                                                                        monkeypatch):
    """§2"""
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        sha = _dangling_commit(fleet)
        _pr_merged(store, child["id"], sha)

        assert _route(fleet, store, fo_id) is True, "deferred, so: leave it alone"
        assert store.validation_rounds(fo_id=fo_id) == []

        # What the production fetch does once the child's merge is upstream: the local default-branch ref...
        monkeypatch.setattr(branchproof, "fetch",
                            lambda repo, *refs: bool(_git(repo, "reset", "--hard", sha))
                            or True)

        assert _route(fleet, store, fo_id) is True, "unparked: the round opened"

        fo = store.get_feature_order(fo_id)
        assert fo["status"] == "validating"
        assert [r["round"] for r in store.validation_rounds(fo_id=fo_id)] == [1]
        assert fo["needs_attention"] == 0
    finally:
        store.close()


def test_the_check_failing_defers_rather_than_completing_the_feature(fleet, monkeypatch):
    """§2"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        _pr_merged(store, child["id"], evidence.default_branch_head(fleet.project))

        def boom(*a, **kw):
            raise RuntimeError("git is not answering about ancestry")

        monkeypatch.setattr(branchproof, "is_ancestor", boom)

        fleet.daemon.settle_features(fleet.spec, store)

        status = store.get_feature_order(fo_id)["status"]
        assert status not in ("completed", "validating"), status
        assert status == "executing"
        assert store.validation_rounds(fo_id=fo_id) == []
        assert validator.calls == []
    finally:
        store.close()


def test_past_the_bound_the_feature_asks_the_user_and_still_opens_no_round(fleet):
    """§3 (#881)"""
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        commit = _dangling_commit(fleet)
        waited = fleet.spec.validation.feature_merge_wait_minutes + 1
        _pr_merged(store, child["id"], commit, minutes_ago=waited)

        assert _route(fleet, store, fo_id) is True, "deferred, past the bound"

        fo = store.get_feature_order(fo_id)
        assert fo["needs_attention"] == 1
        reason = fo["attention_reason"]
        # Three things, because each points at a different remedy
        assert child["id"] in reason
        assert commit in reason
        assert evidence.default_branch_head(fleet.project) in reason
        assert store.validation_rounds(fo_id=fo_id) == []
        assert fo["status"] == "executing"
    finally:
        store.close()


def test_one_defer_event_per_distinct_state(fleet):
    """§3"""
    fleet.daemon.validator = Validator(passed())
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        commit = _dangling_commit(fleet)
        _pr_merged(store, child["id"], commit)

        assert _route(fleet, store, fo_id) is True  # deferred
        assert _route(fleet, store, fo_id) is True  # deferred again, same state

        events = ops.feature_events_of_kind(store, fo_id,
                                            Daemon.VALIDATION_DEFER_EVENT)
        assert len(events) == 1
        said = json.loads(events[0]["payload"])
        assert said["child"] == child["id"] and said["commit"] == commit
        assert said["head"] == evidence.default_branch_head(fleet.project)
        assert said["expired"] is False

        store.conn.execute(
            "UPDATE wo_events SET ts=? WHERE wo_id=? AND kind='pr_merged'",
            (time.time() - (fleet.spec.validation.feature_merge_wait_minutes + 1) * 60,
             child["id"]))
        store.conn.commit()

        assert _route(fleet, store, fo_id) is True  # deferred, now past the bound
        assert _route(fleet, store, fo_id) is True  # deferred, still past it

        events = ops.feature_events_of_kind(store, fo_id,
                                            Daemon.VALIDATION_DEFER_EVENT)
        assert len(events) == 2
        assert json.loads(events[1]["payload"])["expired"] is True
    finally:
        store.close()


def test_every_child_integrated_opens_the_round_as_today(fleet):
    """§2"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = fleet.release("CSV export", "one", "two")
        merged = fleet.merge("exporter.py", "def export():\n    return 'a,b'\n")
        fleet.land_children(fo_id, store)
        first, second = store.feature_children(fo_id)
        _pr_merged(store, first["id"], merged)   # landed on the head
        # `second` has no pr_url and no merge event: it delivered without a PR

        assert _route(fleet, store, fo_id) is True, "nothing deferred: the round opened"

        fo = store.get_feature_order(fo_id)
        assert fo["status"] == "validating"
        assert [(r["round"], r["outcome"])
                for r in store.validation_rounds(fo_id=fo_id)] == [(1, "pending")]
        assert len(ops.feature_events_of_kind(store, fo_id,
                                              "validation_submitted")) == 1
        assert ops.feature_events_of_kind(store, fo_id,
                                          Daemon.VALIDATION_DEFER_EVENT) == []
    finally:
        store.close()


def test_a_failing_fetch_defers_rather_than_judging_a_stale_head(fleet, monkeypatch):
    """§2 + Neo 1394"""
    monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: False)
    monkeypatch.setattr(Daemon, "_defer_unintegrated_children",
                        lambda *a, **kw: False)
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        _pr_merged(store, child["id"], _dangling_commit(fleet))

        fleet.daemon.settle_features(fleet.spec, store)

        status = store.get_feature_order(fo_id)["status"]
        assert status not in ("completed", "validating"), status
        assert status == "executing"
        assert store.validation_rounds(fo_id=fo_id) == []
        assert validator.calls == []
    finally:
        store.close()


def test_fo_submit_errors_to_the_caller_instead_of_deferring(fleet, monkeypatch):
    """Neo 1394 holds: `ops` cannot defer"""
    monkeypatch.setattr(branchproof, "fetch", lambda repo, *refs: False)
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)
        child = store.feature_children(fo_id)[0]
        commit = _dangling_commit(fleet)
        _pr_merged(store, child["id"], commit)
        head = evidence.default_branch_head(fleet.project)

        with pytest.raises(ops.UnintegratedChild) as caught:
            ops.submit_feature_for_validation(
                store, fleet.project, store.get_feature_order(fo_id),
                declared="", summary="", cfg=fleet.spec.validation)

        err = caught.value
        assert (err.child_id, err.commit, err.head) == (child["id"], commit, head)
        # Three things, because each points at a different remedy
        for fact in (child["id"], commit, head):
            assert fact in str(err)
        assert store.validation_rounds(fo_id=fo_id) == []
        assert store.get_feature_order(fo_id)["status"] == "executing"
    finally:
        store.close()


def test_the_two_merge_commit_event_literals_stay_equal():
    """Drift and `ops.recorded_merge_commit` stops seeing the daemon's back-filled merge commit"""
    assert Daemon.MERGE_COMMIT_EVENT == ops.MERGE_COMMIT_EVENT


def test_a_feature_round_records_the_commit_it_judged(fleet):
    """§5"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        fo_id = _landed_feature(fleet, store)

        fleet.drain()

        head = evidence.default_branch_head(fleet.project)
        assert head
        rnd = store.latest_validation_round(fo_id=fo_id)
        assert rnd["head_sha"] == head
        assert validator.calls[0]["packet"].head == head
    finally:
        store.close()


def test_the_judged_commit_is_recorded_on_the_escalate_paths_too(fleet):
    """§5(b)"""
    validator = Validator(passed())
    fleet.daemon.validator = validator
    store = fleet.store()
    try:
        baseless = _landed_feature(fleet, store)
        store.update_feature_order(baseless, base_sha=None)
        empty = fleet.release("nothing merged", "one")
        fleet.land_children(empty, store)

        fleet.drain()

        head = evidence.default_branch_head(fleet.project)
        # A feature with no base resolves no head either (`collect_feature`)
        for fo_id, why, judged in (
                (baseless, "no recorded base commit", ""),
                (empty, "nothing has changed on the default branch", head)):
            rnd = store.latest_validation_round(fo_id=fo_id)
            assert rnd["outcome"] == "escalated", fo_id
            assert why in rnd["reason"]
            assert rnd["head_sha"] == judged, fo_id
    finally:
        store.close()
