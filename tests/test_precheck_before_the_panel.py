"""A fix order whose rule is not proved never reaches the panel, and never costs a round (docs/superpowers/specs/2026-09-27-self-evolution.md)"""

from __future__ import annotations

import json
import time

from jarvis import evidence, ops, remedies, rules, validation
from jarvis.central_store import CentralStore
from jarvis.evidence import EvidencePacket
from jarvis.invariants import IDLE_NO_FINISH_BLOCKER
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)


# -- staging


def register(gap_class: str, condition: dict, *, primitive: str = "",
             params: dict | None = None) -> tuple[str, str]:
    """A detector in the REAL central store, optionally with one remedy row"""
    central = CentralStore()
    try:
        det = central.add_detector(gap_class, condition, source="user",
                                   summary="staged by a test")
        rid = ""
        if primitive:
            rid = central.add_remedy_rule(det["id"], primitive, params=params or {},
                                          argument="staged by a test")["id"]
        return det["id"], rid
    finally:
        central.close()


def trigger_order(fleet, *, status: str = "needs_review", attention: str = "",
                  round_outcome: str = "", ack: tuple[str, ...] = ()) -> dict:
    """An ordinary work order put into the recorded state a detector is proved against"""
    wo = fleet.dispatch("the order the rule is proved against")
    store = fleet.store()
    try:
        store.set_status(wo["id"], status)
        if attention:
            store.update_work_order(wo["id"], needs_attention=1,
                                    attention_reason=attention)
        if round_outcome:
            row = store.open_validation_round(
                wo_id=wo["id"], fingerprint="f" * 16, summary="done", evidence="e",
                pr_url="", round=1, config_version="t")
            store.close_validation_round(int(row["id"]), round_outcome, "judged")
        if ack:
            # THE USER HAS ALREADY SEEN AND DISMISSED THAT LINE (#786)
            store.update_work_order(wo["id"], acknowledged_blockers=json.dumps(list(ack)))
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def age_status(fleet, wo_id: str, seconds: float) -> None:
    """Backdate every recorded status span, so the order has been where it is for an hour"""
    store = fleet.store()
    try:
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE order_id=?",
                           (time.time() - seconds, wo_id))
        store.conn.commit()
        staged = ops.rule_facts(store, store.get_work_order(wo_id),
                                now=time.time()).values.get("seconds_in_status")
        assert staged and staged >= seconds - 60, (
            f"the staged order is not old enough to test the condition: {staged}")
    finally:
        store.close()


def dispatch_fix_order(fleet, **columns) -> tuple[str, Validator]:
    """A fix order carrying the four §7.1 columns, dispatched but not yet delivered"""
    v = fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch("close the gap", **columns)
    return wo["id"], v


def deliver(fleet, wo_id: str) -> None:
    """Finish it the way a worker that produced code does: behind a pull request"""
    fleet.change(wo_id, "print('the fix')\n")
    finish(fleet, wo_id)


def submit_fix_order(fleet, **columns) -> tuple[str, Validator]:
    wo_id, v = dispatch_fix_order(fleet, **columns)
    deliver(fleet, wo_id)
    return wo_id, v


def rounds(fleet, wo_id: str) -> list[dict]:
    store = fleet.store()
    try:
        return [dict(r) for r in store.validation_rounds(wo_id=wo_id)]
    finally:
        store.close()


def refusal_event(fleet, wo_id: str) -> dict:
    store = fleet.store()
    try:
        [event] = store.events_of_kind(wo_id, "validation_refused")
        return json.loads(event["payload"] or "{}")
    finally:
        store.close()


def refusal_reason(fleet, wo_id: str) -> str:
    """The refusal's words off the TIMELINE, which is where they stay"""
    return str(refusal_event(fleet, wo_id)["reason"])


def why_refused(fleet, wo_id: str) -> str:
    """The refusal if there was one, `""` if there was not"""
    store = fleet.store()
    try:
        events = store.events_of_kind(wo_id, "validation_refused")
    finally:
        store.close()
    return "; ".join(str(json.loads(e["payload"] or "{}").get("reason") or "")
                     for e in events)


def envelope(fleet, wo_id: str) -> dict:
    """The queued envelope, NOT a delivered message: `bus.post` never resolves"""
    store = fleet.store()
    try:
        [env] = [e for e in store.queued_envelopes() if e["subject_wo_id"] == wo_id]
        return {**dict(env), "payload": json.loads(env["payload"])}
    finally:
        store.close()


# -- (a)-(d): the four refusals, none of which pays a seat


def test_a_gap_class_with_no_registered_detector_never_reaches_the_panel(fleet):
    trigger = trigger_order(fleet)
    wo_id, v = submit_fix_order(fleet, gap_class="nothing-registered",
                                detector_id="dt-proposed",
                                trigger_wo_id=trigger["id"])
    fleet.drain()

    assert v.calls == [], "a refused submission paid for a panel"
    reason = refusal_reason(fleet, wo_id)
    assert "nothing-registered" in reason and "--detector-id" in reason


def test_a_detector_with_no_remedy_referencing_it_never_reaches_the_panel(fleet):
    trigger = trigger_order(fleet)
    det, _ = register("no-remedy-yet",
                      {"field": "status", "op": "eq", "value": "needs_review"})
    wo_id, v = submit_fix_order(fleet, gap_class="no-remedy-yet", detector_id=det,
                                trigger_wo_id=trigger["id"])
    fleet.drain()

    assert v.calls == []
    reason = refusal_reason(fleet, wo_id)
    assert det in reason and "remedy" in reason


def test_a_detector_that_does_not_fire_on_the_triggering_order_is_refused(fleet):
    """Check 3 is a REAL evaluation against the triggering order's recorded facts"""
    trigger = trigger_order(fleet, status="needs_review")
    det, rid = register("never-fires",
                        {"field": "status", "op": "eq", "value": "cancelled"},
                        primitive="nudge", params={"what": "say something"})
    wo_id, v = submit_fix_order(fleet, gap_class="never-fires", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id=trigger["id"])
    fleet.drain()

    assert v.calls == []
    reason = refusal_reason(fleet, wo_id)
    assert det in reason and trigger["id"] in reason
    assert "status" in reason, "a refusal that does not name the leaf is unactionable"


def test_a_remedy_whose_preconditions_do_not_hold_is_refused_in_its_own_words(fleet):
    """Check 4, through the REAL `lower_attention.can_apply`: the flag is already down"""
    trigger = trigger_order(fleet, status="needs_review")
    det, rid = register("flag-already-down",
                        {"field": "needs_attention", "op": "eq", "value": False},
                        primitive="lower_attention")
    wo_id, v = submit_fix_order(fleet, gap_class="flag-already-down", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id=trigger["id"])
    fleet.drain()

    assert v.calls == []
    reason = refusal_reason(fleet, wo_id)
    # VERBATIM, the precondition's own sentence — `rules.precheck` does not re-word it
    assert f"the attention flag on {trigger['id']} is already down" in reason


def test_a_fix_order_with_no_triggering_order_at_all_is_refused(fleet):
    """`trigger_wo_id` empty and no improvement-order back-link: unproven"""
    det, rid = register("unproven",
                        {"field": "status", "op": "eq", "value": "needs_review"},
                        primitive="nudge", params={"what": "say something"})
    wo_id, v = submit_fix_order(fleet, gap_class="unproven", detector_id=det,
                                remedy_rule_id=rid)
    fleet.drain()

    assert v.calls == []
    assert "--trigger-wo-id" in refusal_reason(fleet, wo_id)


# -- (e): a proved pair is judged like anything else


def test_a_passing_detector_and_remedy_reach_the_panel(fleet):
    trigger = trigger_order(fleet, status="needs_review",
                            attention="the review could not be satisfied",
                            ack=(IDLE_NO_FINISH_BLOCKER,))
    det, rid = register("provable",
                        {"field": "needs_attention", "op": "eq", "value": True},
                        primitive="lower_attention")
    wo_id, v = submit_fix_order(fleet, gap_class="provable", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id=trigger["id"])
    fleet.drain()

    assert len(v.calls) == 1, f"a proved fix order was not judged: {why_refused(fleet, wo_id)}"
    assert [r["outcome"] for r in rounds(fleet, wo_id)] == ["passed"]


# -- what a refusal leaves behind


def test_a_refusal_opens_no_round_and_records_the_four_ids(fleet):
    trigger = trigger_order(fleet)
    wo_id, v = submit_fix_order(fleet, gap_class="nothing-registered",
                                detector_id="dt-proposed",
                                remedy_rule_id="rm-proposed",
                                trigger_wo_id=trigger["id"])
    # THE ENVELOPE IS READ BEFORE ANYTHING TICKS: `bus.post` queues it and the router deletes the row on...
    env = envelope(fleet, wo_id)
    fleet.drain()

    assert rounds(fleet, wo_id) == [], "a paperwork failure burned a round number"
    payload = refusal_event(fleet, wo_id)
    assert payload["gap_class"] == "nothing-registered"
    assert payload["detector_id"] == "dt-proposed"
    assert payload["remedy_rule_id"] == "rm-proposed"
    assert payload["trigger_wo_id"] == trigger["id"]
    assert "nothing-registered" in payload["reason"]
    assert payload["would_be_round"] == 1

    assert env["to_role"] == "implementor"
    assert env["payload"]["outcome"] == "rejected"
    assert env["payload"]["round"] == 1
    assert "no round was spent" in env["payload"]["reason"]
    assert v.calls == []


def test_an_ordinary_order_is_not_pre_checked_and_is_judged(fleet):
    """The pairing: without it, "it reached the panel" proves nothing about the gate"""
    wo_id, v = submit_fix_order(fleet)
    fleet.drain()

    assert len(v.calls) == 1
    store = fleet.store()
    try:
        assert store.events_of_kind(wo_id, "validation_refused") == []
    finally:
        store.close()


# -- §7.3: the checklist, in the packet and nowhere else


def test_the_checklist_is_in_a_fix_orders_packet_and_absent_from_an_ordinary_one(
        tmp_path):
    """Both directions"""
    fix = {"id": "wo-fix", "title": "close the gap", "description": "d",
           "gap_class": "stale-panel-hold", "detector_id": "dt-1",
           "remedy_rule_id": "rm-1", "trigger_wo_id": "wo-trigger"}
    packet = evidence.collect_work_order(tmp_path, fix, declared="ran the tests")
    assert packet.gap_class == "stale-panel-hold"
    assert packet.detector_id == "dt-1"
    assert packet.remedy_rule_id == "rm-1"
    assert packet.trigger_wo_id == "wo-trigger"
    for id_ in ("stale-panel-hold", "dt-1", "rm-1", "wo-trigger"):
        assert id_ in packet.self_evolution
    assert "QUALITY" in packet.self_evolution
    assert "over-fire" in packet.self_evolution and "idempotent" in packet.self_evolution
    assert "follow-up" in packet.self_evolution
    prompt = validation.build_packet_prompt(packet)
    assert packet.self_evolution in prompt

    ordinary = evidence.collect_work_order(
        tmp_path, {"id": "wo-1", "title": "t", "description": "d"}, declared="")
    assert ordinary.self_evolution == "" and ordinary.gap_class == ""
    assert "self-evolution" not in validation.build_packet_prompt(ordinary).lower()


# -- the fingerprint, which may not move for an ordinary order


#: Computed on the code BEFORE the four ids joined the formula
ORDINARY_FINGERPRINT = "964357cd1385c72b"


def _ordinary_packet(**extra) -> EvidencePacket:
    return EvidencePacket(
        unit="work_order", subject_id="wo-fix", title="t", description="d",
        summary="s", declared="ran pytest -q: 412 passed", pr_url="", base="main",
        head="abc", stat="", files=("src/app.py",), diff="x", diff_truncated=False,
        dropped_files=(), diff_sha="deadbeef", side_effects_sha="cafe", **extra)


def test_an_ordinary_packet_hashes_to_its_pre_change_value():
    assert evidence.fingerprint(_ordinary_packet()) == ORDINARY_FINGERPRINT
    # And the four fields being PRESENT but empty changes nothing either: that is the row every...
    assert evidence.fingerprint(_ordinary_packet(
        gap_class="", detector_id="", remedy_rule_id="", trigger_wo_id="",
        self_evolution="")) == ORDINARY_FINGERPRINT


def test_a_fix_orders_ids_join_the_hash_and_the_injected_prose_does_not():
    base = _ordinary_packet(gap_class="g", detector_id="dt-1", remedy_rule_id="rm-1",
                            trigger_wo_id="wo-t", self_evolution="rendered checklist")
    assert evidence.fingerprint(base) != ORDINARY_FINGERPRINT
    # A DIFFERENT TRIGGERING ORDER IS NEW EVIDENCE, with the same diff (§7.3)
    moved = _ordinary_packet(gap_class="g", detector_id="dt-1", remedy_rule_id="rm-1",
                             trigger_wo_id="wo-other", self_evolution="x")
    assert evidence.fingerprint(moved) != evidence.fingerprint(base)
    # THE RENDERED PROSE IS THE OS'S, regenerated every round: it may not move the hash
    reworded = _ordinary_packet(gap_class="g", detector_id="dt-1", remedy_rule_id="rm-1",
                                trigger_wo_id="wo-t",
                                self_evolution="a differently worded checklist")
    assert evidence.fingerprint(reworded) == evidence.fingerprint(base)


def test_the_unproven_notes_do_not_move_the_fingerprint():
    """Same row of the exclusion table as `self_evolution`: the OS wrote the notes"""
    base = _ordinary_packet(gap_class="g", detector_id="dt-1", remedy_rule_id="rm-1",
                            trigger_wo_id="wo-t", self_evolution="rendered checklist")
    noted = _ordinary_packet(
        gap_class="g", detector_id="dt-1", remedy_rule_id="rm-1", trigger_wo_id="wo-t",
        self_evolution="rendered checklist",
        self_evolution_unproven=("check 4 was not run: the predicate raised",))
    assert evidence.fingerprint(noted) == evidence.fingerprint(base)


# -- the daemon site: a forced round is NOT exempt, and it spends the round


def test_a_forced_round_whose_precheck_fails_is_rejected_without_paying_a_seat(fleet):
    """No exemption at the daemon site (Neo question 1681)"""
    trigger = trigger_order(fleet)
    v = fleet.daemon.validator = Validator(passed())
    wo = fleet.dispatch("close the gap", gap_class="nothing-registered",
                        detector_id="dt-proposed", trigger_wo_id=trigger["id"])
    fleet.change(wo["id"], "print('the fix')\n")

    store = fleet.store()
    try:
        # Straight to the submission site in its FORCED form
        row = ops.submit_for_validation(
            store, fleet.project, store.get_work_order(wo["id"]),
            declared="", cfg=ops.validation_config("proj_a"),
            forced_reason="re-judging against the current head")
        assert row is not None, "the forced submission was refused at the wrong site"
    finally:
        store.close()

    fleet.drain()

    assert v.calls == [], "the panel was paid for a round the pre-check refuses"
    [judged] = rounds(fleet, wo["id"])
    assert judged["outcome"] == "rejected"
    assert "nothing-registered" in str(judged["reason"])
    reason = envelope(fleet, wo["id"])["payload"]["reason"]
    assert "HAS been spent" in reason


# -- a named trigger the records do not have: INPUT, so it refuses


def test_a_trigger_wo_id_that_names_no_order_is_refused_before_the_panel(fleet):
    """THE ROUND-2 DEFECT, end to end: a made-up `--trigger-wo-id` used to read as UNREADABLE"""
    det, rid = register("never-fires-either",
                        {"field": "status", "op": "eq", "value": "cancelled"},
                        primitive="nudge", params={"what": "say something"})
    wo_id, v = submit_fix_order(fleet, gap_class="never-fires-either", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id="wo-does-not-exist")
    fleet.drain()

    assert v.calls == [], "a fix order skipped the dry run by naming a made-up order"
    reason = refusal_reason(fleet, wo_id)
    assert "wo-does-not-exist" in reason and "--trigger-wo-id" in reason


# -- a check that passed UNEVALUATED is in the packet the seats read


#: The claim the checklist drops when a check went unevaluated
MECHANICAL = "all decided mechanically"


def test_a_trigger_whose_facts_cannot_be_built_reaches_the_panel_saying_check_3_was_not_run(
        fleet, monkeypatch):
    """A GENUINE read failure, which is the pass-through this feature keeps: the panel judges"""
    trigger = trigger_order(fleet, status="needs_review")
    det, rid = register("facts-unreadable",
                        {"field": "status", "op": "eq", "value": "needs_review"},
                        primitive="nudge", params={"what": "say something"})
    real = rules.facts

    def boom(store, wo, **kw):
        if str(wo.get("id")) == trigger["id"]:
            raise RuntimeError("the state rows could not be read")
        return real(store, wo, **kw)

    monkeypatch.setattr(rules, "facts", boom)
    wo_id, v = submit_fix_order(fleet, gap_class="facts-unreadable", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id=trigger["id"])
    fleet.drain()

    assert len(v.calls) == 1, f"an unreadable trigger was refused: {why_refused(fleet, wo_id)}"
    checklist = v.calls[0]["packet"].self_evolution
    assert MECHANICAL not in checklist, (
        f"the packet claimed a check that never ran: "
        f"{v.calls[0]['packet'].self_evolution_unproven}")
    assert "check 3" in checklist and "the state rows could not be read" in checklist
    assert "YOURS TO RAISE" in checklist


def test_a_can_apply_that_raises_reaches_the_panel_saying_check_4_was_not_run(
        fleet, monkeypatch):
    import dataclasses

    trigger = trigger_order(fleet, status="needs_review")
    det, rid = register("can-apply-raises",
                        {"field": "status", "op": "eq", "value": "needs_review"},
                        primitive="nudge", params={"what": "say something"})

    def boom(store, subject, params):
        raise RuntimeError("the predicate itself failed")

    monkeypatch.setitem(remedies.REMEDIES, "nudge",
                        dataclasses.replace(remedies.REMEDIES["nudge"], can_apply=boom))
    wo_id, v = submit_fix_order(fleet, gap_class="can-apply-raises", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id=trigger["id"])
    fleet.drain()

    assert len(v.calls) == 1, f"a raising predicate refused: {why_refused(fleet, wo_id)}"
    checklist = v.calls[0]["packet"].self_evolution
    assert MECHANICAL not in checklist
    assert "check 4" in checklist and "the predicate itself failed" in checklist


def test_a_fully_decided_fix_order_still_claims_every_check_mechanically(fleet):
    """The pairing: without it, "the claim is absent" proves nothing about the split"""
    trigger = trigger_order(fleet, status="needs_review",
                            attention="the review could not be satisfied",
                            ack=(IDLE_NO_FINISH_BLOCKER,))
    det, rid = register("fully-decided",
                        {"field": "needs_attention", "op": "eq", "value": True},
                        primitive="lower_attention")
    wo_id, v = submit_fix_order(fleet, gap_class="fully-decided", detector_id=det,
                                remedy_rule_id=rid, trigger_wo_id=trigger["id"])
    fleet.drain()

    assert len(v.calls) == 1, why_refused(fleet, wo_id)
    checklist = v.calls[0]["packet"].self_evolution
    assert MECHANICAL in checklist, v.calls[0]["packet"].self_evolution_unproven
    assert "YOURS TO RAISE" not in checklist


# -- the ops pass-through paths, each of which leaves a note


def proved_pair(fleet, gap: str) -> tuple[dict, dict]:
    """A fix-order row whose checks 1-3 pass, and the trigger it is proved against"""
    trigger = trigger_order(fleet, status="needs_review")
    det, rid = register(gap, {"field": "status", "op": "eq", "value": "needs_review"},
                        primitive="nudge", params={"what": "say something"})
    return ({"id": "wo-direct", "gap_class": gap, "detector_id": det,
             "remedy_rule_id": rid, "trigger_wo_id": trigger["id"]}, trigger)


def test_a_central_store_that_cannot_be_opened_lets_the_submission_through(fleet):
    """The pinned ruling (kn-b7deec8f): an unreadable input may not produce a refusal"""
    row, _ = proved_pair(fleet, "store-unopenable")

    def boom():
        raise RuntimeError("the registry database is locked")

    store = fleet.store()
    notes: list[str] = []
    try:
        original, ops.CentralStore = ops.CentralStore, boom
        try:
            assert ops.fix_order_precheck(store, fleet.project, row,
                                          unproven=notes) is None
        finally:
            ops.CentralStore = original
    finally:
        store.close()
    assert notes and "the registry database is locked" in notes[0]


def test_facts_that_cannot_be_built_leave_a_check_3_note(fleet, monkeypatch):
    row, trigger = proved_pair(fleet, "facts-raise-direct")
    real = rules.facts

    def boom(store, wo, **kw):
        if str(wo.get("id")) == trigger["id"]:
            raise RuntimeError("the state rows could not be read")
        return real(store, wo, **kw)

    monkeypatch.setattr(rules, "facts", boom)
    store = fleet.store()
    notes: list[str] = []
    try:
        assert ops.fix_order_precheck(store, fleet.project, row, unproven=notes) is None
    finally:
        store.close()
    [note] = notes
    assert note.startswith("check 3") and "the state rows could not be read" in note


def test_a_precondition_that_raises_leaves_a_check_4_note(fleet, monkeypatch):
    import dataclasses

    row, _ = proved_pair(fleet, "precondition-raises-direct")

    def boom(store, subject, params):
        raise RuntimeError("the predicate itself failed")

    monkeypatch.setitem(remedies.REMEDIES, "nudge",
                        dataclasses.replace(remedies.REMEDIES["nudge"], can_apply=boom))
    store = fleet.store()
    notes: list[str] = []
    try:
        assert ops.fix_order_precheck(store, fleet.project, row, unproven=notes) is None
    finally:
        store.close()
    [note] = notes
    assert note.startswith("check 4") and "the predicate itself failed" in note


# -- the seam between the registry and the primitives (kn-e3aa04c5)


def test_a_real_seed_rule_drives_a_real_primitive_through_the_precheck(fleet):
    """SEED RULE 1 `stale-panel-hold`, END TO END, stubbing neither side"""
    seed = rules.seed_rows()[0]
    detector_id = seed["detector"]["id"]
    remedy = seed["remedies"][0]
    assert seed["detector"]["gap_class"] == "stale-panel-hold"
    assert remedy["primitive"] == "lower_attention"
    assert remedies.REMEDIES["lower_attention"].params == ()
    assert json.loads(remedy["params"]) == {}

    # The seeded rows are in the real store already; this only asserts that
    central = CentralStore()
    try:
        assert [d["id"] for d in central.detectors_for_gap("stale-panel-hold")] == [
            detector_id]
        assert remedy["id"] in [r["id"] for r in central.remedy_rules_for(detector_id)]
    finally:
        central.close()

    # The recorded state seed rule 1 describes: a `needs_review` order still flagged unsatisfiable an...
    trigger = trigger_order(
        fleet, status="needs_review",
        attention="the review could not be satisfied — the work needs your judgement",
        round_outcome="passed", ack=(IDLE_NO_FINISH_BLOCKER,))

    wo_id, v = dispatch_fix_order(fleet, gap_class="stale-panel-hold",
                                  detector_id=detector_id, remedy_rule_id=remedy["id"],
                                  trigger_wo_id=trigger["id"])
    age_status(fleet, trigger["id"], 7200)
    deliver(fleet, wo_id)
    fleet.drain()

    assert len(v.calls) == 1, (f"the one seed rule that fits was refused by its own "
                               f"gate: {why_refused(fleet, wo_id)}")
    assert [r["outcome"] for r in rounds(fleet, wo_id)] == ["passed"]
