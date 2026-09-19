"""Recorded turn usage: the exact accounting the `claude -p --output-format json`
result envelope carries, captured for every turn of every work order.

`tests/test_usage.py` covers the transcript *estimator*. This covers the recorded
path that replaces it wherever a turn's result JSON exists: `claude_cli` deriving a
compact usage dict from the envelope, and `worker_session._reap` persisting it on
BOTH outcomes — a failed turn's tokens were spent just the same (the fixture shape
below is copied from a live 429 turn that cost $0.07).
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import claude_cli, worker_session
from jarvis.project_store import ProjectStore


def result_json(*, is_error: bool = False, cost: float = 0.07195950000000001,
                iterations: list | None = None, **over) -> dict:
    """A result envelope with the exact field shape the CLI emits.

    Copied from a live turn (wo-2fa7c0e9, turn 3 — a failed 429 turn, which is the
    point: usage rides on failures too). `iterations` carries one entry per API call,
    each with its own token counts; context at a call = input + cache_read +
    cache_creation of that iteration.
    """
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


# -- deriving the envelope -------------------------------------------------------------


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
    # Absent, not 1: `iterations` samples a turn rather than listing its calls, so the
    # envelope alone cannot count them. `bill._attach_calls` fills this from the
    # transcript, where one assistant message is exactly one API call.
    assert u["api_calls"] is None
    assert u["context_peak"] == 2 + 45689 + 2558
    assert u["context_window"] == 1000000
    assert u["duration_api_ms"] == 15049
    assert u["cost_by_model"] == {"claude-opus-5": pytest.approx(0.0719595)}


def test_token_totals_come_from_model_usage_not_from_the_usage_object(tmp_path):
    """THE CORRECTION, pinned against the shape that hid it for months.

    The turn's totals come from `modelUsage`, which is the COMPLETE accounting: it
    carries what the turn's subagents spent and what any side-model call spent, and the
    top-level `usage` object carries neither. This fixture makes the two DIFFER, because
    a fixture where they agree cannot fail if the wrong one is read — which is exactly
    how the original went unnoticed: every fixture had them equal.

    The "33-60%" this docstring used to argue from was a per-turn delta measured against
    a session running total, and the tests below the multi-turn banner are what settled
    it. Nothing here changes: a single-turn envelope reads the same either way.
    """
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
    # The two things `modelUsage` does not carry still come from `usage`: the ephemeral
    # TTL split (which decides the PRICE of a cache write) and the per-call context.
    assert u["cache_1h"] == 96_343 and u["cache_5m"] == 0
    assert u["context_peak"] == 2 + 109_799 + 2_009


def test_a_turn_that_used_two_models_keeps_them_apart(tmp_path):
    """One line per model. A turn that fell back to a cheaper one is a fact about the
    bill, and a blended total prices half of it at the wrong rate."""
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
    """The fallback is honest rather than absent: those numbers are the old reading,
    they understate the turn, and the version marker is how a reader can tell."""
    out = tmp_path / "1.json"
    data = result_json()
    data.pop("modelUsage")
    out.write_text(json.dumps(data))

    u = claude_cli.read_turn_result(out).usage

    assert u["usage_v"] == 1
    assert u["cache_read"] == 45689     # from `usage`, because there is nothing else
    assert u["by_model"] == []


def test_context_peak_is_the_max_over_iterations(tmp_path):
    """`iterations` bounds the peak from below — every entry is a real call — but it
    does NOT count the turn's calls.

    It was read as one-entry-per-call and it is not: across 199 live result files it
    holds exactly one entry in 196 of them, so `api_calls` reported 1 for an eleven-call
    turn (wo-e23252e4). The count now comes from the transcript, where one assistant
    message is exactly one API call, and is left absent here rather than guessed —
    `bill._attach_calls` fills it in. The peak is still the largest call, never the sum.
    """
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
    """Old outfiles (and the fake CLI's minimal replies) predate the envelope; the
    absence must read as "not recorded", never as zero tokens."""
    out = tmp_path / "1.json"
    out.write_text(json.dumps({"type": "result", "subtype": "success",
                               "is_error": False, "session_id": "s", "result": "hi"}))

    r = claude_cli.read_turn_result(out)

    assert r is not None and r.ok
    assert r.usage is None


def test_a_failed_turn_still_carries_its_usage(tmp_path):
    """The live example this fixture is copied from: a 429 turn that cost $0.07."""
    out = tmp_path / "1.json"
    out.write_text(json.dumps(result_json(is_error=True)))

    r = claude_cli.read_turn_result(out)

    assert r is not None and not r.ok
    assert r.usage is not None
    assert r.usage["total_cost_usd"] == pytest.approx(0.0719595)


# -- persisting it at reap time --------------------------------------------------------


@pytest.fixture()
def store(jarvis_home, project):
    s = ProjectStore(project)
    yield s
    s.close()


def settled_process(store, tmp_path, data: dict) -> dict:
    """A turn whose process has ended and left `data` in its outfile, ready to reap.

    Built directly on the store rather than through a spawned fake process so the
    outfile carries the real envelope shape byte for byte.
    """
    wo = store.create_work_order("a task", "")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
    outfile = tmp_path / f"{turn['id']}.json"
    outfile.write_text(json.dumps(data))
    # pid NULL + old enough that poll() stops treating it as mid-launch
    store.conn.execute("UPDATE wo_turns SET outfile=?, started_at=? WHERE id=?",
                       (str(outfile), time.time() - 60, turn["id"]))
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
    """The old failed path stored no usage at all — yet the live turn this fixture
    copies failed on a 429 having already spent $0.07. Failure must not erase spend."""
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


# -- a turn's share of a session running total ------------------------------------------
#
# From CLI 2.1.277 `modelUsage` and `total_cost_usd` report the WHOLE resumed session
# rather than the turn that just ran, and every fixture above this line is single-turn —
# where the two readings are identical and no test can tell them apart. These are the
# multi-turn ones. Both shapes come from live orders on the dev machine: the monotone
# series from wo-966987af, the mid-series restart from wo-2005a89b (issue #470).


def turn_file(tmp_path, seq: int, *, own: dict, cumulative: dict,
              cost: float, session: str = "sess-a"):
    """One result JSON in the 2.1.277 shape: `usage` is this turn, `modelUsage` is the
    session so far, and `total_cost_usd` is the session's running bill."""
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
    """The cumulative series a resumed session reports, from the turns' own spend."""
    out, total = [], spend(0)
    for own in turns:
        total = {c: total[c] + own[c] for c in total}
        out.append(dict(total))
    return out


def derive_series(tmp_path, turns: list[dict], cumulative: list[dict],
                  costs: list[float], session: str = "sess-a") -> list[dict]:
    """Read a whole conversation the way the OS does — each turn against the one
    before it — and hand back the envelopes."""
    envelopes, previous = [], None
    for i, (own, cum, cost) in enumerate(zip(turns, cumulative, costs), start=1):
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost,
                        session=session)
        result = claude_cli.read_turn_result(out, previous=previous)
        envelopes.append(result.usage)
        previous = result.usage
    return envelopes


def test_a_turn_is_billed_its_delta_not_the_session_running_total(tmp_path):
    """wo-966987af's shape: five turns, a monotone `modelUsage`, and a bill that said
    184.6M for a 49.4M conversation because it summed the running total five times."""
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
    # and the identity that matters to a bill: the turns sum to the session, ONCE.
    assert sum(e["cache_read"] for e in envelopes) == cumulative[-1]["cache_read"]
    assert sum(e["total_cost_usd"] for e in envelopes) == pytest.approx(32.02)
    assert [e["continues"] for e in envelopes] == [False, True, True, True, True]


def test_a_series_that_restarts_is_read_from_the_file_itself(tmp_path):
    """wo-2005a89b's shape: the running total DROPS mid-order, because the process
    behind it restarted. A value lower than its predecessor starts a new series, and
    that file's own figure is the whole of the turn — never a clamp at zero."""
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
    """THE REGRESSION THE OBVIOUS FIX WOULD HAVE SHIPPED. Up to CLI 2.1.274 `modelUsage`
    IS the turn's own spend and equals `usage` — on 93% of the 814 result files on this
    machine, the residue being subagents rather than a fraction. Subtracting there loses
    a median 15% of every historical bill, so a rising series is not enough to call an
    envelope cumulative: the implied delta has to cover the turn's own `usage` too.
    """
    turns = [spend(9, 3_000_000, 15_000_000, 380_000),
             spend(4, 6_000_000, 28_000_000, 130_000),   # bigger, and still its own
             spend(2, 1_000_000, 3_000_000, 40_000)]

    envelopes = derive_series(tmp_path, turns, turns, [12.23, 20.14, 13.30])

    assert [e["continues"] for e in envelopes] == [False, False, False]
    for envelope, own in zip(envelopes, turns):
        assert envelope["cache_read"] == own["cache_read"]
    assert [e["total_cost_usd"] for e in envelopes] == [12.23, 20.14, 13.30]


def test_a_turn_that_reports_no_usage_keeps_the_delta_it_uncovered(tmp_path):
    """wo-2005a89b turn 4, decided deliberately: an API error returned a `usage` object
    of all zeroes while the running total had moved by 10.75M.

    That spend is real — the session transcript contains it — so it is charged to the
    turn whose envelope first reports it rather than dropped. Dropping it would lose
    tokens the OS can prove were spent, and there is no other turn to give them to.
    """
    turns = [spend(2, 500_000, 2_000_000, 20_000), spend(0, 0, 0, 0)]
    cumulative = [dict(turns[0]),
                  {"input": 4, "cache_write": 900_000,
                   "cache_read": 9_000_000, "output": 50_000}]

    envelopes = derive_series(tmp_path, turns, cumulative, [2.49, 8.80])

    assert envelopes[1]["continues"] is True
    assert envelopes[1]["cache_read"] == 9_000_000 - 2_000_000
    assert envelopes[1]["total_cost_usd"] == pytest.approx(6.31)


def test_a_different_session_is_never_a_continuation(tmp_path):
    """A running total belongs to one accumulator. Diffing across session ids would
    subtract one conversation's history from another's."""
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
    """End to end through the store: the seam that hands each reap the turn before it.

    The plumbing is half the defect. `derive_turn_usage` cannot see a work order, so a
    perfect classifier reached by a `_reap` that passes no history would still record
    the whole session on every turn — and `cost_usd` is what a budget is enforced on.
    """
    turns = [spend(2, 500_000, 2_000_000, 20_000),
             spend(4, 1_500_000, 17_000_000, 90_000)]
    cumulative = running(turns)
    wo = store.create_work_order("a conversation", "")
    for i, (own, cum, cost) in enumerate(zip(turns, cumulative, [2.49, 16.90]), start=1):
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
        out = turn_file(tmp_path, i, own=own, cumulative=cum, cost=cost)
        store.conn.execute("UPDATE wo_turns SET outfile=?, started_at=? WHERE id=?",
                           (str(out), time.time() - 60, turn["id"]))
        worker_session.poll(store)

    rows = store.list_turns(wo["id"])
    assert [json.loads(r["usage_json"])["cache_read"] for r in rows] == \
        [own["cache_read"] for own in turns]
    assert [round(r["cost_usd"], 2) for r in rows] == [2.49, 14.41]


def test_the_turn_file_tracks_the_session_total_and_never_the_turn(tmp_path):
    """wo-966987af, measured per turn against its transcript with the turn's own
    started_at/ended_at as the boundaries. Its five result JSONs report

        turn  calls   this turn   session so far   `modelUsage` in the file
           1     21       2.17M            2.17M                     2.02M
           2    152      39.12M           41.14M                    41.14M
           3     16       4.12M           45.26M                    45.64M
           4     13       0.79M           46.05M                    46.43M
           5     32       2.96M           49.01M                    49.39M

    — the file tracks the SESSION column in every row. Turn 3 spent 4.12M and its file
    says 45.64M, which is the reading `USAGE_SCHEMA_VERSION` 2 took at face value.
    """
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
    # The identity the bill rests on: the turns sum to the session ONCE — 49.39M, not
    # the 184.6M that summing the five files gave.
    assert sum(read) == session_so_far[-1]
    assert sum(e["total_cost_usd"] for e in envelopes) == pytest.approx(32.02)


def test_a_turn_keeps_its_own_cost_when_the_dollars_are_not_a_running_total(tmp_path):
    """The two columns are classified SEPARATELY, and this is the case that needs it.

    `kn-da437b27` has jarvis spawning a new process per turn, so a session whose
    `modelUsage` accumulates while `total_cost_usd` does not is a shape the fleet can
    actually produce. Applying the tokens' verdict to the dollars subtracted a larger
    previous bill from a smaller one — and the clamp turned the negative into $0, on the
    one column `budget.spent` enforces. A turn that cost $1.10 is billed $1.10.
    """
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
