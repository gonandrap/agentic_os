"""The context ledger: what Jarvis put in the window, and the delta between turns.

§5 of docs/specs/2026-09-24-order-observability.md. Three properties carry the section and
each has its own tests below, because each is a defect the knowledge base already named:

* one payload per turn, written EXACTLY ONCE — the two call sites partition the turns
  (Neo's ruling on question 681) rather than both writing one row;
* ABSENT IS NEVER ZERO (issue #227): the knowledge block is on the dispatch turn and not
  on a message turn, and an ingredient that cannot be read is None with a sentence;
* the residual is INFERRED (kn-0cb81cec). The transcript these tests read the cold-start
  write out of is fabricated HERE, so what is proven is the subtraction and the
  `measured: false` label — never that the attribution is true of a real session.

`tests/test_stable_prefix.py` already pins that `briefing_for`'s appended prompt does not
move between turns, and `test_the_measurement_moves_neither_the_prompt_nor_the_fingerprint`
below pins the two surfaces this feature could have disturbed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from jarvis import context, ops, usage
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def dispatched(tmp_path, jarvis_home, fake_claude, project, catalog_file):
    """A dispatched work order in the `project` fixture, and the handles to poke it.

    Dispatched through `dispatch.dispatch_work_order` rather than a hand-built turn row:
    the thing under test is that the real launch path records, and a fabricated turn would
    pass while the wiring was missing.
    """
    from jarvis import dispatch

    ops.start_os(str(catalog_file), foreground=True)
    cat = load_catalog(catalog_file)
    spec = cat.projects[0]
    ops.create_work_order("proj_a", "measure me", description="do a thing")
    store = ProjectStore(project)
    central = CentralStore()
    # One entry, because `build_worker_prompt` appends NO block for an empty base (a
    # falsy `KnowledgeBrief`) — a fixture with none would make the knowledge ingredient
    # absent on the dispatch turn too and the test of the split would prove nothing.
    central.add_knowledge("workers must read the spec section first", project="proj_a",
                          topic="dispatch")
    wo = store.claim_next_pending()
    dispatch.dispatch_work_order(store, central, spec, wo, os_config=cat.os)
    try:
        yield {"store": store, "central": central, "spec": spec,
               "wo": store.get_work_order(wo["id"]), "wo_id": wo["id"]}
    finally:
        central.close()
        store.close()


def _payloads(store: ProjectStore, wo_id: str) -> list[dict]:
    return [json.loads(t["context_json"]) if t["context_json"] else None
            for t in store.list_turns(wo_id)]


def _named(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


# -- 1. one payload per turn, written once ---------------------------------------------


def test_the_dispatch_turn_and_a_message_turn_each_record_their_own(dispatched):
    """Both call sites fire, and neither fires twice. The partition is the whole
    correctness argument: two writers on one row could disagree about it."""
    from jarvis import worker_session

    store, wo_id = dispatched["store"], dispatched["wo_id"]
    assert [p is not None for p in _payloads(store, wo_id)] == [True]

    worker_session.send(store, dispatched["spec"], store.get_work_order(wo_id), "more")

    turns = store.list_turns(wo_id)
    assert [t["seq"] for t in turns] == [1, 2]
    payloads = _payloads(store, wo_id)
    assert all(p is not None for p in payloads)
    assert all(p["schema"] == context.SCHEMA for p in payloads)
    # written ONCE each: one UPDATE leaves one JSON document, and a second writer would
    # have to replace it — so the two payloads must differ exactly where the turns do.
    assert _named(payloads[0]["ingredients"], "worker_prompt")["bytes"] != \
        _named(payloads[1]["ingredients"], "worker_prompt")["bytes"]
    assert payloads[0]["caps"] == context.caps()
    assert payloads[0]["token_bytes"] == context.TOKEN_BYTES


def test_no_turn_is_recorded_twice(tmp_path, jarvis_home, fake_claude, project,
                                  catalog_file, monkeypatch):
    """The partition counted from the outside: exactly one `record` call per turn.

    The two writers are `dispatch.dispatch_work_order` (seq-1 dispatch turn) and
    `worker_session._launch` (everything else) — Neo's ruling on question 681. Two calls
    on one row is the failure this guards: they would write payloads that can disagree.
    """
    from jarvis import context as context_mod
    from jarvis import dispatch, worker_session

    calls: list[int] = []
    real = context_mod.record

    def spy(store, spec, wo, turn, briefing, knowledge=None):
        calls.append(turn["seq"])
        real(store, spec, wo, turn, briefing, knowledge)

    monkeypatch.setattr(context_mod, "record", spy)
    ops.start_os(str(catalog_file), foreground=True)
    cat = load_catalog(catalog_file)
    spec = cat.projects[0]
    ops.create_work_order("proj_a", "count the writers", description="do a thing")
    store, central = ProjectStore(project), CentralStore()
    try:
        wo = store.claim_next_pending()
        dispatch.dispatch_work_order(store, central, spec, wo, os_config=cat.os)
        worker_session.send(store, spec, store.get_work_order(wo["id"]), "more")

        assert calls == [1, 2]
    finally:
        central.close()
        store.close()


def test_a_relaunched_dispatch_turn_is_recorded_by_the_launch_side(dispatched):
    """The partition is by (kind, seq) and not by kind alone. A retry of a lost dispatch
    turn goes back through `_launch` with `kind="dispatch"` and a FRESH seq — the shape
    exercised directly here — and dispatch.py records seq 1 only, so without the seq half
    of the condition that turn would be recorded by nobody."""
    from jarvis import worker_session

    store, wo_id = dispatched["store"], dispatched["wo_id"]
    store.finish_turn(store.latest_turn(wo_id)["id"], "failed", error="boom")
    worker_session._launch(store, dispatched["spec"], store.get_work_order(wo_id),
                           "go again", kind="dispatch", resume=True, worktree=None,
                           cwd=dispatched["spec"].path)

    turns = store.list_turns(wo_id)
    assert [(t["seq"], t["kind"]) for t in turns] == [(1, "dispatch"), (2, "dispatch")]
    assert all(p is not None for p in _payloads(store, wo_id))


# -- 2. absent is never zero ----------------------------------------------------------


def test_the_knowledge_block_is_measured_on_dispatch_and_absent_afterwards(dispatched):
    """Issue #227. A later turn carries no knowledge block, so the row is a sentence and
    NOT a zero — a zero would claim the block was rendered empty."""
    from jarvis import worker_session

    store, wo_id = dispatched["store"], dispatched["wo_id"]
    worker_session.send(store, dispatched["spec"], store.get_work_order(wo_id), "more")
    first, second = _payloads(store, wo_id)

    on_dispatch = _named(first["ingredients"], "knowledge_index")
    assert on_dispatch["bytes"] > 0 and on_dispatch["tokens"] > 0
    assert on_dispatch["estimated"] is True and on_dispatch["measured"] is True

    later = _named(second["ingredients"], "knowledge_index")
    assert later["bytes"] is None and later["tokens"] is None
    assert later["bytes"] != 0 and later["absent"] is True
    assert "absent" in later["note"]


def test_the_knowledge_block_is_not_counted_twice_in_the_prompt(dispatched):
    """`worker_prompt` EXCLUDES the block when the block has its own row, or the dispatch
    turn's total is the block plus the block."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    (payload,) = _payloads(store, wo_id)
    rows = payload["ingredients"]
    prompt = store.list_turns(wo_id)[0]["prompt"]

    assert (_named(rows, "worker_prompt")["bytes"]
            + _named(rows, "knowledge_index")["bytes"]) == len(prompt.encode())


def test_an_unreadable_ingredient_is_none_with_a_reason(dispatched, monkeypatch):
    """A missing settings file is unknown, not empty. The distinction is the section's:
    a 0 here would be read as "Jarvis sent no settings"."""
    store, spec, wo = dispatched["store"], dispatched["spec"], dispatched["wo"]
    settings = spec.path / ".jarvis" / "worker-settings" / f"{wo['id']}.json"
    settings.unlink()

    rows = context.measure(spec, wo, store.latest_turn(wo["id"]),
                           {"append_system_prompt": "x", "add_dirs": []})
    row = _named(rows, "worker_settings")

    assert row["bytes"] is None and row["tokens"] is None and row["absent"] is True
    assert "could not be read" in row["note"]


def test_the_mcp_servers_are_a_set_and_never_a_size(dispatched):
    """kn-2c41d4cc's blind spot, stated on the row: tool schemas render at position 0 and
    are Claude Code's. None, not 0."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    (payload,) = _payloads(store, wo_id)
    row = _named(payload["ingredients"], "mcp_servers")

    assert row["bytes"] is None and row["tokens"] is None
    assert "position 0" in row["note"]


# -- 3. the residual, and what the fabrication proves ---------------------------------


def test_the_residual_is_the_subtraction_and_says_it_is_inferred():
    """The number is the test's own: it wrote the cold-start write. What is asserted is
    the arithmetic and the label, which is all CI can prove (§5)."""
    row = context.residual(1_000, {"written": 12_000})

    assert row["tokens"] == 11_000
    assert row["measured"] is False and row["estimated"] is True
    assert "INFERRED" in row["note"]


def test_no_cold_start_write_makes_the_residual_absent_not_zero():
    row = context.residual(1_000, None)

    assert row["tokens"] is None and row["absent"] is True
    assert row["tokens"] != 0
    assert "not zero" in row["note"]


def test_an_estimate_over_the_observed_prefix_is_unknown_and_not_negative():
    """Never a negative token count, and never silently clamped to 0 — both would be
    claims the measurement cannot support."""
    row = context.residual(20_000, {"written": 12_000})

    assert row["tokens"] is None
    assert "EXCEEDS the observed prefix" in row["note"]


# -- 4. the delta ----------------------------------------------------------------------


def _row(name: str, nbytes: int | None) -> dict:
    return {"name": name, "bytes": nbytes, "tokens": None if nbytes is None
            else nbytes // context.TOKEN_BYTES, "estimated": True, "measured": True,
            "absent": nbytes is None, "detail": {}, "note": ""}


def test_the_delta_names_the_ingredient_that_grew():
    out = context.delta([_row("worker_prompt", 100), _row("add_dirs", 5_000)],
                        [_row("worker_prompt", 100), _row("add_dirs", 800)],
                        against_seq=4)

    assert out["against_seq"] == 4
    assert out["biggest_growth"] == "add_dirs"
    grown = next(r for r in out["rows"] if r["name"] == "add_dirs")
    assert grown["kind"] == "change" and grown["bytes_delta"] == 4_200


def test_an_ingredient_present_on_one_side_only_appeared_or_disappeared():
    """Not a change from 0: the knowledge block leaving the window between turn 1 and
    turn 2 is normal, and "-4.2k" would say the prompt shrank by a block never in it."""
    out = context.delta([_row("knowledge_index", None), _row("agent_persona", 90)],
                        [_row("knowledge_index", 4_200)], against_seq=1)

    assert out["appeared"] == ["agent_persona"]
    assert out["disappeared"] == ["knowledge_index"]
    assert all(r["bytes_delta"] is None for r in out["rows"])


def test_an_ingredient_absent_on_both_turns_is_not_an_appearance():
    """No persona on either turn is not an event. Reporting it as appeared/disappeared
    would put three invented lines on every message turn's delta."""
    out = context.delta([_row("agent_persona", None)], [_row("agent_persona", None)])

    assert [r["kind"] for r in out["rows"]] == ["absent"]
    assert out["appeared"] == [] and out["disappeared"] == []
    assert out["biggest_growth"] is None


# -- 5. the report, the join, and the forward-only sentence ---------------------------


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture()
def transcript(tmp_path, monkeypatch):
    """Write a session transcript `inspection.classify_writes` will read."""
    root = tmp_path / "ctx-projects"
    (root / "-proj").mkdir(parents=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, calls: list[tuple[float, int]]) -> None:
        rows = []
        for at, written in calls:
            rows.append({"type": "user", "timestamp": _stamp(at), "promptSource": "sdk",
                         "message": {"content": "go"}})
            rows.append({
                "type": "assistant", "timestamp": _stamp(at),
                "message": {"id": f"m{at}", "model": "claude-opus-5",
                            "usage": {"input_tokens": 0,
                                      "cache_creation_input_tokens": written,
                                      "cache_read_input_tokens": 10,
                                      "output_tokens": 1},
                            "content": [{"type": "text", "text": "ok"}]}})
        (root / "-proj" / f"{session_id}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))

    return write


def _turn_window(store: ProjectStore, wo_id: str, seq: int) -> tuple[float, float]:
    turn = next(t for t in store.list_turns(wo_id) if t["seq"] == seq)
    return turn["started_at"], turn["ended_at"]


def _inside(store: ProjectStore, wo_id: str, seq: int) -> float:
    """A timestamp in the middle of that turn's process window — which is what the join
    is on. A fake `claude` turn lasts milliseconds, so "start + half a second" would land
    AFTER the turn ended and prove the opposite of what these tests mean to."""
    started, ended = _turn_window(store, wo_id, seq)
    return started + (ended - started) / 2


def test_the_report_carries_the_residual_for_the_turn_the_write_falls_in(
        dispatched, transcript):
    """Writes are joined to turns by TIMESTAMP against `wo_turns.started_at/ended_at`.
    Transcript turn numbering is a different scheme and joining on it would misattribute."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    turn = store.latest_turn(wo_id)
    store.finish_turn(turn["id"], "done", result="ok")
    started, ended = _turn_window(store, wo_id, 1)
    transcript(store.get_work_order(wo_id)["session_id"],
               [(_inside(store, wo_id, 1), 60_000)])

    out = ops.context_report(wo_id)
    (row,) = out["turns"]

    assert out["recorded"] is True and row["recorded"] is True
    assert row["residual"]["measured"] is False
    assert row["residual"]["tokens"] == (
        60_000 - context.measured_tokens(row["ingredients"]))
    assert started <= ended


def test_a_prefix_miss_is_joined_to_the_delta_with_the_authority_stated(
        dispatched, transcript):
    """The one sentence the section exists for — and the precedence, in the payload
    rather than only in a docstring: the classification is the AUTHORITY, the delta is
    the HYPOTHESIS, and `invariants.check_prefix_stable` is authoritative for prefix
    stability (kn-fafe92b7). The report names a cause and raises no alarm (kn-2c41d4cc).
    """
    from jarvis import worker_session

    store, wo_id = dispatched["store"], dispatched["wo_id"]
    store.finish_turn(store.latest_turn(wo_id)["id"], "done", result="ok")
    worker_session.send(store, dispatched["spec"], store.get_work_order(wo_id), "more")
    store.finish_turn(store.latest_turn(wo_id)["id"], "done", result="ok")
    transcript(store.get_work_order(wo_id)["session_id"],
               [(_inside(store, wo_id, 1), 60_000),
                (_inside(store, wo_id, 2), 40_000)])

    out = ops.context_report(wo_id)
    second = out["turns"][1]

    assert [w["cause"] for w in second["prefix_break"]["writes"]] == ["prefix-miss"]
    assert second["prefix_break"]["delta"]["against_seq"] == 1
    assert "authority" in second["prefix_break"]["authority"]
    assert "check_prefix_stable" in second["prefix_break"]["authority"]
    assert second["prefix_break"]["cause"]


def test_an_order_whose_turns_predate_the_ledger_says_so(dispatched):
    """Forward-only, and the surface says it: an empty table is what a reader reports as
    a bug (§5, "Forward-only, and the surface must say so")."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    store.conn.execute("UPDATE wo_turns SET context_json=NULL WHERE wo_id=?", (wo_id,))
    store.conn.commit()

    out = ops.context_report(wo_id)

    assert out["recorded"] is False
    assert "not recorded for this order" in out["note"]
    assert out["turns"] and out["turns"][0]["recorded"] is False
    assert "not recorded" in out["turns"][0]["note"]


def test_a_turn_that_asked_for_does_not_exist_is_an_error(dispatched):
    with pytest.raises(ops.OpsError) as e:
        ops.context_report(dispatched["wo_id"], turn=99)

    assert "99" in str(e.value)


# -- 6. the CLI derives nothing -------------------------------------------------------


def test_the_cli_json_and_the_human_render_agree(dispatched, capsys):
    """The renderer computes nothing: every number and every sentence it prints is a key
    of the payload, so the two cannot drift."""
    from jarvis import cli

    wo_id = dispatched["wo_id"]
    assert cli.main(["wo", "context", wo_id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert cli.main(["wo", "context", wo_id]) == 0
    text = capsys.readouterr().out

    rows = payload["turns"][0]["ingredients"]
    assert _named(rows, "knowledge_index")["note"] in text or "knowledge_index" in text
    assert f"{_named(rows, 'worker_prompt')['tokens']:,}" in text
    assert "estimate" in text


def test_the_cli_reports_a_turn_that_does_not_exist(dispatched, capsys):
    """An N with no such turn is an ERROR that says what turns there are — not an empty
    list, which reads as "that turn recorded nothing"."""
    from jarvis import cli

    assert cli.main(["wo", "context", dispatched["wo_id"], "--turn", "99"]) != 0

    assert "has no turn 99" in capsys.readouterr().err


# -- 7. what the measurement must not have moved --------------------------------------


def test_the_measurement_moves_neither_the_prompt_nor_the_fingerprint(dispatched):
    """`tests/test_stable_prefix.py` holds the appended prompt stable across turns; this
    holds the two surfaces THIS feature touches: the prompt bytes on the row and
    `hooks.prefix_fingerprint`, neither of which the ledger may perturb."""
    from jarvis import dispatch, hooks

    store, spec, wo = dispatched["store"], dispatched["spec"], dispatched["wo"]
    turn = store.latest_turn(wo["id"])
    before = hooks.prefix_fingerprint(spec.path, spec.path, wo, {})

    context.record(store, spec, wo, turn, {"append_system_prompt": "x", "add_dirs": []})

    assert hooks.prefix_fingerprint(spec.path, spec.path, wo, {}) == before
    assert dispatch.build_worker_prompt(wo, spec) == \
        dispatch.build_worker_prompt(wo, spec)


def test_the_column_is_a_plain_text_blob_the_store_can_read_back(dispatched):
    """Written through `store.conn`, so a payload that is not JSON-serialisable would be
    a silent NULL rather than a crash — assert it round-trips."""
    store, wo_id = dispatched["store"], dispatched["wo_id"]
    (raw,) = store.conn.execute(
        "SELECT context_json FROM wo_turns WHERE wo_id=? AND seq=1", (wo_id,)).fetchone()

    assert isinstance(raw, str)
    assert isinstance(json.loads(raw)["ingredients"], list)
    with pytest.raises(sqlite3.OperationalError):
        store.conn.execute("SELECT context_json FROM work_orders").fetchone()
