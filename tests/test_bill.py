"""The bill: does it add up, and does every token land somewhere"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, bill as bill_mod, ops, project_store, usage
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore

# The fixtures and builders are shared with the cost-report suite rather than copied: two divergent...
from tests.test_cost_report import (  # noqa: F401
    add_turn, assistant_row, give_session, placeholder_rows, recorded_usage, registered,
    seal_pass, store, transcripts,
)


@pytest.fixture()
def wo(store):
    return store.create_work_order("an order with a bill", "")


def os_call(wo_id: str, kind: str = "neo_answer", *, label: str = "question",
            ts: float | None = None, cost: float = 0.02, output: int = 900,
            question_id: int | None = 1, usage_v: int | None = None) -> None:
    """Record one OS-side call, optionally back-dated to a chosen moment"""
    agent_usage.record(kind, project="proj_a", wo_id=wo_id, label=label,
                       model="claude-opus-5", question_id=question_id,
                       usage={"total_cost_usd": cost, "input": 10,
                              "cache_write": 5_000, "cache_read": 20_000,
                              "output": output}
                       | ({} if usage_v is None else {"usage_v": usage_v}))
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
    """Pin a turn's clock, so a test about attribution is not a test about timing"""
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (started, ended, turn_id))
    store.conn.commit()


def leaves(line: dict) -> list[dict]:
    """Every line with no children under this one — the charges themselves"""
    children = line.get("children") or []
    if not children:
        return [line]
    return [leaf for child in children for leaf in leaves(child)]


# -- the identity the page claims


def test_the_two_views_and_the_total_are_the_same_tokens(store, wo, transcripts):
    """Turn by turn, actor by actor, and the headline: one set of charges, three sums"""
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
    # And all three classes are actually present
    assert {line["key"] for line in b["actors"]} == {"worker", "jarvis", "subprocesses"}


def test_every_charge_appears_exactly_once_in_each_view(store, wo, transcripts):
    """No leaf is dropped, and none is counted twice — the two ways a bill lies"""
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


# -- where a charge lands


def test_an_os_call_lands_on_the_turn_that_asked_for_it(store, wo):
    """The rule Neo set on question 121: the last turn that had STARTED by then"""
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
    """Named rather than absorbed"""
    turn = add_turn(store, wo["id"], recorded_usage(0.05))
    os_call(wo["id"], "neo_answer", ts=turn["started_at"] - 60)

    b = ops.bill(wo["id"])
    by_turn = {line["key"]: line for line in b["turns"]}

    assert bill_mod.NO_TURN in by_turn
    assert by_turn[bill_mod.NO_TURN]["tokens"]["total"] > 0
    assert b["checks"]["balanced"], b["checks"]["problems"]
    # Last, whatever its timestamp: it is a residue, not the first step of the story
    assert b["turns"][-1]["key"] == bill_mod.NO_TURN


def test_a_running_turn_is_called_running_not_lost(store, wo, transcripts):
    """A turn writes its result JSON when it ends"""
    give_session(store, wo["id"], "sess-live")
    transcripts("sess-live", [assistant_row("m1", write=100_000, out=5_000, at=1_005)])
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="p")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (1_000.0, turn["id"]))
    store.conn.commit()

    b = ops.bill(wo["id"])
    labels = [leaf["label"] for leaf in leaves({"children": b["actors"]})]

    assert any("still running" in label for label in labels)
    assert not any("no result JSON left" in label for label in labels)
    assert b["checks"]["balanced"], b["checks"]["problems"]


# -- the agents: a partition, never an addition


def with_a_subagent(store, wo, transcripts, *, started: float = 1_000.0):
    """One recorded turn that spawned one named subagent inside it"""
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
    """The user's ask: subagents are another layer of granularity, not a footnote"""
    with_a_subagent(store, wo, transcripts)

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    turn_line = worker["children"][0]
    rows = {child["label"]: child for child in turn_line["children"]}
    assert set(rows) == {"the lead agent", "Explore · find the login bug"}
    # The subagent's own transcript figures, to the token — not a share of anything
    assert rows["Explore · find the login bug"]["tokens"]["cache_write"] == 10_000
    assert rows["Explore · find the login bug"]["tokens"]["output"] == 500
    # And the lead is the REMAINDER, so the two together are the turn the CLI recorded
    assert rows["the lead agent"]["tokens"]["cache_write"] == 2_558
    assert rows["the lead agent"]["tokens"]["output"] == 941
    assert turn_line["tokens"]["total"] == 12_558 + 45_689 + 1_441


def test_every_agent_added_up_is_the_workers_whole_session(store, wo, transcripts):
    """The identity the user stated, checked in the payload and asserted here"""
    with_a_subagent(store, wo, transcripts)
    os_call(wo["id"], ts=1_010.0)  # the OS half, so the subtraction is not trivially 0

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    others = b["total"]["tokens"]["total"] - worker["tokens"]["total"]
    assert others > 0
    assert sum(line["tokens"]["total"] for line in b["agents"]) == worker["tokens"]["total"]
    assert {line["label"] for line in b["agents"]} == {
        "the lead agent", "Explore · find the login bug"}
    # The lead reads first: a bill is read top-down and the agent that ran the order belongs at the top...
    assert b["agents"][0]["label"] == "the lead agent"
    assert b["checks"]["balanced"]


def test_a_subagent_bigger_than_its_turn_cannot_inflate_the_bill(store, wo,
                                                                 transcripts):
    """The failure mode a partition must not have: growing when you look closer"""
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
    # The turn is what the CLI recorded, not that plus what the subagent claims
    assert turn_line["tokens"]["cache_write"] == 100
    # The subagent took all 100 there were
    assert {c["label"]: c["tokens"]["cache_write"] for c in turn_line["children"]} == {
        "Task": 100}
    assert b["checks"]["balanced"]
    assert any("could not be taken out of the turn" in note for note in b["notes"])
    # The 99,899 tokens the subagent claims beyond the turn are not lost either: the transcript knows the...
    gap = next(c for c in worker["children"] if c["label"] != "turn 1")
    assert gap["tokens"]["cache_write"] == 99_999


# -- what no transcript measured is not the lead's


def with_placeholder_subagent(store, wo, transcripts, *, started: float = 1_000.0,
                              output: int = 100_000, lead_at: float | None = None):
    """One recorded turn: a lead whose three calls are SEALED, a subagent that is not"""
    give_session(store, wo["id"], "sess-ph")
    first = started + 5 if lead_at is None else lead_at
    transcripts(
        "sess-ph",
        [assistant_row("m1", write=1_000, read=5_000, out=6_000, at=first),
         assistant_row("m2", write=1_000, read=5_000, out=7_000, at=first + 1),
         assistant_row("m3", write=1_000, read=5_000, out=7_000, at=first + 2)],
        subagents=[(placeholder_rows("s1", write=1_000, read=50_000, out=5,
                                     at=started + 20),
                    {"agentType": "Explore", "description": "read the code"})])
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=10_000, cache_read=100_000,
                                          output=output, cache_1h=0, cache_5m=10_000))
    at(store, turn["id"], started, started + 60)
    return turn


def agent_rows(b: dict, turn_label: str = "turn 1") -> dict:
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    turn_line = next(c for c in worker["children"] if c["label"] == turn_label)
    return {child["label"]: child for child in turn_line["children"]}


def test_the_lead_is_charged_its_own_calls_and_not_the_remainder(store, wo, transcripts):
    """The defect: the lead row was the turn minus its subagents"""
    with_placeholder_subagent(store, wo, transcripts)

    b = ops.bill(wo["id"])
    rows = agent_rows(b)

    lead = rows[bill_mod.LEAD]
    assert lead["tokens"]["output"] == 20_000          # its own calls, not ~85k
    assert lead["tokens"]["cache_write"] == 3_000
    assert lead["tokens"]["cache_read"] == 15_000
    assert lead["note"] == bill_mod.LEAD_NOTE
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    for cls in ("input", "cache_write", "cache_read", "output", "total"):
        assert sum(line["tokens"][cls] for line in b["agents"]) \
            == worker["tokens"][cls], cls


def test_what_no_transcript_accounts_for_is_its_own_row_not_the_leads(store, wo,
                                                                      transcripts):
    """The residue gets a row that says it is a residue"""
    with_placeholder_subagent(store, wo, transcripts)

    b = ops.bill(wo["id"])
    rows = agent_rows(b)

    residue = rows[bill_mod.UNATTRIBUTED]
    assert residue["tokens"]["output"] == 100_000 - 20_000 - 5
    assert residue["tokens"]["cache_write"] == 10_000 - 3_000 - 1_000
    assert residue["tokens"]["cache_read"] == 100_000 - 15_000 - 50_000
    assert residue["note"] == bill_mod.UNATTRIBUTED_NOTE
    assert residue["calls"] == 0                       # not a countable act
    # Priced in the envelope's own band
    assert residue["models"] == rows[bill_mod.LEAD]["models"]
    # And it is its own agent line, last — the lead reads first, the residue is not one
    assert [line["label"] for line in b["agents"]][-1] == bill_mod.UNATTRIBUTED
    assert bill_mod.reconcile(b)["balanced"], bill_mod.reconcile(b)["problems"]


def test_jarvis_cost_prints_the_residue_and_the_placeholder_sentence(store, wo,
                                                                     transcripts,
                                                                     capsys):
    """§7, the terminal half: both reworded captions, and §9's UNKNOWN-not-zero rule"""
    from jarvis import cli

    with_placeholder_subagent(store, wo, transcripts)
    b = ops.bill(wo["id"], live=True)

    cli._print_bill(b)
    out = capsys.readouterr().out

    assert bill_mod.UNATTRIBUTED in out
    assert "then what no transcript accounts for" in out
    assert bill_mod.PLACEHOLDER_NOTE in out
    # A seal written before the measurement existed has no `placeholder` block
    cli._print_bill({k: v for k, v in b.items() if k != "placeholder"})
    old = capsys.readouterr().out
    assert bill_mod.PLACEHOLDER_NOTE not in old
    assert bill_mod.PLACEHOLDER_ZERO not in old


def test_a_turn_with_no_subagents_is_unchanged(store, wo, transcripts):
    """The regression guard, and the common case: with no subagent the only things the lead's transcript..."""
    give_session(store, wo["id"], "sess-alone")
    transcripts("sess-alone",
                [assistant_row("m1", write=1_000, read=5_000, out=6_000, at=1_005)])
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=10_000, cache_read=100_000,
                                          output=100_000, cache_1h=0, cache_5m=10_000))
    at(store, turn["id"], 1_000.0, 1_060.0)

    b = ops.bill(wo["id"])

    labels = {line["label"] for line in b["agents"]} \
        | {leaf["label"] for leaf in leaves({"children": b["actors"]})} \
        | {leaf["label"] for leaf in leaves({"children": b["turns"]})}
    assert bill_mod.UNATTRIBUTED not in labels
    lead = next(line for line in b["agents"] if line["label"] == bill_mod.LEAD)
    assert [lead["tokens"][cls] for cls in
            ("input", "cache_write", "cache_read", "output")] == \
        [0, 10_000, 100_000, 100_000]
    assert b["checks"]["balanced"], b["checks"]["problems"]


def test_a_pruned_lead_transcript_still_gives_the_lead_the_remainder_and_says_so(
        store, wo, transcripts):
    """§4: with no lead cover there is no figure to split on, so the old behaviour stands"""
    with_placeholder_subagent(store, wo, transcripts, lead_at=100.0)

    b = ops.bill(wo["id"])
    rows = agent_rows(b)

    assert next(r for r in b["turn_rows"] if r["seq"] == 1)["calls_source"] == "envelope"
    assert bill_mod.UNATTRIBUTED not in rows
    lead = rows[bill_mod.LEAD]
    assert lead["tokens"]["output"] == 100_000 - 5     # the whole remainder
    assert lead["note"] == bill_mod.LEAD_UNMEASURED_NOTE


def test_a_lead_transcript_bigger_than_its_turn_cannot_inflate_the_bill(store, wo,
                                                                        transcripts):
    """§5: the clamp, and the excess it must not swallow"""
    with_placeholder_subagent(store, wo, transcripts, output=10_000)

    b = ops.bill(wo["id"])
    rows = agent_rows(b)

    # Clamped to its envelope share: the subagent's 5 first, the lead the 9,995 left
    assert rows[bill_mod.LEAD]["tokens"]["output"] == 10_000 - 5
    # Nothing is left over in the class that was squeezed
    assert rows[bill_mod.UNATTRIBUTED]["tokens"]["output"] == 0
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    turn_line = next(c for c in worker["children"] if c["label"] == "turn 1")
    assert turn_line["tokens"]["output"] == 10_000     # the envelope, not more
    assert any("more than the turn's envelope reports" in note for note in b["notes"])
    # Said about the LEAD, not in the subagent's words
    assert not any("a subagent's own transcript reports" in note for note in b["notes"])


def test_a_running_turn_declares_its_placeholder_calls(store, wo, transcripts):
    """§6: a turn in flight is charged from the transcript"""
    give_session(store, wo["id"], "sess-live-ph")
    transcripts(
        "sess-live-ph",
        [assistant_row("m1", write=100_000, out=5_000, at=1_005)],
        subagents=[(placeholder_rows("s1", write=1_000, read=2_000, out=5, at=1_010),
                    {"agentType": "Task"})])
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="p")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (1_000.0, turn["id"]))
    store.conn.commit()

    b = ops.bill(wo["id"])

    assert b["placeholder"] == {"calls": 1, "output": 5, "main_calls": 0,
                                "subagent_calls": 1}
    line = next(leaf for leaf in leaves({"children": b["actors"]})
                if "still running" in leaf["label"])
    assert bill_mod.PLACEHOLDER_NOTE in line["note"]
    # The half that guards decision 3: the figure is the MAX-merged transcript sum
    assert line["tokens"]["output"] == 5_000 + 5
    assert b["checks"]["balanced"], b["checks"]["problems"]


def test_a_turn_that_spawned_nothing_gets_no_agent_level(store, wo, transcripts):
    """A level that always says "the lead agent" and nothing else is noise"""
    give_session(store, wo["id"], "sess-plain")
    transcripts("sess-plain", [assistant_row("m1", write=12_558, read=45_689, out=1_441)])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=12_558,
                                   cache_read=45_689, output=1_441))

    b = ops.bill(wo["id"])

    worker = next(line for line in b["actors"] if line["key"] == "worker")
    assert worker["children"][0]["children"] == []
    # It still has an agent row in the agents view
    assert [line["label"] for line in b["agents"]] == ["the lead agent"]
    assert b["agents"][0]["tokens"]["total"] == worker["tokens"]["total"]


def test_an_unrecorded_turn_is_charged_from_the_transcript_not_dropped(store, wo,
                                                                       transcripts):
    """The hole this closes: a work order whose result JSONs are gone used to report only the turns that..."""
    give_session(store, wo["id"], "sess-gap")
    # The transcript knows the whole conversation; only one turn's envelope survives
    transcripts("sess-gap", [assistant_row("m1", write=102_558, read=45_689,
                                           out=1_941)])
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], None)

    b = ops.bill(wo["id"])
    labels = {leaf["label"]: leaf for leaf in leaves({"children": b["actors"]})}

    gap = labels["turns with no result JSON left"]
    # Exactly what the recorded turn did not account for — no more, no less
    assert gap["tokens"]["cache_write"] == 102_558 - 2_558
    assert gap["tokens"]["output"] == 1_941 - 941
    # The whole conversation: the transcript's tokens
    assert b["total"]["tokens"]["total"] == 102_558 + 45_689 + 1_941 + 2
    assert any("no longer have the result JSON" in note for note in b["notes"])


def test_a_pruned_transcript_never_makes_a_recorded_turn_negative(store, wo,
                                                                  transcripts):
    """The clamp"""
    give_session(store, wo["id"], "sess-short")
    transcripts("sess-short", [assistant_row("m1", write=10, out=1)])
    add_turn(store, wo["id"], recorded_usage(0.05))

    b = ops.bill(wo["id"])

    assert b["checks"]["balanced"], b["checks"]["problems"]
    assert all(leaf["tokens"]["total"] >= 0
               for leaf in leaves({"children": b["actors"]}))
    assert b["total"]["tokens"]["cache_write"] == 2_558  # the recorded turn's, intact


# -- a conversation the CLI reports as a running total


def cumulative_turns(store, wo, tmp_path, turns: list[dict], costs: list[float],
                     session: str = "sess-running") -> None:
    """Give `wo` one turn per entry, each leaving a 2.1.277-shaped result JSON behind: its own `usage`"""
    from tests.test_turn_usage import running, turn_file

    give_session(store, wo["id"], session)
    for i, (own, cum, cost) in enumerate(zip(turns, running(turns), costs), start=1):
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost,
                        session=session)
        turn = add_turn(store, wo["id"], None, outfile=str(out))
        at(store, turn["id"], 1_000 + 100 * i, 1_090 + 100 * i)


def test_a_multi_turn_bill_totals_the_conversation_and_not_the_running_total(
        store, wo, transcripts, tmp_path):
    """THE IDENTITY THE FIX EXISTS FOR, against the independent source"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60),
             spend(0, 300, 6_000, 40)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5, 3.1])
    transcripts("sess-running", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])

    b = ops.bill(wo["id"])

    assert b["checks"]["balanced"], b["checks"]["problems"]
    whole = sum(sum(own.values()) for own in turns)
    assert b["total"]["tokens"]["total"] == whole
    assert [row["cache_read"] for row in b["turn_rows"]] == \
        [own["cache_read"] for own in turns]
    # The exact dollars follow the same rule: $3.10 for the session, not $6.60
    assert b["total"]["cost"]["exact_usd"] == pytest.approx(3.1)


def test_a_turn_bigger_than_everything_inside_it_fails_the_checks(store, wo,
                                                                  transcripts):
    """The regression guard, stated as arithmetic the bill can check on itself"""
    give_session(store, wo["id"], "sess-inflated")
    first = add_turn(store, wo["id"], dict(recorded_usage(1.0), usage_v=3, input=0,
                                           cache_write=400_000, cache_read=8_000_000,
                                           output=30_000, cache_1h=0,
                                           cache_5m=400_000))
    at(store, first["id"], 1_100, 1_190)
    # Turn 2 reports turn 1's spend as well as its own — the version-2 reading
    second = add_turn(store, wo["id"], dict(recorded_usage(2.5), usage_v=3, input=0,
                                            cache_write=500_000, cache_read=9_000_000,
                                            output=36_000, cache_1h=0,
                                            cache_5m=500_000))
    at(store, second["id"], 1_200, 1_290)
    transcripts("sess-inflated", [
        assistant_row("m1", write=400_000, read=8_000_000, out=30_000, at=1_105),
        assistant_row("m2", write=100_000, read=1_000_000, out=6_000, at=1_205),
    ])

    b = ops.bill(wo["id"])

    assert not b["checks"]["balanced"]
    assert any("everything inside it" in p for p in b["checks"]["problems"])


def lead_line(label: str, output: int, children: list | None = None) -> dict:
    """One bill line carrying `output` tokens and nothing else, for `reconcile`"""
    return {
        "key": label, "label": label, "note": "", "calls": 1, "unit": "turn",
        "tokens": {"input": 0, "cache_write": 0, "cache_read": 0, "output": output,
                   "cache_1h": 0, "cache_5m": 0, "billed_input": 0, "cached_input": 0,
                   "total": output},
        "cost": {"list_usd": 0.0, "exact_usd": 0.0, "by_class": {},
                 "write_rate": 1.25},
        "exact_missing": 0, "models": [], "usage_versions": [],
        "children": children or [],
    }


def lead_payload(charged: int, cover: dict | None) -> dict:
    """A one-turn bill whose lead row is charged `charged` output, over `cover`"""
    lead = lead_line(bill_mod.LEAD, charged)
    worker = lead_line(bill_mod.WORKER, charged, [lead])
    row = {"seq": 1, "recorded": True, "input": 0, "cache_write": 0, "cache_read": 0,
           "output": charged}
    if cover is not None:
        row["calls_cover_by_model"] = cover
    return {
        "total": lead_line("total", charged),
        "turns": [lead_line("1", charged, [worker])],
        "turn_rows": [row],
    }


def test_a_lead_row_can_never_exceed_the_leads_own_transcript(store):
    """§8: the one direction that is a defect rather than something a transcript cannot see"""
    cover = {"claude-opus-5": {"input": 0, "cache_write": 0, "cache_read": 0,
                               "output": 20_000}}

    inflated = bill_mod.reconcile(lead_payload(30_000, cover))
    assert not inflated["balanced"]
    assert "turn 1: the lead agent is charged 30000 output, more than its own API " \
           "calls report (20000)" in inflated["problems"]

    assert bill_mod.reconcile(lead_payload(20_000, cover))["balanced"]
    # §4's fallback: with no cover the lead legitimately holds the remainder
    assert bill_mod.reconcile(lead_payload(30_000, None))["balanced"]


def test_a_seal_counted_as_the_session_is_corrected_downward(store, wo, transcripts,
                                                             tmp_path):
    """The one shrink a seal may take: a correction, not an ageing"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-sealed")
    transcripts("sess-sealed", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    # Seal it the way the release before this one would have: every turn carrying the session's running...
    store.set_status(wo["id"], "completed")
    inflated = ops.bill(wo["id"], live=True)
    for row in inflated["turn_rows"]:
        row["usage_v"] = 2
    inflated["total"]["usage_versions"] = [2]
    inflated["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    inflated["total"]["tokens"] = {k: v * 3 for k, v in
                                   inflated["total"]["tokens"].items()}
    store.seal_bill(wo["id"], json.dumps(inflated))

    b = ops.bill(wo["id"])

    assert b["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert b["total"]["tokens"]["total"] == sum(sum(own.values()) for own in turns)
    assert b["accuracy"]["corrected_from"]["total"] > b["total"]["tokens"]["total"]


def test_a_correction_is_not_blocked_by_an_os_call_stamped_the_old_way(
        store, wo, transcripts, tmp_path):
    """The defect that made the correction above unreachable on every REAL bill"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-os-call")
    transcripts("sess-os-call", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    os_call(wo["id"], ts=1_150, usage_v=2)
    store.set_status(wo["id"], "completed")
    inflated = ops.bill(wo["id"], live=True)
    assert 2 in inflated["total"]["usage_versions"], "the OS call is not stamped"
    for row in inflated["turn_rows"]:
        row["usage_v"] = 2
    inflated["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    inflated["total"]["tokens"] = {k: v * 3 for k, v in
                                   inflated["total"]["tokens"].items()}
    store.seal_bill(wo["id"], json.dumps(inflated))

    b = ops.bill(wo["id"])

    assert b["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert b["accuracy"]["corrected_from"]["total"] > b["total"]["tokens"]["total"]
    # ...and the same stamp no longer makes the bill disclose a ceiling it does not have
    assert b["accuracy"]["gaps"] == []


def test_an_os_call_counted_before_the_modelusage_fix_is_still_disclosed(
        store, wo, transcripts, tmp_path):
    """Version 1 on a CALL is a real under-reading and survives the narrowing"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-v1-call")
    transcripts("sess-v1-call", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    os_call(wo["id"], ts=1_150, usage_v=1)

    b = ops.bill(wo["id"], live=True)

    assert [row["usage_v"] for row in b["turn_rows"]] == [3, 3], "the turns are current"
    assert [gap for gap in b["accuracy"]["gaps"]
            if "lead agent's spend only" in gap], b["accuracy"]["gaps"]
    # ...and it is not the sentence about turns, which is false of this bill
    assert not [gap for gap in b["accuracy"]["gaps"] if "Some turns were counted" in gap]
    assert not b["accuracy"]["complete"]


def test_a_version_one_os_call_does_not_block_the_correction_it_cannot_speak_for(
        store, wo, transcripts, tmp_path):
    """The disclosure above is not a veto: a call the seal already disclosed is not evidence that aged"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-v1-guard")
    transcripts("sess-v1-guard", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    os_call(wo["id"], ts=1_150, usage_v=1)
    store.set_status(wo["id"], "completed")
    inflated = ops.bill(wo["id"], live=True)
    for row in inflated["turn_rows"]:
        row["usage_v"] = 2
    inflated["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    inflated["total"]["tokens"] = {k: v * 3 for k, v in
                                   inflated["total"]["tokens"].items()}
    store.seal_bill(wo["id"], json.dumps(inflated))

    b = ops.bill(wo["id"])

    assert b["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert b["accuracy"]["corrected_from"]["total"] > b["total"]["tokens"]["total"]
    assert [gap for gap in b["accuracy"]["gaps"] if "lead agent's spend only" in gap]


def test_a_seal_that_worded_the_same_gap_the_old_way_still_corrects(
        store, wo, transcripts, tmp_path):
    """Re-wording a disclosure must not read as evidence that aged"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-reworded")
    transcripts("sess-reworded", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    os_call(wo["id"], ts=1_150, usage_v=1)
    store.set_status(wo["id"], "completed")
    inflated = ops.bill(wo["id"], live=True)
    for row in inflated["turn_rows"]:
        row["usage_v"] = 2
    inflated["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    inflated["total"]["tokens"] = {k: v * 3 for k, v in
                                   inflated["total"]["tokens"].items()}
    # What the OLD code wrote for this bill: the same cause, worded for a turn
    inflated["accuracy"]["gaps"] = [
        "Some turns were counted before the modelUsage fix and their result JSON is "
        "gone too, so their tokens are the old, low reading — a floor, not a total."]
    store.seal_bill(wo["id"], json.dumps(inflated))

    b = ops.bill(wo["id"])

    assert b["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert b["accuracy"]["corrected_from"]["total"] > b["total"]["tokens"]["total"]


def test_an_os_call_with_no_usage_version_at_all_counts_as_the_oldest_reading(
        store, wo):
    """The stamp arrived with the modelUsage fix, so a row without one predates it"""
    add_turn(store, wo["id"], recorded_usage(0.05))
    os_call(wo["id"], ts=1_150)   # no `usage_v` at all

    b = ops.bill(wo["id"], live=True)

    assert [gap for gap in b["accuracy"]["gaps"]
            if "lead agent's spend only" in gap], b["accuracy"]["gaps"]
    assert not b["accuracy"]["complete"]


def test_a_corrected_seal_says_what_it_corrected_rather_than_what_it_added(
        store, wo, transcripts, tmp_path, capsys):
    """Both renderers announced every re-derivation as detail 'adopted only because it still saw every..."""
    from jarvis import cli
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-said")
    transcripts("sess-said", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    store.set_status(wo["id"], "completed")
    inflated = ops.bill(wo["id"], live=True)
    for row in inflated["turn_rows"]:
        row["usage_v"] = 2
    inflated["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    inflated["total"]["tokens"] = {k: v * 3 for k, v in
                                   inflated["total"]["tokens"].items()}
    store.seal_bill(wo["id"], json.dumps(inflated))

    cli._print_provenance(ops.bill(wo["id"])["accuracy"])

    printed = capsys.readouterr().out
    assert "saw every token the seal held" not in printed
    assert "the whole session's running total" in printed


def test_the_bills_provenance_renderer_is_not_the_autopsys(store, wo, capsys):
    """TWO FUNCTIONS, TWO NAMES"""
    from jarvis import cli

    cli._print_provenance({"sealed_at": 1_000.0, "gaps": [], "complete": True})
    printed = capsys.readouterr().out

    assert "sealed when this order settled" in printed
    assert cli._print_provenance is not cli._print_autopsy_provenance


def test_a_seal_stands_when_the_shrink_is_a_transcript_that_was_pruned(
        store, wo, transcripts, tmp_path):
    """The other way a bill gets smaller, and it must not pass for a correction"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-pruned")
    # A third turn with no result JSON of its own: only the transcript speaks for it
    third = store.create_turn(wo["id"], kind="message", prompt="p")
    at(store, third["id"], 1_400, 1_490)
    rows = [assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                          out=own["output"], at=1_000 + 100 * i + 5)
            for i, own in enumerate(turns, start=1)]
    rows.append(assistant_row("m3", write=900_000, read=900_000, out=9_000, at=1_405))
    transcripts("sess-pruned", rows)
    # Carrying the same old OS-call stamp as the test above
    os_call(wo["id"], ts=1_150, usage_v=2)
    store.set_status(wo["id"], "completed")
    sealed = ops.bill(wo["id"], live=True)
    for row in sealed["turn_rows"]:
        row["usage_v"] = 2
    sealed["total"]["usage_versions"] = [2]
    sealed["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    store.seal_bill(wo["id"], json.dumps(sealed))
    was = sealed["total"]["tokens"]["total"]
    # ...and then the transcript ages out, taking turn 3's only evidence with it
    (tmp_path / "projects" / "-proj" / "sess-pruned.jsonl").unlink()

    b = ops.bill(wo["id"])

    assert b["total"]["tokens"]["total"] == was, "a pruned transcript rewrote the seal"
    assert "corrected_from" not in b["accuracy"]
    assert b["payload_v"] == bill_mod.PAYLOAD_VERSION - 1


# -- the hierarchy above a work order


def test_a_feature_orders_bill_is_its_childrens_bills(store, transcripts):
    """Same shape one level up: the feature's total is its orders' totals"""
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
    # Each order's own turns survive into its own bill
    for order in b["orders"]:
        assert order["turns"]
    # And the feature's by-actor view splits by ORDER, not by merging every child's "turn 1" into one line
    worker = next(line for line in b["actors"] if line["key"] == "worker")
    assert {c["key"].split("/")[-1] for c in worker["children"]} \
        == {planner["id"], child["id"]}


def test_a_feature_seal_corrects_when_its_childrens_turns_were_read_the_old_way(
        store, transcripts, tmp_path):
    """The same correction one level up, where the turns are a level down"""
    from tests.test_turn_usage import spend

    fo = store.create_feature_order("a feature sealed under the old reading", "")
    child = store.create_work_order("build it", "", parent_id=fo["id"])
    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, child, tmp_path, turns, [1.0, 2.5], session="sess-feature")
    transcripts("sess-feature", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    store.set_status(child["id"], "completed")
    inflated = ops.bill(fo["id"], live=True)
    assert "turn_rows" not in inflated, "a feature carries its turns one level down"
    for order in inflated["orders"]:
        for row in order["turn_rows"]:
            row["usage_v"] = 2
    inflated["payload_v"] = bill_mod.PAYLOAD_VERSION - 1
    inflated["total"]["tokens"] = {k: v * 3 for k, v in
                                   inflated["total"]["tokens"].items()}
    store.seal_bill(fo["id"], json.dumps(inflated), feature=True)

    b = ops.bill(fo["id"])

    assert b["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert b["accuracy"]["corrected_from"]["total"] > b["total"]["tokens"]["total"]


def test_a_feature_discloses_a_floor_its_child_turns_were_counted_at(
        store, transcripts):
    """The other caller reads turns a level down too: narrowing `_accuracy` to turn versions must not lose..."""
    fo = store.create_feature_order("a feature counted before the fix", "")
    child = store.create_work_order("build it", "", parent_id=fo["id"])
    store.conn.commit()
    add_turn(store, child["id"], recorded_usage(0.05))  # no `usage_v`, no outfile

    b = ops.bill(fo["id"])

    assert [gap for gap in b["accuracy"]["gaps"]
            if gap.startswith(child["id"]) and "a floor, not a total" in gap], \
        b["accuracy"]["gaps"]
    assert not b["accuracy"]["complete"]


# -- the version marker


def test_a_stale_envelope_is_re_derived_from_its_outfile(store, wo, tmp_path):
    """Rows written before the modelUsage fix counted a fraction of their turn"""
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

    # The modelUsage figures, not the top-level `usage` ones it was stored with
    assert b["total"]["tokens"]["cache_read"] == 2_700
    assert b["total"]["tokens"]["output"] == 150


def test_a_stale_envelope_with_no_outfile_is_kept_and_flagged(store, wo):
    """Marked, never dropped: a wrong number that says how it was counted can be labelled on the page"""
    add_turn(store, wo["id"], recorded_usage(0.05))  # no `usage_v`, no outfile

    b = ops.bill(wo["id"])
    worker = next(line for line in b["actors"] if line["key"] == "worker")

    assert worker["usage_versions"] == [1]
    assert worker["tokens"]["total"] > 0


# -- the seal: a bill is worked out once, when the order settles


def test_a_settled_orders_bill_is_frozen_and_survives_the_evidence(store, wo,
                                                                   transcripts,
                                                                   monkeypatch):
    """The defect this closes: a bill computed on demand gets CHEAPER as it ages"""
    give_session(store, wo["id"], "sess-seal")
    transcripts("sess-seal", [assistant_row("m1", write=12_558, read=45_689, out=1_441)])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=12_558,
                                   cache_read=45_689, output=1_441))
    store.set_status(wo["id"], "completed")

    sealed = bill_mod.seal("proj_a", store.project_path, store.get_work_order(wo["id"]))
    before = sealed["total"]["tokens"]["total"]
    assert before == 12_558 + 45_689 + 1_441

    # Now take the evidence away, exactly as Claude Code's own pruning does
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(store.project_path / "gone"))
    store.conn.execute("UPDATE wo_turns SET usage_json=NULL, outfile=''")
    store.conn.commit()

    assert ops.bill(wo["id"])["total"]["tokens"]["total"] == before
    assert ops.bill(wo["id"])["accuracy"]["sealed_at"]
    # ...and the proof that the seal is doing the work: recomputing now finds nothing
    assert ops.bill(wo["id"], live=True)["total"]["tokens"]["total"] == 0


def test_the_daemon_seals_every_settled_order_and_only_once(store, wo, project,
                                                            transcripts):
    """Sealed by the reconcile tick rather than at each of the six places an order can settle"""
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
    # An open order is not sealed: it has not finished spending
    assert store.get_work_order(open_wo["id"])["bill_json"] is None
    # Idempotent — and cheap, because a sealed order leaves the queue for good
    assert store.unsealed_terminal_orders() == []
    was = sealed["bill_sealed_at"]
    daemon.seal_bills(project, store)
    assert store.get_work_order(wo["id"])["bill_sealed_at"] == was


def _settle_reopen_settle(store, wo, tmp_path, transcripts):
    """Sealed at turn 1, then resumed and settled again with turn 2 spent (#870)"""
    give_session(store, wo["id"], "sess-again")
    first = assistant_row("m1", write=1_000, out=10)
    transcripts("sess-again", [first])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=1_000,
                                   cache_read=0, output=10))
    store.set_status(wo["id"], "failed")
    seal_pass(tmp_path, store)
    store.set_status(wo["id"], "running")
    transcripts("sess-again", [first, assistant_row("m2", write=500, out=5)])
    add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0, cache_write=500,
                                   cache_read=0, output=5))
    store.set_status(wo["id"], "completed")


def test_a_seal_taken_before_the_last_settlement_is_re_derived(store, wo, tmp_path,
                                                               transcripts):
    _settle_reopen_settle(store, wo, tmp_path, transcripts)
    assert ops.bill(wo["id"])["total"]["tokens"]["cache_write"] == 1_000

    seal_pass(tmp_path, store)

    assert ops.bill(wo["id"])["total"]["tokens"]["cache_write"] == 1_500
    assert store.resettled_sealed_orders() == []


def test_a_resettled_seal_whose_evidence_is_gone_is_kept_not_retried(
        store, wo, tmp_path, transcripts, monkeypatch):
    _settle_reopen_settle(store, wo, tmp_path, transcripts)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(store.project_path / "gone"))
    store.conn.execute("UPDATE wo_turns SET usage_json=NULL, outfile=''")
    store.conn.commit()

    seal_pass(tmp_path, store)

    assert ops.bill(wo["id"])["total"]["tokens"]["cache_write"] == 1_000
    assert store.resettled_sealed_orders() == []


def test_an_unsealed_old_order_is_costed_live_and_says_what_it_lost(store, wo):
    """The user's rule for orders that predate the seal: a simplified bill"""
    add_turn(store, wo["id"], recorded_usage(0.05))  # pre-fix row, no outfile
    store.set_status(wo["id"], "completed")

    b = ops.bill(wo["id"])

    assert b["accuracy"]["live"] is True
    assert b["accuracy"]["sealed_at"] is None
    assert b["accuracy"]["complete"] is False
    assert any("modelUsage" in gap for gap in b["accuracy"]["gaps"])


# -- the grain under a turn: one row per API call


def test_a_turns_api_calls_are_itemised_and_sum_back_to_the_turn(store, wo, transcripts):
    """The defect the user found: a turn read 517k of context it never held at once"""
    give_session(store, wo["id"], "sess-calls")
    # `peak=1_205` is what the envelope's one sampled iteration saw
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05, peak=1_205), input=0,
                                          cache_write=1_500, cache_read=2_200,
                                          output=180, cache_1h=0, cache_5m=1_500))
    # Three calls, one conversation growing under them — the real shape of a turn
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
    # The identity that makes the view trustworthy: the calls ARE the turn
    for cls, total in (("cache_write", 1_500), ("cache_read", 2_200), ("output", 180)):
        assert sum(c[cls] for c in row["call_rows"]) == total == row[cls]
    # ...and the context peak is what ONE call carried, never their sum
    assert row["context_peak"] == 1_500
    assert ops.bill(wo["id"], live=True)["checks"]["balanced"]


def test_a_turn_with_more_calls_than_the_cap_says_what_it_folded(store, wo,
                                                                 transcripts,
                                                                 monkeypatch):
    """A capped list that just stops reads as a complete one"""
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
    # The dearest are kept, because "which call cost that" is the question this answers
    assert [c["cache_write"] for c in row["call_rows"]] == [30, 40]
    assert row["calls_folded"]["count"] == 2
    assert row["calls_folded"]["cache_write"] == 10 + 20
    # Nothing is lost: what is shown plus what is folded is still the whole turn
    shown = sum(c["cache_write"] for c in row["call_rows"])
    assert shown + row["calls_folded"]["cache_write"] == 100


def test_api_calls_is_absent_rather_than_guessed_when_the_transcript_is_gone(store, wo):
    """`iterations` in a result envelope is a SAMPLE of a turn, not a list of its calls"""
    add_turn(store, wo["id"], dict(recorded_usage(0.05), api_calls=None))
    store.set_status(wo["id"], "completed")

    row = ops.bill(wo["id"], live=True)["turn_rows"][0]

    assert row["api_calls"] is None
    assert row["calls_source"] == "envelope"
    assert "call_rows" not in row


def test_the_cache_write_rate_is_the_one_paid_not_the_range(store, wo):
    """The user's complaint: a line whose tokens were all 5-minute said `1.25-2x`"""
    add_turn(store, wo["id"], dict(recorded_usage(0.05), cache_write=1_000,
                                   cache_1h=0, cache_5m=1_000))
    b = ops.bill(wo["id"], live=True)
    assert bill_mod.write_rate_of(b["total"]) == pytest.approx(1.25)
    assert "5-minute" in bill_mod.rate_note(b["total"])

    # An all-1h line is the other end, and it must not read as 1.25x either
    wo2 = store.create_work_order("an hour", "")
    add_turn(store, wo2["id"], dict(recorded_usage(0.05), cache_write=1_000,
                                    cache_1h=1_000, cache_5m=0))
    b2 = ops.bill(wo2["id"], live=True)
    assert bill_mod.write_rate_of(b2["total"]) == pytest.approx(2.0)

    # And a line the CLI reported no split for charges the floor, and SAYS it is a floor
    wo3 = store.create_work_order("unknown", "")
    add_turn(store, wo3["id"], dict(recorded_usage(0.05), cache_write=1_000,
                                    cache_1h=0, cache_5m=0))
    b3 = ops.bill(wo3["id"], live=True)
    assert bill_mod.write_rate_of(b3["total"]) == pytest.approx(1.25)
    assert "floor" in bill_mod.rate_note(b3["total"])


def test_an_old_seal_is_re_derived_while_the_evidence_survives(store, wo, transcripts):
    """Sealing froze the numbers — and, until now, the SHAPE with them"""
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

    # Age the seal back to the shape this release replaces
    stale = {k: v for k, v in sealed.items() if k != "payload_v"}
    for row in stale["turn_rows"]:
        row.pop("call_rows", None)
        row["api_calls"] = 1
    store.seal_bill(wo["id"], json.dumps(stale), at=was_sealed_at)

    upgraded = ops.bill(wo["id"])

    assert upgraded["payload_v"] == bill_mod.PAYLOAD_VERSION
    assert upgraded["turn_rows"][0]["api_calls"] == 2
    assert upgraded["accuracy"]["resealed_at"]
    # The seal TIME is preserved: re-deriving the payload did not move when it settled
    assert store.get_work_order(wo["id"])["bill_sealed_at"] == was_sealed_at
    # Written back, so the next reader gets it without recomputing
    assert json.loads(store.get_work_order(wo["id"])["bill_json"])["payload_v"] == \
        bill_mod.PAYLOAD_VERSION


def test_an_old_seal_stands_once_the_evidence_is_gone(store, wo, transcripts,
                                                      monkeypatch):
    """The rule that keeps the upgrade safe: a bill must never shrink when re-read"""
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


def test_an_old_seal_is_not_restaled_by_the_new_keys(store, wo, transcripts):
    """§9's decided tradeoff: the attribution keys are ADDITIVE at version 5"""
    give_session(store, wo["id"], "sess-v5")
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=1_000, cache_read=600, output=50,
                                          cache_1h=0, cache_5m=1_000))
    transcripts("sess-v5", [assistant_row("m1", write=1_000, read=600, out=50,
                                          at=turn["started_at"] + 1)])
    store.set_status(wo["id"], "completed")
    sealed = bill_mod.seal("proj_a", store.project_path, store.get_work_order(wo["id"]))
    at_sealed = store.get_work_order(wo["id"])["bill_sealed_at"]

    # A seal from before this release: version 5, and none of the new keys
    stale = {k: v for k, v in sealed.items() if k != "placeholder"}
    for row in stale["turn_rows"]:
        row.pop("calls_cover_by_model", None)
    store.seal_bill(wo["id"], json.dumps(stale), at=at_sealed)
    order = store.get_work_order(wo["id"])

    assert bill_mod._upgrade_seal("proj_a", store.project_path, order,
                                  json.loads(order["bill_json"]), feature=False) is None
    served = ops.bill(wo["id"])
    assert "placeholder" not in served                 # unknown, and rendered as nothing
    assert served["total"]["tokens"] == sealed["total"]["tokens"]
    assert "resealed_at" not in served["accuracy"]     # no transcript was re-read


def test_a_bill_without_a_catalog_fails_loudly_rather_than_guessing(store, wo,
                                                                    transcripts):
    """No catalog, no classification — and the error says which key is missing"""
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
    """The other way a catalog goes missing: it is there and it is wrong"""
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


# -- the observability class


def observability_call(wo_id: str, kind: str = "observe_why", *,
                       ts: float | None = None) -> None:
    """One metered look at the order — the envelope the meter writes: wall clock, zeros"""
    agent_usage.record(kind, project="proj_a", wo_id=wo_id,
                       usage={"wall_ms": 12, "total_cost_usd": 0.0, "input": 0,
                              "cache_write": 0, "cache_read": 0, "output": 0})
    if ts is not None:
        central = CentralStore()
        try:
            central.conn.execute(
                "UPDATE agent_calls SET ts=? WHERE id=(SELECT MAX(id) FROM agent_calls)",
                (ts,))
            central.conn.commit()
        finally:
            central.close()


def test_looking_at_an_order_is_its_own_class_costing_nothing(store, wo):
    """Its own class, balanced, and `0.00`"""
    observability_call(wo["id"], "observe_why")
    observability_call(wo["id"], "observe_live")

    b = ops.bill(wo["id"])
    by_actor = {line["key"]: line for line in b["actors"]}

    assert set(by_actor) == {bill_mod.OBSERVE}
    line = by_actor[bill_mod.OBSERVE]
    assert line["cost"]["list_usd"] == 0.0 and line["tokens"]["total"] == 0
    assert {child["label"] for child in line["children"]} == {
        agent_usage.describe("observe_why"), agent_usage.describe("observe_live")}
    assert b["checks"]["balanced"], b["checks"]["problems"]
    assert b["total"]["cost"]["list_usd"] == 0.0
    # A zero-token line prices its cache writes at the floor rather than dividing by zero
    assert bill_mod.write_rate_of(line) > 0


def test_an_order_nobody_looked_at_says_not_recorded_and_never_zero(store, wo, capsys):
    """Absent is not zero, here too (Neo's ruling on question 766)"""
    from jarvis import cli

    add_turn(store, wo["id"], recorded_usage(0.05))

    b = ops.bill(wo["id"])
    absent = dict(bill_mod.absent_notes(b))

    assert bill_mod.OBSERVE in absent
    assert "not recorded" in absent[bill_mod.OBSERVE]
    assert "0.00" not in absent[bill_mod.OBSERVE]

    cli.main(["cost", wo["id"]])
    out = capsys.readouterr().out
    assert absent[bill_mod.OBSERVE] in out


def test_a_metered_look_has_no_reading_so_it_discloses_no_under_reading(store, wo,
                                                                       tmp_path,
                                                                       transcripts):
    """Its envelope carries NO `usage_v`"""
    from tests.test_turn_usage import spend

    turns = [spend(0, 2_000, 1_000, 100), spend(0, 500, 4_000, 60)]
    cumulative_turns(store, wo, tmp_path, turns, [1.0, 2.5], session="sess-v3-look")
    transcripts("sess-v3-look", [
        assistant_row(f"m{i}", write=own["cache_write"], read=own["cache_read"],
                      out=own["output"], at=1_000 + 100 * i + 5)
        for i, own in enumerate(turns, start=1)
    ])
    observability_call(wo["id"], "observe_inspect", ts=1_150)

    b = ops.bill(wo["id"], live=True)

    assert [row["usage_v"] for row in b["turn_rows"]] == [3, 3], "the turns are current"
    assert not [gap for gap in b["accuracy"]["gaps"]
                if "lead agent's spend only" in gap], b["accuracy"]["gaps"]
    central = CentralStore()
    try:
        (row,) = [r for r in central.agent_calls(wo_id=wo["id"])
                  if agent_usage.is_observability(r["kind"])]
    finally:
        central.close()
    envelope = json.loads(row["usage_json"])
    assert envelope["wall_ms"] == 12 and "usage_v" not in envelope


# -- the re-write tax, labelled by side (spec 2026-10-02 §1.5)


def two_sided(store, wo, transcripts) -> None:
    """A session whose MAIN side pays a re-write tax and whose subagent cannot"""
    give_session(store, wo["id"], "sess-sides")
    transcripts(
        "sess-sides",
        [assistant_row("m1", write=60_000, read=50_000, out=10, at=1_001),
         assistant_row("m2", write=60_000, read=1_000, out=10, at=1_002),
         assistant_row("m3", write=60_000, read=2_000, out=10, at=1_003)],
        subagents=[[assistant_row("s1", write=5_000, read=100_000, out=5, at=1_005),
                    assistant_row("s2", write=5_000, read=120_000, out=5, at=1_006)]])
    turn = add_turn(store, wo["id"], dict(recorded_usage(0.05), input=0,
                                          cache_write=190_000, cache_read=273_000,
                                          output=40))
    at(store, turn["id"], 1_000.0, 1_060.0)


def test_the_rewrite_tax_names_the_subagent_side_and_its_zero(store, wo, transcripts):
    """The unlabelled rollup read as "subagent cache spend is negligible\""""
    two_sided(store, wo, transcripts)

    rewrite = ops.bill(wo["id"], live=True)["rewrite"]
    side = rewrite["by_side"]

    assert side["main"]["tokens"] == rewrite["tokens"] > 0
    assert side["main"]["boundaries"] == rewrite["boundaries"] == 1
    assert side["subagent"]["tokens"] == 0
    assert side["subagent"]["boundaries"] == 0
    assert side["subagent"]["cache_write"] == 10_000
    assert side["subagent"]["count"] == 1
    assert side["subagent"]["structural_zero"] is True
    # The TOTAL is untouched: `jarvis cost`'s headline arithmetic must not move
    assert rewrite["cache_write"] == (side["main"]["cache_write"]
                                      + side["subagent"]["cache_write"])


def test_the_structural_zero_is_printed_with_what_the_subagents_wrote(store, wo,
                                                                      transcripts,
                                                                      capsys):
    from jarvis import cli

    two_sided(store, wo, transcripts)

    cli._print_bill(ops.bill(wo["id"], live=True))
    out = capsys.readouterr().out

    assert "a STRUCTURAL zero" in out and "10,000" in out
    assert "1 subagent(s) still wrote" in out


def test_a_non_zero_subagent_side_prints_the_two_sides_and_no_note(store, wo,
                                                                  transcripts, capsys):
    """A real finding, so the sentence above would be a lie about it (§1.5)"""
    from jarvis import cli

    two_sided(store, wo, transcripts)
    payload = ops.bill(wo["id"], live=True)
    payload["rewrite"]["by_side"]["subagent"].update(
        tokens=400, boundaries=1, structural_zero=False)

    cli._print_bill(payload)
    out = capsys.readouterr().out

    assert "a STRUCTURAL zero" not in out
    assert "main " in out and "subagent 400" in out


def test_the_absent_sentences_have_one_source(store, wo):
    """The four sentences were duplicated in `cli._print_bill` and in bill.html"""
    observability_call(wo["id"])

    keys = dict(bill_mod.absent_notes(ops.bill(wo["id"])))

    assert bill_mod.OBSERVE not in keys
    assert set(keys) == {bill_mod.WORKER, bill_mod.JARVIS, bill_mod.SUBPROC}
    assert set(bill_mod.ABSENT_NOTES) == {bill_mod.WORKER, bill_mod.JARVIS,
                                          bill_mod.SUBPROC, bill_mod.OBSERVE}


# -- a compaction is counted once


def compact_turn(store, wo_id: str, usage: dict | None, state: str = "done") -> dict:
    """A `compact` turn row: the OS's `/compact`, recorded as a turn like any other"""
    turn = store.create_turn(wo_id, kind=project_store.COMPACT_TURN, prompt="/compact")
    if state == "running":
        return store.get_turn(turn["id"])
    store.finish_turn(turn["id"], state, result="r",
                      cost_usd=usage["total_cost_usd"] if usage else None,
                      num_turns=1,
                      usage_json=json.dumps(usage) if usage else None)
    return store.get_turn(turn["id"])


def compaction_call(wo_id: str, ts: float | None = None, cost: float = 0.3) -> None:
    os_call(wo_id, agent_usage.COMPACTION, label="compaction", ts=ts, cost=cost,
            question_id=None)


COMPACTION_TOKENS = 10 + 5_000 + 20_000 + 900
TURN_TOKENS = 2 + 2_558 + 45_689 + 941


def test_compaction_is_not_charged_to_the_worker(store, wo):
    """One compaction, one charge: on the Jarvis half, never on both"""
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], recorded_usage(0.07))
    compact_turn(store, wo["id"], recorded_usage(0.3))
    compaction_call(wo["id"])

    b = ops.bill(wo["id"])
    actors = {line["key"]: line for line in b["actors"]}

    assert b["total"]["tokens"]["total"] == 2 * TURN_TOKENS + COMPACTION_TOKENS
    assert actors["worker"]["tokens"]["total"] == 2 * TURN_TOKENS
    assert actors["jarvis"]["tokens"]["total"] == COMPACTION_TOKENS


def test_compaction_keeps_its_turn_line(store, wo):
    """The turn does not disappear from the by-turn table; it changes actor"""
    first = add_turn(store, wo["id"], recorded_usage(0.05))
    at(store, first["id"], 1_000, 1_090)
    compact = compact_turn(store, wo["id"], recorded_usage(0.3))
    at(store, compact["id"], 1_100, 1_190)
    compaction_call(wo["id"], ts=1_150)

    b = ops.bill(wo["id"])
    line = next(line for line in b["turns"] if line["key"] == str(compact["seq"]))

    assert [child["key"] for child in line["children"]] \
        == [f"{compact['seq']}/{bill_mod.JARVIS}"]
    assert line["tokens"]["total"] == COMPACTION_TOKENS


def test_compaction_does_not_mask_a_transcript_gap(store, wo, transcripts):
    """`recorded` inflated by a compaction swallowed a real gap under `max(0, …)`"""
    give_session(store, wo["id"], "sess-compact-gap")
    transcripts("sess-compact-gap",
                [assistant_row("m1", write=20_000, read=45_689, out=941)])
    add_turn(store, wo["id"], recorded_usage(0.05))
    add_turn(store, wo["id"], None)
    compact_turn(store, wo["id"], dict(recorded_usage(0.3), cache_write=100_000))

    b = ops.bill(wo["id"])
    labels = {leaf["label"]: leaf for leaf in leaves({"children": b["actors"]})}

    gap = labels["turns with no result JSON left"]
    assert gap["tokens"]["cache_write"] == 20_000 - 2_558


def test_in_flight_compaction_does_not_mislabel_the_gap(store, wo, transcripts):
    """A running compact turn is not one of the worker's unrecorded turns"""
    give_session(store, wo["id"], "sess-compact-live")
    transcripts("sess-compact-live",
                [assistant_row("m1", write=50_000, out=2_000, at=1_005)])
    lost = add_turn(store, wo["id"], None)
    at(store, lost["id"], 1_000, 1_090)
    running = compact_turn(store, wo["id"], None, state="running")
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (1_100.0, running["id"]))
    store.conn.commit()

    b = ops.bill(wo["id"])
    labels = {leaf["label"]: leaf for leaf in leaves({"children": b["actors"]})}

    gap = labels["turns with no result JSON left"]
    assert gap["calls"] == 1


def test_only_compact_turns_reads_as_no_turns_on_record(store, wo, transcripts):
    """An order whose only row is a compaction has no worker turns at all"""
    give_session(store, wo["id"], "sess-compact-only")
    transcripts("sess-compact-only",
                [assistant_row("m1", write=50_000, read=1_000, out=2_000)])
    compact_turn(store, wo["id"], recorded_usage(0.3))

    b = ops.bill(wo["id"])
    labels = {leaf["label"]: leaf for leaf in leaves({"children": b["actors"]})}

    assert "the conversation, from its transcript" in labels
    assert "turns with no result JSON left" not in labels


def _double_counted(*, worker_child: bool, jarvis_child: bool) -> dict:
    """A payload shaped like the defect: a compact turn with one or both children"""
    seq = 3
    children = []
    if worker_child:
        children.append(bill_mod._line(f"{seq}/{bill_mod.WORKER}", "the worker"))
    if jarvis_child:
        jarvis = bill_mod._line(f"{seq}/{bill_mod.JARVIS}", "jarvis")
        jarvis["children"].append(bill_mod._line(
            f"{seq}/{bill_mod.JARVIS}/{agent_usage.describe(agent_usage.COMPACTION)}",
            agent_usage.describe(agent_usage.COMPACTION)))
        children.append(jarvis)
    line = bill_mod._line(str(seq), f"turn {seq}")
    line["children"] = children
    return {
        "total": bill_mod._line("total", "total"),
        "turns": [line],
        "actors": [],
        "turn_rows": [{"seq": seq, "kind": project_store.COMPACT_TURN,
                       "recorded": True}],
    }


def test_double_counted_compaction_fails_reconcile():
    """A check the fold invariants cannot make: `wo_turns` against `agent_calls`"""
    both = bill_mod.reconcile(_double_counted(worker_child=True, jarvis_child=True))

    assert not both["balanced"]
    assert any("turn 3" in p and "counted twice" in p for p in both["problems"])
    # The worker child alone is a pruned `agent_calls` row; the Jarvis child alone is the fixed state
    for kwargs in ({"worker_child": True, "jarvis_child": False},
                   {"worker_child": False, "jarvis_child": True}):
        checks = bill_mod.reconcile(_double_counted(**kwargs))
        assert checks["balanced"], checks["problems"]


def test_shortfall_check_survives_a_compaction(store, wo, transcripts):
    """A compaction sets no `calls_cover`, which disabled the shortfall check whole"""
    give_session(store, wo["id"], "sess-compact-short")
    compact = compact_turn(store, wo["id"], recorded_usage(0.3))
    at(store, compact["id"], 1_000, 1_090)
    inflated = add_turn(store, wo["id"], dict(recorded_usage(2.5), usage_v=3, input=0,
                                              cache_write=500_000,
                                              cache_read=9_000_000, output=36_000,
                                              cache_1h=0, cache_5m=500_000))
    at(store, inflated["id"], 1_200, 1_290)
    transcripts("sess-compact-short", [
        assistant_row("m1", write=100_000, read=1_000_000, out=6_000, at=1_205),
    ])

    b = ops.bill(wo["id"])

    assert not b["checks"]["balanced"]
    assert any("everything inside it" in p for p in b["checks"]["problems"])
