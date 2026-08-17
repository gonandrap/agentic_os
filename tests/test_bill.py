"""The bill: does it add up, and does every token land somewhere.

`tests/test_usage.py` covers the transcript parser, `tests/test_turn_usage.py` the
capture of a turn's own accounting, and `tests/test_cost_report.py` which work orders a
report speaks for. What is left here is the claim the bill page makes in so many words:
that the sum of the parts is the whole, at every level and in both groupings, and that
no charge is quietly dropped on the way.

That claim is the whole feature. A cost surface that is merely PLAUSIBLE is worse than
none — it invites a conclusion about where the tokens went, and the reader has no way to
check it. So the tests here are mostly arithmetic identities rather than renderings, and
they are written against the two things a bill can do wrong: lose a charge, or count one
twice.
"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, bill as bill_mod, ops
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

# The fixtures and builders are shared with the cost-report suite rather than copied:
# two divergent definitions of "a recorded turn" is exactly how two accountings of one
# work order get built.
from tests.test_cost_report import (  # noqa: F401
    add_turn, assistant_row, give_session, recorded_usage, registered, store,
    transcripts,
)


@pytest.fixture()
def wo(store):
    return store.create_work_order("an order with a bill", "")


def os_call(wo_id: str, kind: str = "neo_answer", *, label: str = "question",
            ts: float | None = None, cost: float = 0.02, output: int = 900,
            question_id: int | None = 1) -> None:
    """Record one OS-side call, optionally back-dated to a chosen moment.

    The timestamp is what the turn attribution keys on, so a test about attribution has
    to be able to place a call before, during or after a turn.
    """
    agent_usage.record(kind, project="proj_a", wo_id=wo_id, label=label,
                       model="claude-opus-5", question_id=question_id,
                       usage={"total_cost_usd": cost, "input": 10,
                              "cache_write": 5_000, "cache_read": 20_000,
                              "output": output})
    if ts is not None:
        central = CentralStore()
        try:
            central.conn.execute(
                "UPDATE agent_calls SET ts=? WHERE id=(SELECT MAX(id) FROM agent_calls)",
                (ts,))
            central.conn.commit()
        finally:
            central.close()


def at(store, turn_id: int, started: float, ended: float) -> None:
    """Pin a turn's clock, so a test about attribution is not a test about timing.

    Turns created in the same millisecond leave no room between them for a call to
    land, and "ended_at + 1 second" is often already inside the NEXT turn.
    """
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (started, ended, turn_id))
    store.conn.commit()


def leaves(line: dict) -> list[dict]:
    """Every line with no children under this one — the charges themselves."""
    children = line.get("children") or []
    if not children:
        return [line]
    return [leaf for child in children for leaf in leaves(child)]


# -- the identity the page claims ------------------------------------------------------


def test_the_two_views_and_the_total_are_the_same_tokens(store, wo, transcripts):
    """Turn by turn, actor by actor, and the headline: one set of charges, three sums.

    Built with all three classes of spend on one work order — the worker's own turns,
    what Jarvis spent answering it, and what the worker spawned beneath itself — because
    the failure mode being ruled out is a class that appears in one view and not the
    other.
    """
    give_session(store, wo["id"], "sess-bill")
    transcripts("sess-bill", [assistant_row("m1", write=2_558, read=45_689, out=941)])
    add_turn(store, wo["id"], recorded_usage(0.05))
    os_call(wo["id"], "neo_answer")
    os_call(wo["id"], "panel_seat", label="blast")
    os_call(wo["id"], agent_usage.WORKER_SUBPROCESS, label="pytest", question_id=None)

    b = ops.bill(wo["id"])

    assert b["checks"]["balanced"], b["checks"]["problems"]
    total = b["total"]["tokens"]
    for view in ("actors", "turns"):
        assert sum(line["tokens"]["total"] for line in b[view]) == total["total"], view
    # And all three classes are actually present, so the identity above is not holding
    # because everything landed in one bucket.
    assert {line["key"] for line in b["actors"]} == {"worker", "jarvis", "subprocesses"}


def test_every_charge_appears_exactly_once_in_each_view(store, wo, transcripts):
    """No leaf is dropped, and none is counted twice — the two ways a bill lies."""
    give_session(store, wo["id"], "sess-once")
    transcripts("sess-once", [assistant_row("m1", write=2_558, read=45_689, out=941)])
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], recorded_usage(0.07))
    for i in range(3):
        os_call(wo["id"], "panel_seat", label=f"seat{i}")

    b = ops.bill(wo["id"])

    for view in ("actors", "turns"):
        charged = leaves({"children": b[view]})
        assert sum(leaf["tokens"]["total"] for leaf in charged) \
            == b["total"]["tokens"]["total"], view
        assert sum(leaf["calls"] for leaf in charged) == b["total"]["calls"], view


def test_a_parent_line_is_the_sum_of_its_children_all_the_way_down(store, wo):
    add_turn(store, wo["id"], recorded_usage(0.05))
    for seat in ("premise", "record", "blast"):
        os_call(wo["id"], "panel_seat", label=seat)

    b = ops.bill(wo["id"])

    def check(line):
        children = line.get("children") or []
        if children:
            for cls in ("input", "cache_write", "cache_read", "output"):
                assert sum(c["tokens"][cls] for c in children) == line["tokens"][cls], \
                    f"{line['key']}/{cls}"
            assert sum(c["cost"]["list_usd"] for c in children) == \
                pytest.approx(line["cost"]["list_usd"])
        for child in children:
            check(child)

    for view in ("actors", "turns"):
        for line in b[view]:
            check(line)


# -- where a charge lands --------------------------------------------------------------


def test_an_os_call_lands_on_the_turn_that_asked_for_it(store, wo):
    """The rule Neo set on question 121: the last turn that had STARTED by then.

    A worker asks Neo and ends its turn, so the answer is paid for while nothing is
    running. Attributing by "which turn was live" would put every Neo answer outside the
    turns; attributing to the last turn that started puts it on the turn that asked.
    """
    first = add_turn(store, wo["id"], recorded_usage(0.05))
    second = add_turn(store, wo["id"], recorded_usage(0.06))
    at(store, first["id"], 100, 200)
    at(store, second["id"], 300, 400)
    os_call(wo["id"], "neo_answer", ts=250)   # between them: the question turn 1 asked
    os_call(wo["id"], "digest", label="", ts=350)  # while turn 2 was running

    b = ops.bill(wo["id"])
    by_turn = {line["key"]: line for line in b["turns"]}

    assert "jarvis" in {c["key"].split("/")[-1] for c in by_turn["1"]["children"]}
    assert "jarvis" in {c["key"].split("/")[-1] for c in by_turn["2"]["children"]}
    assert "outside any turn" not in by_turn


def test_a_call_older_than_the_first_turn_is_shown_outside_the_turns(store, wo):
    """Named rather than absorbed. The turns view claims to be exhaustive, so a charge
    it cannot place has to be visible — and still inside the total."""
    turn = add_turn(store, wo["id"], recorded_usage(0.05))
    os_call(wo["id"], "neo_answer", ts=turn["started_at"] - 60)

    b = ops.bill(wo["id"])
    by_turn = {line["key"]: line for line in b["turns"]}

    assert bill_mod.NO_TURN in by_turn
    assert by_turn[bill_mod.NO_TURN]["tokens"]["total"] > 0
    assert b["checks"]["balanced"], b["checks"]["problems"]
    # Last, whatever its timestamp: it is a residue, not the first step of the story.
    assert b["turns"][-1]["key"] == bill_mod.NO_TURN


def test_a_running_turn_is_called_running_not_lost(store, wo, transcripts):
    """A turn writes its result JSON when it ends. Until then its spend is the
    transcript's, and calling that "gone" would read as data loss on every live page."""
    give_session(store, wo["id"], "sess-live")
    transcripts("sess-live", [assistant_row("m1", write=100_000, out=5_000)])
    store.create_turn(wo["id"], kind="dispatch", prompt="p")

    b = ops.bill(wo["id"])
    labels = [leaf["label"] for leaf in leaves({"children": b["actors"]})]

    assert any("still running" in label for label in labels)
    assert not any("no result JSON left" in label for label in labels)


# -- what must NOT be added ------------------------------------------------------------


def test_subagents_are_a_share_of_the_worker_not_a_line_of_their_own(store, wo,
                                                                     transcripts):
    """A subagent's tokens are already inside the turn that spawned it.

    `modelUsage` — the source of a turn's totals — counts them, which is exactly why the
    recorded turns agree with the transcript to the token. Adding the transcript's
    subagent figure as a line would count that spend twice.
    """
    give_session(store, wo["id"], "sess-subs")
    transcripts("sess-subs", [assistant_row("m1", write=2_558, read=45_689, out=941)],
                subagents=[[assistant_row("s1", write=10_000, out=500)]])
    # The turn's recorded envelope COVERS the subagent: `modelUsage` counts every model
    # call the turn made, its subagents' included, which is why the recorded turns of a
    # live work order agree with its transcript (main + subagents) to the token.
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=12_558,
                                   cache_read=45_689, output=1_441))

    b = ops.bill(wo["id"])

    assert b["subagents"]["count"] == 1
    assert b["subagents"]["list_usd"] > 0
    # Reported, and NOT a line: the worker's own total is the transcript's whole total,
    # subagents included, not that total plus them again.
    labels = [leaf["label"] for leaf in leaves({"children": b["actors"]})]
    assert not any("subagent" in label.lower() for label in labels)
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    assert worker["tokens"]["total"] == 12_558 + 45_689 + 1_441
    # One turn, one line: the subagent produced no second charge anywhere.
    assert len(leaves(worker)) == 1


def test_an_unrecorded_turn_is_charged_from_the_transcript_not_dropped(store, wo,
                                                                       transcripts):
    """The hole this closes: a work order whose result JSONs are gone used to report
    only the turns that survived, which on a long order is a fraction of the truth."""
    give_session(store, wo["id"], "sess-gap")
    # The transcript knows the whole conversation; only one turn's envelope survives.
    transcripts("sess-gap", [assistant_row("m1", write=102_558, read=45_689,
                                           out=1_941)])
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], None)

    b = ops.bill(wo["id"])
    labels = {leaf["label"]: leaf for leaf in leaves({"children": b["actors"]})}

    gap = labels["turns with no result JSON left"]
    # Exactly what the recorded turn did not account for — no more, no less.
    assert gap["tokens"]["cache_write"] == 102_558 - 2_558
    assert gap["tokens"]["output"] == 1_941 - 941
    # The whole conversation: the transcript's tokens, plus the 2 fresh input tokens
    # only the recorded turn knows about.
    assert b["total"]["tokens"]["total"] == 102_558 + 45_689 + 1_941 + 2
    assert any("no longer have the result JSON" in note for note in b["notes"])


def test_a_pruned_transcript_never_makes_a_recorded_turn_negative(store, wo,
                                                                  transcripts):
    """The clamp. A transcript smaller than the turns (Claude Code pruned part of it)
    must not produce a negative line that quietly reduces the total."""
    give_session(store, wo["id"], "sess-short")
    transcripts("sess-short", [assistant_row("m1", write=10, out=1)])
    add_turn(store, wo["id"], recorded_usage(0.05))

    b = ops.bill(wo["id"])

    assert b["checks"]["balanced"], b["checks"]["problems"]
    assert all(leaf["tokens"]["total"] >= 0
               for leaf in leaves({"children": b["actors"]}))
    assert b["total"]["tokens"]["cache_write"] == 2_558  # the recorded turn's, intact


# -- the hierarchy above a work order --------------------------------------------------


def test_a_feature_orders_bill_is_its_childrens_bills(store, transcripts):
    """Same shape one level up: the feature's total is its orders' totals, and each
    order keeps its own turn-by-turn detail rather than collapsing to a number."""
    fo = store.create_feature_order("a feature with a bill", "")
    planner = store.create_work_order("plan it", "", kind="planner")
    store.conn.execute("UPDATE feature_orders SET plan_wo_id=? WHERE id=?",
                       (planner["id"], fo["id"]))
    child = store.create_work_order("build it", "", parent_id=fo["id"])
    store.conn.commit()
    add_turn(store, planner["id"], recorded_usage(0.05))
    add_turn(store, child["id"], recorded_usage(0.07))
    os_call(child["id"], "panel_seat", label="blast")

    b = ops.bill(fo["id"])

    assert b["kind"] == "feature_order"
    assert b["checks"]["balanced"], b["checks"]["problems"]
    assert {o["id"] for o in b["orders"]} == {planner["id"], child["id"]}
    assert sum(o["total"]["tokens"]["total"] for o in b["orders"]) \
        == b["total"]["tokens"]["total"]
    # Each order's own turns survive into its own bill — the hierarchy goes all the way
    # down, which is the thing a feature order's cost was missing.
    for order in b["orders"]:
        assert order["turns"]
    # And the feature's by-actor view splits by ORDER, not by merging every child's
    # "turn 1" into one line.
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    assert {c["key"].split("/")[-1] for c in worker["children"]} \
        == {planner["id"], child["id"]}


# -- the version marker ----------------------------------------------------------------


def test_a_stale_envelope_is_re_derived_from_its_outfile(store, wo, tmp_path):
    """Rows written before the modelUsage fix counted a fraction of their turn.

    They are re-derived on read wherever the result JSON survives, because a column
    holding two incompatible counts cannot be summed at all.
    """
    outfile = tmp_path / "1.json"
    outfile.write_text(json.dumps({
        "total_cost_usd": 1.0, "num_turns": 3,
        "usage": {"input_tokens": 1, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 900, "output_tokens": 50,
                  "cache_creation": {"ephemeral_1h_input_tokens": 100,
                                     "ephemeral_5m_input_tokens": 0}},
        "modelUsage": {"claude-opus-5": {
            "inputTokens": 3, "outputTokens": 150, "cacheReadInputTokens": 2_700,
            "cacheCreationInputTokens": 300, "costUSD": 1.0,
            "contextWindow": 1_000_000}},
    }))
    stale = dict(recorded_usage(1.0), usage_v=1, input=1, cache_write=100,
                 cache_read=900, output=50)
    stale.pop("usage_v")  # a real pre-fix row carries no marker at all
    add_turn(store, wo["id"], stale, outfile=str(outfile))

    b = ops.bill(wo["id"])

    # The modelUsage figures, not the top-level `usage` ones it was stored with.
    assert b["total"]["tokens"]["cache_read"] == 2_700
    assert b["total"]["tokens"]["output"] == 150


def test_a_stale_envelope_with_no_outfile_is_kept_and_flagged(store, wo):
    """Marked, never dropped: a wrong number that says how it was counted can be
    labelled on the page, while dropping it reports a turn that certainly cost
    something as having cost nothing. Neo asked for exactly this on question 121.
    """
    add_turn(store, wo["id"], recorded_usage(0.05))  # no `usage_v`, no outfile

    b = ops.bill(wo["id"])
    worker = next(line for line in b["actors"] if line["key"] == "worker")

    assert worker["usage_versions"] == [1]
    assert worker["tokens"]["total"] > 0
