"""What the OS spends on itself, and whether the work order it was spent for can see it"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, claude_cli, ops, panel
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore

# A really dispatched order, shared rather than rebuilt: the meter tests below have to run against...
from tests.test_context import dispatched  # noqa: F401

#: What `testing.FAKE_CLAUDE`'s `emit_headless` reports for every one-shot call
FAKE_CALL = {"input": 5, "cache_write": 200, "cache_read": 800, "output": 60,
             "cost_usd": 0.002}


def calls(**filters) -> list[dict]:
    central = CentralStore()
    try:
        return central.agent_calls(**filters)
    finally:
        central.close()


def paid_calls(**filters) -> list[dict]:
    """The calls that cost MONEY — the rows below without §10's metered looks at the order"""
    return [row for row in calls(**filters)
            if not agent_usage.is_observability(row["kind"])]


# -- the store


def test_a_call_is_stored_with_its_tokens_split_out(jarvis_home):
    """The token columns are what the fleet report sums in SQL"""
    envelope = {"total_cost_usd": 0.5, "input": 10, "cache_write": 20, "cache_read": 30,
                "output": 40, "cache_5m": 20, "api_calls": 2, "context_peak": 60}
    agent_usage.record("neo_answer", usage=envelope, project="proj_a", wo_id="wo-1",
                       label="question", model="claude-opus-5", question_id=7)

    (row,) = calls(wo_id="wo-1")
    assert (row["input"], row["cache_write"], row["cache_read"], row["output"]) == (
        10, 20, 30, 40)
    assert row["cost_usd"] == 0.5
    assert row["kind"] == "neo_answer" and row["label"] == "question"
    assert row["project"] == "proj_a" and row["question_id"] == 7
    # The envelope survives whole: the 1h/5m split and the per-call context peak are in no column
    assert json.loads(row["usage_json"])["cache_5m"] == 20


def test_a_headless_result_records_without_being_unwrapped(jarvis_home):
    """Call sites hold a `HeadlessResult`, not an envelope"""
    result = claude_cli.HeadlessResult(text="{}", usage={"output": 9},
                                       model="claude-haiku-4-5")
    agent_usage.record("digest", usage=result, wo_id="wo-2")

    (row,) = calls(wo_id="wo-2")
    assert row["output"] == 9
    # The model came off the result, not the (absent) keyword: an OS call priced against the wrong family...
    assert row["model"] == "claude-haiku-4-5"


def test_a_call_with_no_usage_is_still_recorded_as_a_call(jarvis_home):
    """"A call was made and cost something unknown" and "no call was made" are different facts"""
    agent_usage.record("neo_answer", usage=None, wo_id="wo-3", ok=False)

    (row,) = calls(wo_id="wo-3")
    assert row["usage_json"] is None and row["ok"] == 0
    assert (row["input"], row["output"], row["cost_usd"]) == (0, 0, None)


def test_the_input_size_is_taken_off_a_headless_result(jarvis_home):
    """How big the OS's own prompt was, recorded with the call (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
    result = claude_cli.HeadlessResult(text="{}", usage={"output": 9},
                                       model="claude-haiku-4-5",
                                       prompt_chars=120_000,
                                       system_prompt_chars=9_000)
    agent_usage.record("digest", usage=result, wo_id="wo-size-1")

    (row,) = calls(wo_id="wo-size-1")
    assert (row["prompt_chars"], row["system_prompt_chars"]) == (120_000, 9_000)


def test_the_input_size_is_taken_off_a_plain_envelope_too(jarvis_home):
    """The load-bearing half: the seats"""
    agent_usage.record("panel_seat", usage={"output": 9, "prompt_chars": 4_200,
                                            "system_prompt_chars": 700},
                       wo_id="wo-size-2")

    (row,) = calls(wo_id="wo-size-2")
    assert (row["prompt_chars"], row["system_prompt_chars"]) == (4_200, 700)


def test_the_latency_is_taken_off_a_headless_result(jarvis_home):
    """How long the model took, recorded with the call (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
    result = claude_cli.HeadlessResult(text="{}", usage={"output": 9},
                                       model="claude-haiku-4-5", latency_ms=2_400)
    agent_usage.record("neo_answer", usage=result, wo_id="wo-lat-1")

    (row,) = calls(wo_id="wo-lat-1")
    assert row["latency_ms"] == 2_400


def test_the_latency_is_taken_off_a_plain_envelope_too(jarvis_home):
    """The load-bearing half: `structured.request` and `panel._record` pass `usage=result.usage`"""
    agent_usage.record("panel_seat", usage={"output": 9, "latency_ms": 1_750},
                       wo_id="wo-lat-2")

    (row,) = calls(wo_id="wo-lat-2")
    assert row["latency_ms"] == 1_750


def test_a_call_nobody_timed_records_no_latency_at_all(jarvis_home):
    """NULL, never 0: the report must not print "0 ms" for a call that was not measured"""
    agent_usage.record("neo_answer", usage={"output": 9}, wo_id="wo-lat-3")
    agent_usage.record("neo_answer", usage=None, wo_id="wo-lat-4")

    assert [row["latency_ms"] for row in calls(wo_id="wo-lat-3")] == [None]
    assert [row["latency_ms"] for row in calls(wo_id="wo-lat-4")] == [None]


def test_a_call_with_no_usage_records_no_size_and_keeps_its_failure(jarvis_home):
    """0/0 is what "not measured" reads as, and `ok=False` still says the call failed"""
    agent_usage.record("neo_answer", usage=None, wo_id="wo-size-3", ok=False)

    (row,) = calls(wo_id="wo-size-3")
    assert (row["prompt_chars"], row["system_prompt_chars"]) == (0, 0)
    assert row["ok"] == 0


def test_recording_never_raises(jarvis_home, monkeypatch):
    """Accounting is an observer"""
    class Broken:
        def add_agent_call(self, *a, **kw):
            raise RuntimeError("disk gone")

        def close(self):
            raise RuntimeError("still gone")

    monkeypatch.setattr(agent_usage, "CentralStore", Broken)
    assert agent_usage.record("neo_answer", usage={"output": 1}, wo_id="wo-4") is None


def test_the_recorder_binds_everything_but_the_usage(jarvis_home):
    """The seam for transports that hand their accounting to a callback (`digest`)"""
    seen = []
    sink = agent_usage.recorder("digest", project="proj_a", wo_id="wo-5",
                                record=lambda kind, **kw: seen.append((kind, kw)))
    sink({"output": 3})

    assert seen == [("digest", {"usage": {"output": 3}, "project": "proj_a",
                                "wo_id": "wo-5", "label": "", "model": "",
                                "question_id": None})]


# -- the call sites


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def asked(started):
    """A dispatched work order with one question answered by Neo"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    daemon._neo_drain()
    return daemon, wo


def test_neo_answering_a_question_is_billed_to_the_work_order_that_asked(asked):
    """The whole point: a work order that asks Neo four questions costs four Neo calls"""
    _, wo = asked

    (row,) = paid_calls(wo_id=wo["id"])
    assert row["kind"] == "neo_answer" and row["project"] == "proj_a"
    assert row["question_id"] == 1
    assert (row["input"], row["output"]) == (FAKE_CALL["input"], FAKE_CALL["output"])
    assert row["cost_usd"] == FAKE_CALL["cost_usd"]


def test_the_panel_records_one_row_per_seat(started, monkeypatch):
    """ONE ROW PER SEAT, never one per question"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Which delimiter should the exporter use?")

    store = NeoStore()
    try:
        q = store.claim_next()
        assert q is not None
        cfg = load_catalog_neo_with_panel()
        panel.decide(store, q, cfg)
    finally:
        store.close()

    seats = [c["label"] for c in calls(wo_id=wo["id"]) if c["kind"] == "panel_seat"]
    # Four seats blind (the fake routes `panel`), then the chair synthesising
    assert sorted(seats) == ["blast", "chair", "premise", "record", "taste"]
    assert all(c["output"] == FAKE_CALL["output"]
               for c in paid_calls(wo_id=wo["id"]))


def load_catalog_neo_with_panel():
    """A `NeoConfig` with the panel on — the panel ships disabled"""
    from jarvis.catalog import NeoConfig, PanelConfig

    return NeoConfig(enabled=True, model="claude-fake-1",
                     panel=PanelConfig(enabled=True, roster=tuple(panel.SEATS)))


def test_a_digest_is_billed_to_the_question_it_shortens(started):
    """A digest is an extra call per question that the user never asked for"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    # Long enough to be worth a digest (`digest.MIN_CHARS`), and escalated
    ops.ask_question(wo["id"], "FORCE_ESCALATE. Which delimiter? " * 40)
    daemon._neo_drain()
    daemon._digest_batch()

    kinds = [c["kind"] for c in calls(wo_id=wo["id"])]
    assert "digest" in kinds and "neo_answer" in kinds


def test_every_attempt_of_a_retried_call_is_billed(jarvis_home):
    """`structured.request` retries an unparseable reply"""
    from jarvis import structured

    replies = iter([claude_cli.HeadlessResult(text="not json", usage={"output": 1}),
                    claude_cli.HeadlessResult(text='{"ok": 1}', usage={"output": 2})])
    seen: list[dict] = []
    out = structured.request("q", validate=lambda d: d["ok"], attempts=2,
                             call=lambda *a, **kw: next(replies),
                             on_usage=seen.append)

    assert out == 1
    assert [u["output"] for u in seen] == [1, 2]


def test_a_transport_that_returns_a_bare_string_still_works(jarvis_home):
    """The `call=` seam predates usage capture and its fakes return strings"""
    from jarvis import structured

    assert structured.request("q", validate=lambda d: d["ok"],
                              call=lambda *a, **kw: '{"ok": "yes"}') == "yes"


# -- the report


def test_the_work_order_total_is_the_worker_plus_jarvis(asked):
    """The number that answers "what did this work order cost\""""
    _, wo = asked

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    # ONE, still, with §10's meter recording a row on every dispatch: a metered look is not a `claude -p`...
    assert unit["os_calls"] == 1
    assert unit["os_cost_usd"] > 0
    assert unit["total_cost_usd"] == pytest.approx(
        round(unit["list_cost_usd"] + unit["os_cost_usd"], 4))
    # And the two accountings stay unmixed: the exact CLI figure has its own field
    assert unit["os_recorded_cost_usd"] == pytest.approx(FAKE_CALL["cost_usd"])


def test_jarvis_spend_survives_a_pruned_transcript(asked):
    """A worker's spend is read from a file Claude Code prunes on its own schedule"""
    _, wo = asked

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert not unit["found"]  # the fake writes no transcript for the worker's session
    assert unit["os_calls"] == 1 and unit["measurable"]
    assert unit["total_cost_usd"] == unit["os_cost_usd"]


def test_the_per_call_detail_says_what_the_spend_went_on(asked):
    """The table that answers "why did an order I never touched cost three dollars\""""
    _, wo = asked

    report = ops.cost_report(target=wo["id"], project="proj_a")
    (call,) = report["os_calls_detail"]
    assert call["kind"] == "neo_answer" and call["ok"]
    assert call["list_cost_usd"] > 0 and call["cost_usd"] == FAKE_CALL["cost_usd"]
    assert [k["kind"] for k in report["units"][0]["os_by_kind"]] == ["neo_answer"]


def test_the_fleet_total_carries_the_os_spend(asked):
    _, wo = asked

    totals = ops.cost_report()["totals"]
    assert totals["os_calls"] == 1
    assert totals["total_cost_usd"] == pytest.approx(
        round(totals["list_cost_usd"] + totals["os_cost_usd"], 2))


def test_deleting_a_work_order_takes_its_os_spend_with_it(asked):
    """`wo delete` is documented as erasing the work order and its whole history"""
    _, wo = asked
    assert calls(wo_id=wo["id"])

    deleted = ops.delete_work_order(wo["id"], "proj_a")

    assert deleted["deleted"]["agent_calls"] == 2  # the Neo answer and the metered write
    assert calls(wo_id=wo["id"]) == []


# -- the meter over the observability paths


def collector(rows: list[dict]):
    """A stand-in for `agent_usage.record` — asserts on what WOULD be written, no DB"""
    def fake(kind, **kwargs):
        rows.append({"kind": kind, **kwargs})
        return len(rows)

    return fake


def test_the_observability_kinds_are_their_own_class():
    """Named beside `SUBPROCESS_KINDS`"""
    assert agent_usage.OBSERVABILITY_KINDS == frozenset({
        agent_usage.OBSERVE_LIVE, agent_usage.OBSERVE_INSPECT,
        agent_usage.OBSERVE_CONTEXT, agent_usage.OBSERVE_WHY,
        agent_usage.OBSERVE_CONTEXT_WRITE})
    for kind in agent_usage.OBSERVABILITY_KINDS:
        assert agent_usage.is_observability(kind)
        assert kind in agent_usage.KIND_LABELS
    assert not agent_usage.is_observability(agent_usage.WORKER_SUBPROCESS)
    assert not agent_usage.is_subprocess(agent_usage.OBSERVE_LIVE)


def test_one_row_per_invocation_with_wall_clock_and_zero_tokens():
    """Wall clock in the envelope and the four token classes written as explicit zeros"""
    from jarvis import observability

    rows: list[dict] = []

    @observability.metered(agent_usage.OBSERVE_LIVE, target="wo_id", project="project",
                           record=collector(rows))
    def report(wo_id, project=None):
        return {"id": wo_id}

    assert report("wo-1", project="proj_a") == {"id": "wo-1"}
    assert report("wo-2") == {"id": "wo-2"}

    assert [r["kind"] for r in rows] == [agent_usage.OBSERVE_LIVE] * 2
    assert [r["wo_id"] for r in rows] == ["wo-1", "wo-2"]
    assert [r["project"] for r in rows] == ["proj_a", ""]
    for row in rows:
        assert row["usage"]["wall_ms"] >= 0
        assert row["usage"]["total_cost_usd"] == 0
        assert [row["usage"][cls] for cls in
                ("input", "cache_write", "cache_read", "output")] == [0, 0, 0, 0]
        assert row["ok"] is True


def test_a_failing_report_still_records_a_row_and_re_raises():
    from jarvis import observability

    rows: list[dict] = []

    @observability.metered(agent_usage.OBSERVE_WHY, target="wo_id",
                           record=collector(rows))
    def report(wo_id):
        raise ValueError("no such order")

    with pytest.raises(ValueError, match="no such order"):
        report("wo-9")

    assert len(rows) == 1
    assert rows[0]["ok"] is False and rows[0]["wo_id"] == "wo-9"


def test_a_broken_meter_never_touches_the_report():
    """Accounting is an observer (this module's docstring)"""
    from jarvis import observability

    def explode(kind, **kwargs):
        raise RuntimeError("the meter is broken")

    @observability.metered(agent_usage.OBSERVE_INSPECT, target="target", record=explode)
    def report(target):
        return {"target": target, "found": True}

    assert report("wo-1") == {"target": "wo-1", "found": True}

    @observability.metered(agent_usage.OBSERVE_INSPECT, target="target", record=explode)
    def failing(target):
        raise ValueError("gone")

    with pytest.raises(ValueError, match="gone"):
        failing("wo-1")


def test_each_observability_path_is_metered_at_its_definition(dispatched, monkeypatch):
    """All five paths, wrapped where they are DEFINED"""
    import contextlib

    from jarvis import context as context_mod

    wo_id = dispatched["wo_id"]
    rows: list[dict] = []
    monkeypatch.setattr(agent_usage, "record", collector(rows))

    for call in (lambda: ops.diagnose(wo_id),
                 lambda: ops.context_report(wo_id),
                 lambda: ops.inspect_report(wo_id),
                 lambda: ops.live_report(wo_id)):
        with contextlib.suppress(Exception):  # a report may fail; the row still lands
            call()
    context_mod.record(dispatched["store"], dispatched["spec"], dispatched["wo"],
                       dispatched["store"].latest_turn(wo_id),
                       {"append_system_prompt": "x", "add_dirs": []})

    assert [r["kind"] for r in rows] == [
        agent_usage.OBSERVE_WHY, agent_usage.OBSERVE_CONTEXT,
        agent_usage.OBSERVE_INSPECT, agent_usage.OBSERVE_LIVE,
        agent_usage.OBSERVE_CONTEXT_WRITE]
    assert {r["wo_id"] for r in rows} == {wo_id}
    assert all(r["usage"]["wall_ms"] >= 0 and r["usage"]["total_cost_usd"] == 0
               for r in rows)
