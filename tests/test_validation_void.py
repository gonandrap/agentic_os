"""A round the panel VOIDS: there was nothing for a reviewer, and nobody is owed a
decision. Spec docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md.

The live case is wo-ec96a1e9: a release order that shipped 0.10.5 perfectly and was
escalated to the user because its worktree was empty, which is what makes autonomous
shipping impossible. These drive the real `Daemon._validate_work_order`, for the reason
tests/test_validation_side_effects.py gives — the defect was never in one function.
"""

from __future__ import annotations

import json

import pytest

from jarvis import evidence, ops, release
from jarvis.evidence import EvidencePacket
from jarvis.invariants import VALIDATION_STUCK_BLOCKER, true_blockers
from tests.test_release_staging import FakeRunner, prod_checkout  # noqa: F401
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)

pytestmark = pytest.mark.usefixtures("fake_systemd")


@pytest.fixture(autouse=True)
def no_real_systemd(monkeypatch):
    """Every test here stages a release, so every tick reaches `release.maybe_restart`.
    Without this the suite asks the DEVELOPER's systemd to restart the fleet."""
    monkeypatch.setattr(release, "SystemdRunner", FakeRunner)


def _round(fleet, wo_id):
    store = fleet.store()
    try:
        return dict(store.validation_rounds(wo_id=wo_id)[-1])
    finally:
        store.close()


def stage_release(wo_id: str, version: str = "0.10.5") -> None:
    """The marker `scripts/shipit.sh --stage` writes, byte for byte in shape."""
    path = release.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "wo_id": wo_id, "project": "proj_a", "version": version,
        "tag": f"jarvis-{version}", "staged_at": 1_758_000_000, "state": "staged"}))


# ------------------------------------------------- the rule itself, one packet at a time


def _packet(files=(), side_effects=()) -> EvidencePacket:
    return EvidencePacket(
        unit="work_order", subject_id="wo-1", title="t", description="", summary="",
        declared="", pr_url="", base="", head="", stat="", diff="",
        diff_truncated=False, dropped_files=(), diff_sha="",
        files=tuple(files), side_effects=tuple(side_effects))


def test_the_whole_decision_table():
    """Four rows, and the two that must NOT move are rows 2 and 3 — spec §3."""
    attested = {"kind": "release_staged", "attested": True}
    judgeable = {"kind": "knowledge_added", "attested": False}

    assert evidence.nothing_to_judge(_packet(files=("a.py",))) == ""
    assert evidence.nothing_to_judge(_packet()) == "escalate"
    assert evidence.nothing_to_judge(_packet(side_effects=[judgeable])) == ""
    assert evidence.nothing_to_judge(_packet(side_effects=[attested])) == "void"
    # A judgeable effect BESIDE an attested one is still judged: voiding there would
    # throw away the half a reviewer could have read.
    assert evidence.nothing_to_judge(
        _packet(side_effects=[attested, judgeable])) == ""
    # Files present outrank everything — a release that also changed code is reviewed.
    assert evidence.nothing_to_judge(
        _packet(files=("a.py",), side_effects=[attested])) == ""


def test_an_effect_with_no_attested_key_at_all_is_judged_not_voided():
    """The fail-safe direction for a record written before the flag existed, or by a
    collector that returns dicts of its own shape."""
    assert evidence.nothing_to_judge(_packet(side_effects=[{"kind": "mystery"}])) == ""


def test_a_collector_that_does_not_opt_in_is_not_attested():
    """`attested` defaults to False, so tomorrow's collector gets JUDGED rather than
    silently voided — Neo's condition on question 378."""
    assert ops.SideEffectCollector("new", lambda s, w: []).attested is False
    by_name = {c.name: c for c in ops.SIDE_EFFECT_COLLECTORS}
    assert by_name["knowledge"].attested is False
    assert by_name["release"].attested is True


def test_the_stamp_is_the_registrys_and_never_the_collectors(fleet):
    """A collector cannot mark its own effects attested: the registry overwrites it."""
    liar = ops.SideEffectCollector(
        "liar", lambda store, wo_id: [{"kind": "x", "attested": True}])
    store = fleet.store()
    try:
        original = ops.SIDE_EFFECT_COLLECTORS
        ops.SIDE_EFFECT_COLLECTORS = (liar,)
        try:
            assert ops.side_effects_of(store, "wo-any")[0]["attested"] is False
        finally:
            ops.SIDE_EFFECT_COLLECTORS = original
    finally:
        store.close()


def test_attested_is_not_hashed_into_the_digest():
    """It is stamped by the OS, not produced by the submitter — `history`'s rule. Hashing
    it would change the digest of every knowledge effect already collected and make the
    next round of every open work order read as new evidence (spec §6)."""
    bare = [{"kind": "knowledge_added", "id": "kn-1"}]
    stamped = [{"kind": "knowledge_added", "id": "kn-1", "attested": False}]
    flipped = [{"kind": "knowledge_added", "id": "kn-1", "attested": True}]
    assert evidence.side_effects_digest(bare) == evidence.side_effects_digest(stamped)
    assert evidence.side_effects_digest(bare) == evidence.side_effects_digest(flipped)
    # ...and everything else still moves it.
    assert evidence.side_effects_digest(bare) != evidence.side_effects_digest(
        [{"kind": "knowledge_added", "id": "kn-2"}])


# ------------------------------------------------------ the release, collected and voided


def test_a_release_work_order_ships_and_settles_with_no_human_touch(fleet):
    """THE DEFINITION OF DONE. wo-ec96a1e9 did all of this and was escalated anyway."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch("ship 0.10.5")  # a release authors nothing in its worktree
    stage_release(wo["id"])

    finish(fleet, wo["id"], summary="shipped 0.10.5", pr=None)
    fleet.drain()

    assert seen.calls == [], "a reviewer was asked to judge a release"
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "void"
    assert "verifies itself" in rnd["reason"]
    assert "jarvis-0.10.5" in rnd["reason"]

    store = fleet.store()
    try:
        after = store.get_work_order(wo["id"])
        assert after["status"] == "completed"
        assert not after["needs_attention"], "a voided release asked for the user"
        # The flag is re-derived every reconcile tick, so "cleared once" is not the
        # claim that matters — this is.
        assert VALIDATION_STUCK_BLOCKER not in true_blockers(store, after)
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0, \
            "a round nobody judged spent one of the submitter's"
    finally:
        store.close()


def test_the_void_is_on_the_timeline_in_words(fleet):
    """The record is what the user and Neo read — a round that vanished silently is the
    same defect one step quieter."""
    from jarvis import timeline

    fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch("ship it")
    stage_release(wo["id"])
    finish(fleet, wo["id"], summary="shipped", pr=None)
    fleet.drain()

    store = fleet.store()
    try:
        events = store.events_of_kind(wo["id"], "validation_void")
        assert len(events) == 1
        rows = timeline.build_timeline(store.get_work_order(wo["id"]),
                                       store.list_events(wo["id"]), [])
    finally:
        store.close()
    labels = [r["label"] for r in rows if r["kind"] == "validation_void"]
    assert labels == ["Validation voided — nothing for a reviewer to judge"]


def test_the_release_effect_survives_the_marker_being_consumed(fleet):
    """`verify_on_boot` DELETES the marker on success. A round still pending across that
    daemon restart would otherwise collect nothing and escalate a shipped release."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch("ship 0.11.0")
    store = fleet.store()
    try:
        store.add_event(wo["id"], "release_verified",
                        {"version": "0.11.0", "tag": "jarvis-0.11.0",
                         "detail": "release jarvis-0.11.0 verified live"})
    finally:
        store.close()
    assert release.read_marker() is None, "the fixture left a marker behind"

    finish(fleet, wo["id"], summary="shipped 0.11.0", pr=None)
    fleet.drain()

    assert seen.calls == []
    assert _round(fleet, wo["id"])["outcome"] == "void"


def test_a_marker_naming_a_DIFFERENT_work_order_is_not_this_ones_effect(fleet):
    """Otherwise any empty work order finishing while a release was in flight would void
    itself on somebody else's tag — a submitter reaching void by delivering nothing."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch("delivered nothing")
    stage_release("wo-somebody-else")

    finish(fleet, wo["id"], summary="nothing", pr=None)
    fleet.drain()

    assert seen.calls == []
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "escalated"
    assert "nothing to review" in rnd["reason"]


def test_a_release_that_also_wrote_knowledge_is_judged_not_voided(fleet):
    """One attested effect does not clear the packet: the knowledge write is something a
    reviewer can read, so the round goes to the panel."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch("ship and learn")
    stage_release(wo["id"])
    ops.learn_add("what shipping taught us", wo_id=wo["id"])

    finish(fleet, wo["id"], summary="shipped", pr=None)
    fleet.drain()

    assert len(seen.calls) == 1, "the knowledge write was never judged"
    kinds = {e["kind"] for e in seen.calls[0]["packet"].side_effects}
    assert kinds == {"release_staged", "knowledge_added"}
    assert _round(fleet, wo["id"])["outcome"] == "passed"


def test_the_release_effect_says_what_a_reader_can_check(fleet):
    """The packet is the record too: a void that recorded nothing would leave the OS
    unable to say WHAT it decided not to review."""
    store = fleet.store()
    try:
        stage_release("wo-x", version="1.2.3")
        effect = ops.side_effects_of(store, "wo-x")[0]
    finally:
        store.close()
    assert effect["kind"] == "release_staged"
    assert effect["id"] == "jarvis-1.2.3"
    assert "jarvis-1.2.3" in effect["summary"]
    assert "1.2.3" in effect["detail"]
    assert effect["attested"] is True


def test_a_work_order_that_shipped_nothing_collects_no_release_effect(fleet):
    store = fleet.store()
    try:
        assert ops.side_effects_of(store, "wo-quiet") == []
    finally:
        store.close()


def test_the_staged_release_handshake_still_runs_end_to_end(fleet, prod_checkout):
    """The void is not the end of the release — the daemon's half still has to run.

    Void → `completed`, the reconcile hook restarts the units because the shipping
    worker's turn has settled, and the boot check verifies the version on disk and
    leaves the work order exactly where the void put it.
    """
    fleet.daemon.validator = Validator(passed())
    runner = FakeRunner()
    fleet.daemon.release_runner = runner  # the daemon's own seam — see `no_real_systemd`
    wo = fleet.dispatch("ship 0.6.0")
    stage_release(wo["id"], version="0.6.0")
    finish(fleet, wo["id"], summary="shipped 0.6.0", pr=None)
    fleet.drain()

    store = fleet.store()
    try:
        assert _round(fleet, wo["id"])["outcome"] == "void"
        assert store.get_work_order(wo["id"])["status"] == "completed"

        # The DAEMON's own tick did the restart, once the shipping worker's turn had
        # settled — that hand-off is the whole point of staging and the void must not
        # have moved it.
        assert ("restart", release.UI_UNIT) in runner.calls
        assert ("restart_detached", release.DAEMON_UNIT, "jarvis-0.6.0") in runner.calls

        marker = release.read_marker()
        assert marker["state"] == "restarting"
        base = marker["restart_at"] + 5
        def store_for(name):
            return store if name == "proj_a" else None

        out = release.verify_on_boot(store_for, runner=FakeRunner(
            {release.DAEMON_UNIT: base, release.UI_UNIT: base + 1}))

        assert out["verified"] is True
        assert store.get_work_order(wo["id"])["status"] == "completed"
        assert release.read_marker() is None
    finally:
        store.close()
