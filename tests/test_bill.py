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

from jarvis import agent_usage, bill as bill_mod, ops, usage
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


# -- the agents: a partition, never an addition ---------------------------------------


def with_a_subagent(store, wo, transcripts, *, started: float = 1_000.0):
    """One recorded turn that spawned one named subagent inside it.

    The turn's own envelope COVERS the subagent — `modelUsage` counts every model call
    the turn made, its subagents' included, which is exactly why the recorded turns of a
    live work order agree with its transcript to the token. So the subagent's row has to
    come OUT of the turn, and this fixture is built so that a bill which added it
    instead would be visibly, arithmetically wrong.
    """
    give_session(store, wo["id"], "sess-subs")
    transcripts(
        "sess-subs",
        [assistant_row("m1", write=2_558, read=45_689, out=941, at=started + 1)],
        subagents=[([assistant_row("s1", write=10_000, out=500, at=started + 5)],
                    {"agentType": "Explore", "description": "find the login bug"})])
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=12_558, cache_read=45_689,
                                          output=1_441))
    at(store, turn["id"], started, started + 60)
    return turn


def test_a_subagent_gets_its_own_row_inside_the_turn_that_spawned_it(store, wo,
                                                                     transcripts):
    """The user's ask: subagents are another layer of granularity, not a footnote.

    Named from the meta file Claude Code writes beside the transcript, so the row says
    what the subagent was FOR — "Explore · find the login bug" — and not just that some
    agent cost some tokens.
    """
    with_a_subagent(store, wo, transcripts)

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    turn_line = worker["children"][0]
    rows = {child["label"]: child for child in turn_line["children"]}
    assert set(rows) == {"the lead agent", "Explore · find the login bug"}
    # The subagent's own transcript figures, to the token — not a share of anything.
    assert rows["Explore · find the login bug"]["tokens"]["cache_write"] == 10_000
    assert rows["Explore · find the login bug"]["tokens"]["output"] == 500
    # And the lead is the REMAINDER, so the two together are the turn the CLI recorded.
    assert rows["the lead agent"]["tokens"]["cache_write"] == 2_558
    assert rows["the lead agent"]["tokens"]["output"] == 941
    assert turn_line["tokens"]["total"] == 12_558 + 45_689 + 1_441


def test_every_agent_added_up_is_the_workers_whole_session(store, wo, transcripts):
    """The identity the user stated, checked in the payload and asserted here.

    "Sum the usage of all agents for a work order and it equals the total minus the OS's
    usage" — which is the worker's actor line, since the OS's half of an order is what
    Jarvis spent on it plus the claude processes the worker launched itself.
    """
    with_a_subagent(store, wo, transcripts)
    os_call(wo["id"], ts=1_010.0)  # the OS half, so the subtraction is not trivially 0

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    others = b["total"]["tokens"]["total"] - worker["tokens"]["total"]
    assert others > 0
    assert sum(line["tokens"]["total"] for line in b["agents"]) == worker["tokens"]["total"]
    assert {line["label"] for line in b["agents"]} == {
        "the lead agent", "Explore · find the login bug"}
    # The lead reads first: a bill is read top-down and the agent that ran the order
    # belongs at the top of its own list.
    assert b["agents"][0]["label"] == "the lead agent"
    assert b["checks"]["balanced"]


def test_a_subagent_bigger_than_its_turn_cannot_inflate_the_bill(store, wo,
                                                                 transcripts):
    """The failure mode a partition must not have: growing when you look closer.

    Two sources disagree here — the subagent's transcript claims more than the turn's
    own envelope contains — and the rule is that the TURN is the budget. The subagent
    takes what is there, the lead gets nothing, and the excess is reported rather than
    added.
    """
    give_session(store, wo["id"], "sess-big")
    transcripts("sess-big", [assistant_row("m1", write=100, at=1_001)],
                subagents=[([assistant_row("s1", write=99_999, out=1, at=1_005)],
                            {"agentType": "Task"})])
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=100,
                                          cache_read=0, output=0))
    at(store, turn["id"], 1_000.0, 1_060.0)

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    turn_line = next(c for c in worker["children"] if c["label"] == "turn 1")
    # The turn is what the CLI recorded, not that plus what the subagent claims. Its
    # rows are a partition of it: the subagent takes the 100 there are, the lead gets 0.
    assert turn_line["tokens"]["cache_write"] == 100
    # The subagent took all 100 there were, so there is no lead row: a line charging
    # nothing is not a fact about the order, it is a row of zeroes.
    assert {c["label"]: c["tokens"]["cache_write"] for c in turn_line["children"]} == {
        "Task": 100}
    assert b["checks"]["balanced"]
    assert any("could not be taken out of the turn" in note for note in b["notes"])
    # The 99,899 tokens the subagent claims beyond the turn are not lost either: the
    # transcript knows the session spent them, and the gap line is where a session that
    # spent more than its recorded turns has always been charged.
    gap = next(c for c in worker["children"] if c["label"] != "turn 1")
    assert gap["tokens"]["cache_write"] == 99_999


def test_a_turn_that_spawned_nothing_gets_no_agent_level(store, wo, transcripts):
    """A level that always says "the lead agent" and nothing else is noise.

    The same rule the per-model level follows: a breakdown earns its place by
    distinguishing something from something.
    """
    give_session(store, wo["id"], "sess-plain")
    transcripts("sess-plain", [assistant_row("m1", write=12_558, read=45_689, out=1_441)])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=12_558,
                                   cache_read=45_689, output=1_441))

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    assert worker["children"][0]["children"] == []
    # It still has an agent row in the agents view — one agent ran it, and a view that
    # omitted the only agent of a plain order would break the identity above.
    assert [line["label"] for line in b["agents"]] == ["the lead agent"]
    assert b["agents"][0]["tokens"]["total"] == worker["tokens"]["total"]


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


# -- the seal: a bill is worked out once, when the order settles -----------------------


def test_a_settled_orders_bill_is_frozen_and_survives_the_evidence(store, wo,
                                                                   transcripts,
                                                                   monkeypatch):
    """The defect this closes: a bill computed on demand gets CHEAPER as it ages.

    Claude Code prunes session transcripts and result JSONs on its own schedule, so an
    order costed live months later reports whatever happens to be left — a smaller
    number, with no indication that it is smaller. The bill is sealed when the order
    settles and read back from the seal after that.
    """
    give_session(store, wo["id"], "sess-seal")
    transcripts("sess-seal", [assistant_row("m1", write=12_558, read=45_689, out=1_441)])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=12_558,
                                   cache_read=45_689, output=1_441))
    store.set_status(wo["id"], "completed")

    sealed = bill_mod.seal("proj_a", store.project_path, store.get_work_order(wo["id"]))
    before = sealed["total"]["tokens"]["total"]
    assert before == 12_558 + 45_689 + 1_441

    # Now take the evidence away, exactly as Claude Code's own pruning does.
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(store.project_path / "gone"))
    store.conn.execute("UPDATE wo_turns SET usage_json=NULL, outfile=''")
    store.conn.commit()

    assert ops.bill(wo["id"])["total"]["tokens"]["total"] == before
    assert ops.bill(wo["id"])["accuracy"]["sealed_at"]
    # ...and the proof that the seal is doing the work: recomputing now finds nothing.
    assert ops.bill(wo["id"], live=True)["total"]["tokens"]["total"] == 0


def test_the_daemon_seals_every_settled_order_and_only_once(store, wo, project,
                                                            transcripts):
    """Sealed by the reconcile tick rather than at each of the six places an order can
    settle — one place to get right, and it catches the orders that settled before this
    existed, while their evidence is still on disk."""
    from jarvis.daemon import Daemon

    give_session(store, wo["id"], "sess-tick")
    transcripts("sess-tick", [assistant_row("m1", write=1_000, out=10)])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=1_000,
                                   cache_read=0, output=10))
    open_wo = store.create_work_order("still running", "")
    store.set_status(open_wo["id"], "running")
    store.set_status(wo["id"], "completed")

    daemon = Daemon.__new__(Daemon)
    daemon.seal_bills(project, store)

    sealed = store.get_work_order(wo["id"])
    assert sealed["bill_json"] and sealed["bill_sealed_at"]
    # An open order is not sealed: it has not finished spending.
    assert store.get_work_order(open_wo["id"])["bill_json"] is None
    # Idempotent — and cheap, because a sealed order leaves the queue for good.
    assert store.unsealed_terminal_orders() == []
    was = sealed["bill_sealed_at"]
    daemon.seal_bills(project, store)
    assert store.get_work_order(wo["id"])["bill_sealed_at"] == was


def test_an_unsealed_old_order_is_costed_live_and_says_what_it_lost(store, wo):
    """The user's rule for orders that predate the seal: a simplified bill, with a
    disclaimer that it is inaccurate — never a smaller number presented as a total."""
    add_turn(store, wo["id"], recorded_usage(0.05))  # pre-fix row, no outfile
    store.set_status(wo["id"], "completed")

    b = ops.bill(wo["id"])

    assert b["accuracy"]["live"] is True
    assert b["accuracy"]["sealed_at"] is None
    assert b["accuracy"]["complete"] is False
    assert any("modelUsage" in gap for gap in b["accuracy"]["gaps"])


# -- the grain under a turn: one row per API call ---------------------------------------


def test_a_turns_api_calls_are_itemised_and_sum_back_to_the_turn(store, wo, transcripts):
    """The defect the user found: a turn read 517k of context it never held at once.

    A turn is an agent LOOP — the model answers, a tool runs, the model is called again
    with the result appended — and every call re-sends the conversation. So a turn's
    cache-read is a SUM over its calls, and with the turn as the finest grain there was
    no way to see that from the bill. Each call now has a row, and they are a PARTITION
    of the turn: they add up to it exactly, class by class.
    """
    give_session(store, wo["id"], "sess-calls")
    # `peak=1_205` is what the envelope's one sampled iteration saw — a real call, so a
    # real lower bound, but not the turn's largest. The transcript knows the largest.
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05, peak=1_205), input=0,
                                          cache_write=1_500, cache_read=2_200,
                                          output=180, cache_1h=0, cache_5m=1_500))
    # Three calls, one conversation growing under them — the real shape of a turn. Their
    # timestamps have to fall INSIDE the turn: a call is charged to the last turn that
    # had started when it landed, the same rule everything else on the bill uses.
    began = turn["started_at"]
    transcripts("sess-calls", [
        assistant_row("m1", write=1_000, read=0, out=50, at=began + 1),
        assistant_row("m2", write=200, read=1_000, out=60, at=began + 2),
        assistant_row("m3", write=300, read=1_200, out=70, at=began + 3),
    ])
    store.set_status(wo["id"], "completed")

    row = ops.bill(wo["id"], live=True)["turn_rows"][0]

    assert row["api_calls"] == 3, "the count comes from the calls, not from a sample"
    assert row["calls_source"] == "transcript"
    assert [c["cache_read"] for c in row["call_rows"]] == [0, 1_000, 1_200]
    # The identity that makes the view trustworthy: the calls ARE the turn.
    for cls, total in (("cache_write", 1_500), ("cache_read", 2_200), ("output", 180)):
        assert sum(c[cls] for c in row["call_rows"]) == total == row[cls]
    # ...and the context peak is what ONE call carried, never their sum — raised here
    # above the envelope's sampled 1_205 by the largest call the transcript knows of.
    assert row["context_peak"] == 1_500
    assert ops.bill(wo["id"], live=True)["checks"]["balanced"]


def test_a_turn_with_more_calls_than_the_cap_says_what_it_folded(store, wo,
                                                                 transcripts,
                                                                 monkeypatch):
    """A capped list that just stops reads as a complete one.

    The cap exists because these rows are SEALED — a long turn would grow the seal
    without bound — but what it drops has to be counted and shown, or the page quietly
    understates a turn while looking exhaustive.
    """
    monkeypatch.setattr(bill_mod, "TURN_CALL_LIMIT", 2)
    give_session(store, wo["id"], "sess-cap")
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=100, cache_read=20, output=4,
                                          cache_1h=0, cache_5m=100))
    transcripts("sess-cap", [
        assistant_row(f"m{i}", write=10 * i, read=5, out=1,
                      at=turn["started_at"] + i)
        for i in range(1, 5)
    ])
    store.set_status(wo["id"], "completed")

    row = ops.bill(wo["id"], live=True)["turn_rows"][0]

    assert row["api_calls"] == 4, "the COUNT is never capped, only the rows"
    assert len(row["call_rows"]) == 2
    # The dearest are kept, because "which call cost that" is the question this answers.
    assert [c["cache_write"] for c in row["call_rows"]] == [30, 40]
    assert row["calls_folded"]["count"] == 2
    assert row["calls_folded"]["cache_write"] == 10 + 20
    # Nothing is lost: what is shown plus what is folded is still the whole turn.
    shown = sum(c["cache_write"] for c in row["call_rows"])
    assert shown + row["calls_folded"]["cache_write"] == 100


def test_api_calls_is_absent_rather_than_guessed_when_the_transcript_is_gone(store, wo):
    """`iterations` in a result envelope is a SAMPLE of a turn, not a list of its calls.

    Measured over 199 live result files it holds exactly one entry in 196 of them, so
    reading its length reported `1` for an eleven-call turn. A count that cannot be
    known is now left absent — a dash on the page — rather than asserted wrongly.
    """
    add_turn(store, wo["id"], dict(recorded_usage(0.05), api_calls=None))
    store.set_status(wo["id"], "completed")

    row = ops.bill(wo["id"], live=True)["turn_rows"][0]

    assert row["api_calls"] is None
    assert row["calls_source"] == "envelope"
    assert "call_rows" not in row


def test_the_cache_write_rate_is_the_one_paid_not_the_range(store, wo):
    """The user's complaint: a line whose tokens were all 5-minute said `1.25-2x`.

    The range is the price LIST. What a reader of one line wants is the price PAID, and
    it is already implied by the dollar figure beside it — the two disagreeing on the
    same row is what made the range unreadable.
    """
    add_turn(store, wo["id"], dict(recorded_usage(0.05), cache_write=1_000,
                                   cache_1h=0, cache_5m=1_000))
    b = ops.bill(wo["id"], live=True)
    assert bill_mod.write_rate_of(b["total"]) == pytest.approx(1.25)
    assert "5-minute" in bill_mod.rate_note(b["total"])

    # An all-1h line is the other end, and it must not read as 1.25x either.
    wo2 = store.create_work_order("an hour", "")
    add_turn(store, wo2["id"], dict(recorded_usage(0.05), cache_write=1_000,
                                    cache_1h=1_000, cache_5m=0))
    b2 = ops.bill(wo2["id"], live=True)
    assert bill_mod.write_rate_of(b2["total"]) == pytest.approx(2.0)

    # And a line the CLI reported no split for charges the floor, and SAYS it is a floor.
    wo3 = store.create_work_order("unknown", "")
    add_turn(store, wo3["id"], dict(recorded_usage(0.05), cache_write=1_000,
                                    cache_1h=0, cache_5m=0))
    b3 = ops.bill(wo3["id"], live=True)
    assert bill_mod.write_rate_of(b3["total"]) == pytest.approx(1.25)
    assert "floor" in bill_mod.rate_note(b3["total"])


def test_an_old_seal_is_re_derived_while_the_evidence_survives(store, wo, transcripts):
    """Sealing froze the numbers — and, until now, the SHAPE with them.

    A bill sealed before per-call rows existed could never show them, not even on an
    order whose transcript was still on disk. So the payload carries a version and an
    older one is re-derived once, keeping the original seal time.
    """
    give_session(store, wo["id"], "sess-upgrade")
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=1_000, cache_read=600, output=50,
                                          cache_1h=0, cache_5m=1_000))
    transcripts("sess-upgrade", [
        assistant_row("m1", write=600, read=0, out=20, at=turn["started_at"] + 1),
        assistant_row("m2", write=400, read=600, out=30, at=turn["started_at"] + 2),
    ])
    store.set_status(wo["id"], "completed")
    sealed = bill_mod.seal("proj_a", store.project_path, store.get_work_order(wo["id"]))
    was_sealed_at = store.get_work_order(wo["id"])["bill_sealed_at"]

    # Age the seal back to the shape this release replaces.
    stale = {k: v for k, v in sealed.items() if k != "payload_v"}
    for row in stale["turn_rows"]:
        row.pop("call_rows", None)
        row["api_calls"] = 1
    store.seal_bill(wo["id"], json.dumps(stale), at=was_sealed_at)

    upgraded = ops.bill(wo["id"])

    assert upgraded["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert upgraded["turn_rows"][0]["api_calls"] == 2
    assert upgraded["accuracy"]["resealed_at"]
    # The seal TIME is preserved: re-deriving the payload did not move when it settled.
    assert store.get_work_order(wo["id"])["bill_sealed_at"] == was_sealed_at
    # Written back, so the next reader gets it without recomputing.
    assert json.loads(store.get_work_order(wo["id"])["bill_json"])["payload_v"] == \
        bill_mod.PAYLOAD_VERSION


def test_an_old_seal_stands_once_the_evidence_is_gone(store, wo, transcripts,
                                                      monkeypatch):
    """The rule that keeps the upgrade safe: a bill must never shrink when re-read.

    An upgrade is adopted only if recomputing still sees every token the seal holds. The
    moment Claude Code prunes the transcript, it does not — and the old seal, shape and
    all, is the better record.
    """
    give_session(store, wo["id"], "sess-pruned")
    transcripts("sess-pruned", [assistant_row("m1", write=9_000, read=0, out=100)])
    add_turn(store, wo["id"], None)  # no envelope: the transcript is the only source
    store.set_status(wo["id"], "completed")
    sealed = bill_mod.seal("proj_a", store.project_path, store.get_work_order(wo["id"]))
    before = sealed["total"]["tokens"]["total"]
    assert before == 9_100

    stale = {k: v for k, v in sealed.items() if k != "payload_v"}
    store.seal_bill(wo["id"], json.dumps(stale),
                    at=store.get_work_order(wo["id"])["bill_sealed_at"])
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(store.project_path / "gone"))

    b = ops.bill(wo["id"])

    assert b["total"]["tokens"]["total"] == before, "the seal stood"
    assert "payload_v" not in b, "and it was NOT re-derived from nothing"
    assert not b["accuracy"].get("resealed_at")


def test_a_bill_without_a_catalog_fails_loudly_rather_than_guessing(store, wo,
                                                                    transcripts):
    """No catalog, no classification — and the error says which key is missing.

    A bill splits its re-write tax by cause against `os.cold_prefix_floor`, and there is
    deliberately no default anywhere to fall back on: a report that classified every
    boundary against a threshold nobody configured would print a finding the
    configuration never produced. Paired with the same call succeeding once the catalog
    is back, so this cannot pass against a bill that is simply broken.
    """
    from jarvis.central_store import CentralStore

    give_session(store, wo["id"], "sess-nocat")
    transcripts("sess-nocat", [assistant_row("m1", write=9_000, read=0, out=100)])
    add_turn(store, wo["id"], None)

    central = CentralStore()
    try:
        kept = central.get_state("catalog_path")
        central.set_state("catalog_path", "")
        central.conn.commit()
    finally:
        central.close()

    with pytest.raises(ops.OpsError, match="no catalog registered"):
        ops.bill(wo["id"])

    central = CentralStore()
    try:
        central.set_state("catalog_path", kept)
        central.conn.commit()
    finally:
        central.close()
    assert ops.bill(wo["id"])["total"]["tokens"]["total"] == 9_100


def test_a_catalog_that_cannot_be_parsed_fails_the_bill_too(store, wo, transcripts,
                                                            tmp_path):
    """The other way a catalog goes missing: it is there and it is wrong.

    Same rule — the bill must not fall through to a guessed threshold — but a different
    exception path, which is why it is asserted separately rather than assumed.
    """
    from jarvis.central_store import CentralStore

    give_session(store, wo["id"], "sess-badcat")
    transcripts("sess-badcat", [assistant_row("m1", write=9_000, read=0, out=100)])
    add_turn(store, wo["id"], None)

    broken = tmp_path / "broken.json"
    broken.write_text('{"os": {"cold_prefix_floor": "not a number"}, "projects": []}')
    central = CentralStore()
    try:
        central.set_state("catalog_path", str(broken))
        central.conn.commit()
    finally:
        central.close()

    with pytest.raises(Exception, match="cold_prefix_floor"):
        ops.bill(wo["id"])
