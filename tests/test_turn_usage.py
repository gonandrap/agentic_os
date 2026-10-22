"""Recorded turn usage: the exact accounting the `claude -p --output-format json` result envelope..."""

from __future__ import annotations

import json

import pytest

from jarvis import db
from jarvis import claude_cli, worker_session
from jarvis.project_store import ProjectStore


def result_json(*, is_error: bool = False, cost: float = 0.07195950000000001,
                iterations: list | None = None, **over) -> dict:
    """A result envelope with the exact field shape the CLI emits"""
    if iterations is None:
        iterations = [{
            "input_tokens": 2, "output_tokens": 941,
            "cache_read_input_tokens": 45689, "cache_creation_input_tokens": 2558,
            "cache_creation": {"ephemeral_5m_input_tokens": 0,
                               "ephemeral_1h_input_tokens": 2558},
            "type": "message",
        }]
    data = {
        "is_error": is_error, "duration_api_ms": 15049, "num_turns": 2,
        "stop_reason": "stop_sequence",
        "session_id": "8820ad3c-4908-4594-b47b-5812beb95d2d",
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": 2, "cache_creation_input_tokens": 2558,
            "cache_read_input_tokens": 45689, "output_tokens": 941,
            "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 2558,
                               "ephemeral_5m_input_tokens": 0},
            "inference_geo": "not_available",
            "iterations": iterations,
            "speed": "standard",
        },
        "modelUsage": {
            "claude-opus-5": {
                "inputTokens": 2, "outputTokens": 941,
                "cacheReadInputTokens": 45689, "cacheCreationInputTokens": 2558,
                "webSearchRequests": 0, "costUSD": cost,
                "contextWindow": 1000000, "maxOutputTokens": 64000,
                "canonicalModel": "claude-opus-5", "provider": "firstParty",
            },
        },
        "permission_denials": [], "subtype": "success",
        "result": "the turn's final message", "type": "result",
        "duration_ms": 16743, "uuid": "e565f682-f76e-46ee-b256-70467ecdae73",
    }
    data.update(over)
    return data


# -- deriving the envelope


def test_read_turn_result_carries_the_usage_envelope(tmp_path):
    out = tmp_path / "1.json"
    out.write_text(json.dumps(result_json(is_error=False)))

    r = claude_cli.read_turn_result(out)

    assert r is not None and r.ok
    u = r.usage
    assert u is not None
    assert u["total_cost_usd"] == pytest.approx(0.0719595)
    assert u["input"] == 2
    assert u["cache_write"] == 2558
    assert u["cache_read"] == 45689
    assert u["cache_1h"] == 2558
    assert u["cache_5m"] == 0
    assert u["output"] == 941
    # Absent, not 1: `iterations` samples a turn rather than listing its calls
    assert u["api_calls"] is None
    assert u["context_peak"] == 2 + 45689 + 2558
    assert u["context_window"] == 1000000
    assert u["duration_api_ms"] == 15049
    assert u["cost_by_model"] == {"claude-opus-5": pytest.approx(0.0719595)}


def test_token_totals_come_from_model_usage_not_from_the_usage_object(tmp_path):
    """THE CORRECTION, pinned against the shape that hid it for months"""
    out = tmp_path / "1.json"
    out.write_text(json.dumps(result_json(**{
        "usage": {  # the tail of the turn only, as the CLI reports it
            "input_tokens": 26, "cache_creation_input_tokens": 96_343,
            "cache_read_input_tokens": 950_329, "output_tokens": 11_212,
            "cache_creation": {"ephemeral_1h_input_tokens": 96_343,
                               "ephemeral_5m_input_tokens": 0},
            "iterations": [{"input_tokens": 2, "output_tokens": 947,
                            "cache_read_input_tokens": 109_799,
                            "cache_creation_input_tokens": 2_009}],
        },
        "modelUsage": {  # what the whole turn actually spent
            "claude-opus-5": {
                "inputTokens": 82, "outputTokens": 46_562,
                "cacheReadInputTokens": 2_981_947, "cacheCreationInputTokens": 225_603,
                "costUSD": 4.4267385, "contextWindow": 1_000_000},
        },
    })))

    u = claude_cli.read_turn_result(out).usage

    assert u["input"] == 82
    assert u["cache_write"] == 225_603
    assert u["cache_read"] == 2_981_947
    assert u["output"] == 46_562
    assert u["usage_v"] == claude_cli.USAGE_SCHEMA_VERSION
    # The two things `modelUsage` does not carry still come from `usage`: the ephemeral TTL split (which...
    assert u["cache_1h"] == 96_343 and u["cache_5m"] == 0
    assert u["context_peak"] == 2 + 109_799 + 2_009


def test_a_turn_that_used_two_models_keeps_them_apart(tmp_path):
    """One line per model"""
    out = tmp_path / "1.json"
    out.write_text(json.dumps(result_json(**{"modelUsage": {
        "claude-opus-5": {"inputTokens": 10, "outputTokens": 500,
                          "cacheReadInputTokens": 1_000,
                          "cacheCreationInputTokens": 100, "costUSD": 0.05,
                          "contextWindow": 1_000_000},
        "claude-haiku-4-5-20251001": {"inputTokens": 5, "outputTokens": 50,
                                      "cacheReadInputTokens": 200,
                                      "cacheCreationInputTokens": 20, "costUSD": 0.001,
                                      "contextWindow": 200_000},
    }})))

    u = claude_cli.read_turn_result(out).usage

    assert {m["model"] for m in u["by_model"]} == {"claude-opus-5",
                                                  "claude-haiku-4-5-20251001"}
    assert u["output"] == 550          # the sum, and the split is kept beside it
    assert u["context_window"] == 1_000_000  # the largest, not the last


def test_a_result_with_no_model_usage_says_which_reading_it_is(tmp_path):
    """The fallback is honest rather than absent: those numbers are the old reading"""
    out = tmp_path / "1.json"
    data = result_json()
    data.pop("modelUsage")
    out.write_text(json.dumps(data))

    u = claude_cli.read_turn_result(out).usage

    assert u["usage_v"] == 1
    assert u["cache_read"] == 45689     # from `usage`, because there is nothing else
    assert u["by_model"] == []


def test_context_peak_is_the_max_over_iterations(tmp_path):
    """`iterations` bounds the peak from below"""
    iterations = [
        {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1_000,
         "cache_creation_input_tokens": 500, "type": "message"},
        {"input_tokens": 3, "output_tokens": 900, "cache_read_input_tokens": 80_000,
         "cache_creation_input_tokens": 7_000, "type": "message"},
        {"input_tokens": 1, "output_tokens": 20, "cache_read_input_tokens": 60_000,
         "cache_creation_input_tokens": 100, "type": "message"},
    ]
    out = tmp_path / "1.json"
    out.write_text(json.dumps(result_json(iterations=iterations)))

    u = claude_cli.read_turn_result(out).usage

    assert u["api_calls"] is None, "a count this envelope cannot know is not guessed"
    assert u["iterations_sampled"] == 3
    assert u["context_peak"] == 3 + 80_000 + 7_000


def test_a_result_without_usage_has_no_envelope(tmp_path):
    """Old outfiles (and the fake CLI's minimal replies) predate the envelope"""
    out = tmp_path / "1.json"
    out.write_text(json.dumps({"type": "result", "subtype": "success",
                               "is_error": False, "session_id": "s", "result": "hi"}))

    r = claude_cli.read_turn_result(out)

    assert r is not None and r.ok
    assert r.usage is None


def test_a_failed_turn_still_carries_its_usage(tmp_path):
    """The live example this fixture is copied from: a 429 turn that cost $0.07"""
    out = tmp_path / "1.json"
    out.write_text(json.dumps(result_json(is_error=True)))

    r = claude_cli.read_turn_result(out)

    assert r is not None and not r.ok
    assert r.usage is not None
    assert r.usage["total_cost_usd"] == pytest.approx(0.0719595)


# -- persisting it at reap time


@pytest.fixture()
def store(jarvis_home, project):
    s = ProjectStore(project)
    yield s
    s.close()


def settled_process(store, tmp_path, data: dict) -> dict:
    """A turn whose process has ended and left `data` in its outfile, ready to reap"""
    wo = store.create_work_order("a task", "")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
    outfile = tmp_path / f"{turn['id']}.json"
    outfile.write_text(json.dumps(data))
    # pid NULL + old enough that poll() stops treating it as mid-launch
    store.conn.execute("UPDATE wo_turns SET outfile=?, started_at=? WHERE id=?",
                       (str(outfile), db.now() - 60, turn["id"]))
    return store.get_turn(turn["id"])


def test_reap_records_the_usage_on_a_done_turn(store, tmp_path):
    turn = settled_process(store, tmp_path, result_json(is_error=False))

    settled = worker_session.poll(store)

    assert [t["id"] for t in settled] == [turn["id"]]
    fresh = store.get_turn(turn["id"])
    assert fresh["state"] == "done"
    u = json.loads(fresh["usage_json"])
    assert u["context_peak"] == 48249
    assert u["cache_1h"] == 2558
    assert fresh["cost_usd"] == pytest.approx(0.0719595)


def test_reap_records_usage_and_cost_on_a_failed_turn(store, tmp_path):
    """The old failed path stored no usage at all"""
    turn = settled_process(store, tmp_path, result_json(is_error=True))

    worker_session.poll(store)

    fresh = store.get_turn(turn["id"])
    assert fresh["state"] == "failed"
    assert fresh["usage_json"], "a failed turn's spend went unrecorded"
    u = json.loads(fresh["usage_json"])
    assert u["total_cost_usd"] == pytest.approx(0.0719595)
    assert u["output"] == 941
    assert fresh["cost_usd"] == pytest.approx(0.0719595)
    assert fresh["num_turns"] == 2


# -- a turn's share of a session running total


def turn_file(tmp_path, seq: int, *, own: dict, cumulative: dict,
              cost: float, session: str = "sess-a"):
    """One result JSON in the 2.1.277 shape: `usage` is this turn"""
    out = tmp_path / f"{seq}.json"
    out.write_text(json.dumps(result_json(
        cost=cost,
        session_id=session,
        **{
            "usage": {
                "input_tokens": own["input"],
                "cache_creation_input_tokens": own["cache_write"],
                "cache_read_input_tokens": own["cache_read"],
                "output_tokens": own["output"],
                "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                   "ephemeral_5m_input_tokens": own["cache_write"]},
                "iterations": [],
            },
            "modelUsage": {"claude-opus-5": {
                "inputTokens": cumulative["input"],
                "outputTokens": cumulative["output"],
                "cacheReadInputTokens": cumulative["cache_read"],
                "cacheCreationInputTokens": cumulative["cache_write"],
                "costUSD": cost, "contextWindow": 1_000_000}},
        })))
    return out


def spend(input_=2, cache_write=0, cache_read=0, output=0) -> dict:
    return {"input": input_, "cache_write": cache_write,
            "cache_read": cache_read, "output": output}


def running(turns: list[dict]) -> list[dict]:
    """The cumulative series a resumed session reports, from the turns' own spend"""
    out, total = [], spend(0)
    for own in turns:
        total = {c: total[c] + own[c] for c in total}
        out.append(dict(total))
    return out


def derive_series(tmp_path, turns: list[dict], cumulative: list[dict],
                  costs: list[float], session: str = "sess-a") -> list[dict]:
    """Read a whole conversation the way the OS does"""
    envelopes, previous = [], None
    for i, (own, cum, cost) in enumerate(zip(turns, cumulative, costs), start=1):
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost,
                        session=session)
        result = claude_cli.read_turn_result(out, previous=previous)
        envelopes.append(result.usage)
        previous = result.usage
    return envelopes


def test_a_turn_is_billed_its_delta_not_the_session_running_total(tmp_path):
    """wo-966987af's shape: five turns"""
    turns = [
        spend(2, 1_000_000, 1_000_000, 20_000),
        spend(4, 2_000_000, 37_000_000, 120_000),
        spend(2, 500_000, 4_000_000, 10_000),
        spend(1, 90_000, 700_000, 2_000),
        spend(3, 400_000, 2_550_000, 8_000),
    ]
    cumulative = running(turns)

    envelopes = derive_series(tmp_path, turns, cumulative,
                             [2.28, 25.60, 28.88, 29.87, 32.02])

    for envelope, own in zip(envelopes, turns):
        for cls in own:
            assert envelope[cls] == own[cls], envelope
    # and the identity that matters to a bill: the turns sum to the session, ONCE
    assert sum(e["cache_read"] for e in envelopes) == cumulative[-1]["cache_read"]
    assert sum(e["total_cost_usd"] for e in envelopes) == pytest.approx(32.02)
    assert [e["continues"] for e in envelopes] == [False, True, True, True, True]


def test_a_series_that_restarts_is_read_from_the_file_itself(tmp_path):
    """wo-2005a89b's shape: the running total DROPS mid-order, because the process behind it restarted"""
    first = [spend(2, 500_000, 2_000_000, 20_000),
             spend(4, 1_500_000, 17_000_000, 90_000)]
    second = [spend(2, 100_000, 400_000, 5_000),
              spend(1, 800_000, 10_000_000, 40_000)]
    turns = first + second
    cumulative = running(first) + running(second)

    envelopes = derive_series(tmp_path, turns, cumulative, [2.49, 16.90, 0.33, 6.31])

    assert [e["continues"] for e in envelopes] == [False, True, False, True]
    for envelope, own in zip(envelopes, turns):
        assert envelope["cache_read"] == own["cache_read"]
    assert envelopes[2]["total_cost_usd"] == pytest.approx(0.33)
    assert envelopes[3]["total_cost_usd"] == pytest.approx(5.98)   # 6.31 - 0.33


def test_a_per_turn_envelope_is_never_diffed(tmp_path):
    """THE REGRESSION THE OBVIOUS FIX WOULD HAVE SHIPPED"""
    turns = [spend(9, 3_000_000, 15_000_000, 380_000),
             spend(4, 6_000_000, 28_000_000, 130_000),   # bigger, and still its own
             spend(2, 1_000_000, 3_000_000, 40_000)]

    envelopes = derive_series(tmp_path, turns, turns, [12.23, 20.14, 13.30])

    assert [e["continues"] for e in envelopes] == [False, False, False]
    for envelope, own in zip(envelopes, turns):
        assert envelope["cache_read"] == own["cache_read"]
    assert [e["total_cost_usd"] for e in envelopes] == [12.23, 20.14, 13.30]


def test_a_turn_that_reports_no_usage_keeps_the_delta_it_uncovered(tmp_path):
    """wo-2005a89b turn 4, decided deliberately: an API error returned a `usage` object of all zeroes..."""
    turns = [spend(2, 500_000, 2_000_000, 20_000), spend(0, 0, 0, 0)]
    cumulative = [dict(turns[0]),
                  {"input": 4, "cache_write": 900_000,
                   "cache_read": 9_000_000, "output": 50_000}]

    envelopes = derive_series(tmp_path, turns, cumulative, [2.49, 8.80])

    assert envelopes[1]["continues"] is True
    assert envelopes[1]["cache_read"] == 9_000_000 - 2_000_000
    assert envelopes[1]["total_cost_usd"] == pytest.approx(6.31)


def test_a_different_session_is_never_a_continuation(tmp_path):
    """A running total belongs to one accumulator"""
    turns = [spend(2, 500_000, 2_000_000, 20_000), spend(4, 900_000, 9_000_000, 50_000)]
    cumulative = running(turns)

    first = claude_cli.read_turn_result(
        turn_file(tmp_path, 1, own=turns[0], cumulative=cumulative[0], cost=2.49,
                  session="sess-a")).usage
    second = claude_cli.read_turn_result(
        turn_file(tmp_path, 2, own=turns[1], cumulative=cumulative[1], cost=8.80,
                  session="sess-b"), previous=first).usage

    assert second["continues"] is False
    assert second["cache_read"] == cumulative[1]["cache_read"]


def test_reaping_a_conversation_records_each_turn_and_not_the_session(store, tmp_path):
    """End to end through the store: the seam that hands each reap the turn before it"""
    turns = [spend(2, 500_000, 2_000_000, 20_000),
             spend(4, 1_500_000, 17_000_000, 90_000)]
    cumulative = running(turns)
    wo = store.create_work_order("a conversation", "")
    for i, (own, cum, cost) in enumerate(zip(turns, cumulative, [2.49, 16.90]), start=1):
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost)
        store.conn.execute("UPDATE wo_turns SET outfile=?, started_at=? WHERE id=?",
                           (str(out), db.now() - 60, turn["id"]))
        worker_session.poll(store)

    rows = store.list_turns(wo["id"])
    assert [json.loads(r["usage_json"])["cache_read"] for r in rows] == \
        [own["cache_read"] for own in turns]
    assert [round(r["cost_usd"], 2) for r in rows] == [2.49, 14.41]


def test_the_turn_file_tracks_the_session_total_and_never_the_turn(tmp_path):
    """wo-966987af, measured per turn against its transcript with the turn's own started_at/ended_at as..."""
    session_so_far = [2_020_000, 41_140_000, 45_640_000, 46_430_000, 49_390_000]
    this_turn = [2_170_000, 39_120_000, 4_120_000, 790_000, 2_960_000]
    turns = [spend(0, 0, own, 0) for own in this_turn]
    cumulative = [spend(0, 0, total, 0) for total in session_so_far]

    envelopes = derive_series(tmp_path, turns, cumulative,
                              [2.28, 25.60, 28.88, 29.87, 32.02])

    read = [e["cache_read"] for e in envelopes]
    assert read[2] == 4_500_000, "turn 3 is its own spend, not the session's"
    for got, measured in zip(read, this_turn):
        assert abs(got - measured) < measured * 0.1, (read, this_turn)
    # The identity the bill rests on: the turns sum to the session ONCE
    assert sum(read) == session_so_far[-1]
    assert sum(e["total_cost_usd"] for e in envelopes) == pytest.approx(32.02)


def test_a_turn_keeps_its_own_cost_when_the_dollars_are_not_a_running_total(tmp_path):
    """The two columns are classified SEPARATELY, and this is the case that needs it"""
    turns = [spend(2, 500_000, 2_000_000, 20_000),
             spend(4, 300_000, 1_200_000, 9_000),
             spend(1, 100_000, 400_000, 3_000)]

    envelopes = derive_series(tmp_path, turns, running(turns), [4.40, 1.10, 2.75])

    assert [e["continues"] for e in envelopes] == [False, True, True]
    for envelope, own in zip(envelopes, turns):
        assert envelope["cache_read"] == own["cache_read"], "the tokens still diff"
    assert [e["total_cost_usd"] for e in envelopes] == [4.40, 1.10, 2.75]
    assert [m["cost_usd"] for e in envelopes for m in e["by_model"]] == \
        [4.40, 1.10, 2.75]


def test_a_rising_per_turn_series_is_still_never_diffed(tmp_path):
    """The floor arm ALONE, with nothing else left to decide it"""
    turns = [spend(2, 1_000_000, 4_000_000, 50_000),
             spend(5, 2_000_000, 9_000_000, 90_000),
             spend(7, 3_000_000, 14_000_000, 120_000)]
    for before, after in zip(turns, turns[1:]):
        assert all(after[c] >= before[c] for c in before), "the fixture must be monotone"

    envelopes = derive_series(tmp_path, turns, turns, [4.10, 9.30, 14.05])

    assert [e["continues"] for e in envelopes] == [False, False, False]
    assert [e["cache_read"] for e in envelopes] == [own["cache_read"] for own in turns]
    assert [e["total_cost_usd"] for e in envelopes] == [4.10, 9.30, 14.05]


def test_a_turn_with_no_model_usage_is_never_the_next_turn_s_baseline(tmp_path):
    """A version-1 envelope is not a running total, whatever it carries"""
    first = result_json(cost=0.50, session_id="sess-a")
    first.pop("modelUsage")
    out = tmp_path / "1.json"
    out.write_text(json.dumps(first))
    previous = claude_cli.read_turn_result(out).usage
    assert previous["usage_v"] == 1

    second = claude_cli.read_turn_result(
        turn_file(tmp_path, 2, own=spend(2, 100_000, 900_000, 9_000),
                  cumulative=spend(2, 100_000, 900_000, 9_000), cost=1.40),
        previous=previous).usage

    assert second["continues"] is False
    assert second["total_cost_usd"] == pytest.approx(1.40), "a version-1 cost was diffed"
    assert second["cache_read"] == 900_000
