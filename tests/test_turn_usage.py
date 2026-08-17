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
    assert u["api_calls"] == 1
    assert u["context_peak"] == 2 + 45689 + 2558
    assert u["context_window"] == 1000000
    assert u["duration_api_ms"] == 15049
    assert u["cost_by_model"] == {"claude-opus-5": pytest.approx(0.0719595)}


def test_token_totals_come_from_model_usage_not_from_the_usage_object(tmp_path):
    """THE CORRECTION, pinned against the shape that hid it for months.

    The result envelope's top-level `usage` object is not the turn's total. Measured
    over the 186 live result files that carry both, it runs at 33-60% of `modelUsage` —
    which agrees to the TOKEN with the transcript `usage.read_session` derives
    independently, and whose `costUSD` sums to `total_cost_usd` to the cent. So the
    totals come from `modelUsage`, and this fixture makes the two DIFFER, because a
    fixture where they agree cannot fail if the wrong one is read (which is exactly how
    the original went unnoticed: every test fixture had them equal).

    Dollars were never wrong — they come from `total_cost_usd` — so what this pins is
    every token figure the OS records. Ruled on question 121, wo-4576667e.
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
    """`iterations` is what gives the exact per-call context size — the /context
    statistic, headlessly. The peak is the largest call, not the sum."""
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

    assert u["api_calls"] == 3
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
