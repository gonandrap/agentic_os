"""A plan submission is not an empty packet. Spec
docs/superpowers/specs/2026-09-25-a-plan-submission-is-not-an-empty-packet.md.

The live case is fo-ff8570fa's planner wo-83e4183c: it submitted a plan the feature order
is already executing off, and sat in `needs_review` with `VALIDATION_STUCK_BLOCKER` for
ever because a plan authors nothing in the planner's worktree. These drive the real
`Daemon._validate_work_order` — spec §11: the defect was never in one function.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.invariants import VALIDATION_STUCK_BLOCKER, true_blockers
from tests.test_feature_orders import ASK, a_plan, child
from tests.test_validation_loop import _settle
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)


def _round(fleet, wo_id):
    store = fleet.store()
    try:
        return dict(store.validation_rounds(wo_id=wo_id)[-1])
    finally:
        store.close()


def planning(fleet) -> dict:
    """A feature order whose planner has been opened, dispatched and has had its turn."""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    fleet.tick()
    store = fleet.store()
    try:
        row = store.get_feature_order(fo["id"])
        assert row["plan_wo_id"], "the daemon opened no planner"
        assert _settle(store, row["plan_wo_id"])
        return row
    finally:
        store.close()


def submitted(fleet) -> dict:
    """A planner that has submitted a plan and had its round judged. Spec §11.1's state,
    which §11.2 starts from."""
    fo = planning(fleet)
    ops.submit_plan(fo["id"], a_plan(child("schema")))
    fleet.drain()
    return fo


# -- §11.1: the void, and the planner that settles off it -------------------------------


def test_a_submitted_plan_voids_its_round_and_settles_its_planner(fleet):
    """THE DEFINITION OF DONE. wo-83e4183c did all of this and was escalated anyway."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    fo = submitted(fleet)

    assert seen.calls == [], "a reviewer was asked to judge a plan Neo already reviews"
    rnd = _round(fleet, fo["plan_wo_id"])
    assert rnd["outcome"] == "void"
    assert fo["id"] in rnd["reason"]

    store = fleet.store()
    try:
        planner = store.get_work_order(fo["plan_wo_id"])
        assert planner["status"] != "needs_review"
        # Re-derived every reconcile tick, so "cleared once" is not the claim that
        # matters — this is. `jarvis wo ack` could not put it down.
        assert VALIDATION_STUCK_BLOCKER not in true_blockers(store, planner)

        ops.review_plan(fo["id"], accept=True, feedback="looks right",
                        decided_by="user")

        planner = store.get_work_order(fo["plan_wo_id"])
        assert planner["status"] == "completed"
        waiting = store.list_work_orders(statuses=("needs_review",))
        assert fo["plan_wo_id"] not in [w["id"] for w in waiting]
    finally:
        store.close()


# -- §11.2: the rejection path over a settled planner -----------------------------------


def test_rejection_still_reaches_a_settled_planner(fleet):
    """A planner that is `completed` rather than `needs_review` still receives a sent-back
    plan: `send_message` revives the session. Spec §9."""
    fleet.daemon.validator = Validator(passed())
    fo = submitted(fleet)

    out = ops.review_plan(fo["id"], accept=False, feedback="split the schema child",
                          decided_by="user")

    assert "delivery_error" not in out
    assert out["delivered"]
    store = fleet.store()
    try:
        queued = store.list_messages(fo["plan_wo_id"])
        assert any("split the schema child" in m["content"] for m in queued)
    finally:
        store.close()


# -- §11.3: `verified` is strict, both directions ---------------------------------------


def test_a_work_order_no_feature_order_points_at_collects_nothing(fleet):
    """Row 2 of `nothing_to_judge` is intact for a work order that really delivered
    nothing — the guard this change must not widen."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    wo = fleet.dispatch("delivered nothing")

    store = fleet.store()
    try:
        assert ops.side_effects_of(store, wo["id"]) == []
    finally:
        store.close()

    finish(fleet, wo["id"], summary="nothing", pr=None)
    fleet.drain()

    assert seen.calls == []
    rnd = _round(fleet, wo["id"])
    assert rnd["outcome"] == "escalated"
    assert "nothing to review" in rnd["reason"]


def test_a_plan_on_a_DIFFERENT_work_order_does_not_attest_this_one(fleet):
    """Otherwise any empty work order finishing while a plan was pending would void
    itself on somebody else's plan — a submitter reaching void by delivering nothing."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    fo = submitted(fleet)
    other = fleet.dispatch("delivered nothing")

    store = fleet.store()
    try:
        assert store.get_feature_order(fo["id"])["plan"], "no plan was stored"
        assert ops.side_effects_of(store, other["id"]) == []
    finally:
        store.close()

    finish(fleet, other["id"], summary="nothing", pr=None)
    fleet.drain()

    assert _round(fleet, other["id"])["outcome"] == "escalated"


def test_a_plan_no_pointer_claims_is_unattested_and_its_round_is_still_judged(fleet,
                                                                             monkeypatch):
    """The OTHER direction of §6: `_plan_effects` collects the plan but sets
    `verified=False` whenever the feature order was reached by its OWN id rather than by
    `plan_wo_id` — the shape `ops.submit_plan` files a plan question with once the planner
    is gone. Unattested is row 3 of `nothing_to_judge`, so such a round is JUDGED by a
    seat and never voided."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    fo = submitted(fleet)
    other = fleet.dispatch("delivered nothing")

    store = fleet.store()
    try:
        effects = ops.side_effects_of(store, fo["id"])
        assert [e["kind"] for e in effects] == ["plan_submitted"]
        assert effects[0]["attested"] is False, "an unclaimed plan attested a packet"
    finally:
        store.close()

    # The same unattested effect on a real round: the registered collector is asked about
    # the feature order's id, so `_plan_effects` itself decides `verified` exactly as it
    # did above — nothing about the attestation is faked here.
    monkeypatch.setattr(ops, "SIDE_EFFECT_COLLECTORS", tuple(
        ops.SideEffectCollector(
            c.name,
            (lambda s, _wo, _fo=fo["id"]: ops._plan_effects(s, _fo))
            if c.name == "plan" else c.collect,
            attested=c.attested)
        for c in ops.SIDE_EFFECT_COLLECTORS))

    finish(fleet, other["id"], summary="nothing", pr=None)
    fleet.drain()

    rnd = _round(fleet, other["id"])
    assert rnd["outcome"] != "void", rnd["reason"]
    assert len(seen.calls) == 1, "an unattested effect never reached a seat"
    assert seen.calls[0]["packet"].side_effects[0]["attested"] is False


# -- §11.4: a planner that also wrote code is judged ------------------------------------


def test_a_planner_that_also_wrote_code_is_judged(fleet):
    """`files` and `pr_url` both outrank the attested test, so this change can never void
    a planner whose code is unmerged. Spec §8."""
    seen = Validator(passed())
    fleet.daemon.validator = seen
    fo = planning(fleet)
    fleet.change(fo["plan_wo_id"], "print('planner code')\n")
    store = fleet.store()
    try:
        store.update_work_order(fo["plan_wo_id"],
                                pr_url="https://github.com/x/y/pull/7")
    finally:
        store.close()

    ops.submit_plan(fo["id"], a_plan(child("codey")))
    fleet.drain()

    assert len(seen.calls) == 1, "a planner's unlanded code was never judged"
    assert _round(fleet, fo["plan_wo_id"])["outcome"] != "void"


# -- the registry entry itself ----------------------------------------------------------


def test_the_plan_collector_is_registered_and_opts_in(fleet):
    by_name = {c.name: c for c in ops.SIDE_EFFECT_COLLECTORS}
    assert by_name["plan"].attested is True


def test_a_submitted_plan_is_one_attested_effect_on_its_planner(fleet):
    fleet.daemon.validator = Validator(passed())
    fo = submitted(fleet)

    store = fleet.store()
    try:
        effects = ops.side_effects_of(store, fo["plan_wo_id"])
    finally:
        store.close()
    assert len(effects) == 1
    effect = effects[0]
    assert effect["kind"] == "plan_submitted"
    assert effect["id"] == fo["id"]
    assert effect["attested"] is True
    assert "verified" not in effect
    assert "1" in effect["summary"]  # one child
    assert "docs/specs/exporter.md" in effect["detail"]


@pytest.mark.parametrize("wo_id", ["wo-nobody", ""])
def test_the_collector_raises_nothing_on_absence(fleet, wo_id):
    """`side_effects_of` swallows nothing, so a raising collector leaves every round
    unjudged for ever. Spec §5."""
    store = fleet.store()
    try:
        assert ops.side_effects_of(store, wo_id) == []
    finally:
        store.close()
