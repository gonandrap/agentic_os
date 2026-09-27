"""What the OS spends on itself, and whether the work order it was spent for can see it.

`tests/test_usage.py` and `tests/test_cost_report.py` cover the WORKER half of the bill —
transcripts, and attributing them to a work order. This covers the half that had no
accounting at all until now: every `claude -p` call Jarvis makes on a work order's behalf
(Neo answering its questions, the panel's seats deliberating, the digest shortening the
result for the dashboard).

Three things are worth a test here and the rest is arithmetic:

* the envelope is CAPTURED — `run_headless` parsed the CLI's result JSON and dropped the
  `usage` object beside it, which is how this spend stayed invisible for so long;
* it is attributed to the right work order, ONE ROW PER SEAT — an aggregate cannot answer
  whether the panel earns its price, which is the number the panel's own eval turns on;
* the accounting never breaks the work. It observes; a work order must not fail, and Neo
  must not stop answering, because a row could not be written.
"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, claude_cli, ops, panel
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore

# A really dispatched order, shared rather than rebuilt: the meter tests below have to run
# against the real report functions, and a hand-built turn row would pass while the
# instrumentation was missing.
from tests.test_context import dispatched  # noqa: F401

#: What `testing.FAKE_CLAUDE`'s `emit_headless` reports for every one-shot call.
FAKE_CALL = {"input": 5, "cache_write": 200, "cache_read": 800, "output": 60,
             "cost_usd": 0.002}


def calls(**filters) -> list[dict]:
    central = CentralStore()
    try:
        return central.agent_calls(**filters)
    finally:
        central.close()


def paid_calls(**filters) -> list[dict]:
    """The calls that cost MONEY — the rows below without §10's metered looks at the order.

    Every dispatch now also records one `observe_context_write` row (a measured zero, see
    the meter section at the foot of this file), so a test about what Jarvis SPENT filters
    them out rather than counting to two.
    """
    return [row for row in calls(**filters)
            if not agent_usage.is_observability(row["kind"])]


# -- the store ------------------------------------------------------------------------


def test_a_call_is_stored_with_its_tokens_split_out(jarvis_home):
    """The token columns are what the fleet report sums in SQL; `usage_json` keeps the
    full envelope for anyone reading one call."""
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
    # The envelope survives whole: the 1h/5m split and the per-call context peak are in
    # no column, and they are what a token-economics question is asked with.
    assert json.loads(row["usage_json"])["cache_5m"] == 20


def test_a_headless_result_records_without_being_unwrapped(jarvis_home):
    """Call sites hold a `HeadlessResult`, not an envelope. Making each one reach inside
    it is how a call site ends up recording the wrong thing."""
    result = claude_cli.HeadlessResult(text="{}", usage={"output": 9},
                                       model="claude-haiku-4-5")
    agent_usage.record("digest", usage=result, wo_id="wo-2")

    (row,) = calls(wo_id="wo-2")
    assert row["output"] == 9
    # The model came off the result, not the (absent) keyword: an OS call priced against
    # the wrong family is a wrong number in the only report that says what Jarvis costs.
    assert row["model"] == "claude-haiku-4-5"


def test_a_call_with_no_usage_is_still_recorded_as_a_call(jarvis_home):
    """"A call was made and cost something unknown" and "no call was made" are different
    facts. The row exists, and its token columns stay zero so it cannot inflate a total."""
    agent_usage.record("neo_answer", usage=None, wo_id="wo-3", ok=False)

    (row,) = calls(wo_id="wo-3")
    assert row["usage_json"] is None and row["ok"] == 0
    assert (row["input"], row["output"], row["cost_usd"]) == (0, 0, None)


def test_recording_never_raises(jarvis_home, monkeypatch):
    """Accounting is an observer. A work order must not fail because a row could not be
    written — so a broken store costs a row, and nothing else."""
    class Broken:
        def add_agent_call(self, *a, **kw):
            raise RuntimeError("disk gone")

        def close(self):
            raise RuntimeError("still gone")

    monkeypatch.setattr(agent_usage, "CentralStore", Broken)
    assert agent_usage.record("neo_answer", usage={"output": 1}, wo_id="wo-4") is None


def test_the_recorder_binds_everything_but_the_usage(jarvis_home):
    """The seam for transports that hand their accounting to a callback (`digest`)."""
    seen = []
    sink = agent_usage.recorder("digest", project="proj_a", wo_id="wo-5",
                                record=lambda kind, **kw: seen.append((kind, kw)))
    sink({"output": 3})

    assert seen == [("digest", {"usage": {"output": 3}, "project": "proj_a",
                                "wo_id": "wo-5", "label": "", "model": "",
                                "question_id": None})]


# -- the call sites -------------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def asked(started):
    """A dispatched work order with one question answered by Neo."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    daemon._neo_drain()
    return daemon, wo


def test_neo_answering_a_question_is_billed_to_the_work_order_that_asked(asked):
    """The whole point: a work order that asks Neo four questions costs four Neo calls,
    and until now not one of them appeared anywhere."""
    _, wo = asked

    (row,) = paid_calls(wo_id=wo["id"])
    assert row["kind"] == "neo_answer" and row["project"] == "proj_a"
    assert row["question_id"] == 1
    assert (row["input"], row["output"]) == (FAKE_CALL["input"], FAKE_CALL["output"])
    assert row["cost_usd"] == FAKE_CALL["cost_usd"]


def test_the_panel_records_one_row_per_seat(started, monkeypatch):
    """ONE ROW PER SEAT, never one per question. Whether the panel earns its price is
    exactly what the seats cost against what the single agent would have — and an
    aggregate can neither answer that nor name the expensive seat."""
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
    # Four seats blind (the fake routes `panel`), then the chair synthesising.
    assert sorted(seats) == ["blast", "chair", "premise", "record", "taste"]
    assert all(c["output"] == FAKE_CALL["output"]
               for c in paid_calls(wo_id=wo["id"]))


def load_catalog_neo_with_panel():
    """A `NeoConfig` with the panel on — the panel ships disabled."""
    from jarvis.catalog import NeoConfig, PanelConfig

    return NeoConfig(enabled=True, model="claude-fake-1",
                     panel=PanelConfig(enabled=True, roster=tuple(panel.SEATS)))


def test_a_digest_is_billed_to_the_question_it_shortens(started):
    """A digest is an extra call per question that the user never asked for. It belongs
    on the bill of the work order whose answer it is shortening."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    # Long enough to be worth a digest (`digest.MIN_CHARS`), and escalated — a digest is
    # written for the questions the USER has to read.
    ops.ask_question(wo["id"], "FORCE_ESCALATE. Which delimiter? " * 40)
    daemon._neo_drain()
    daemon._digest_batch()

    kinds = [c["kind"] for c in calls(wo_id=wo["id"])]
    assert "digest" in kinds and "neo_answer" in kinds


def test_every_attempt_of_a_retried_call_is_billed(jarvis_home):
    """`structured.request` retries an unparseable reply. The retry is a second call the
    OS paid for, and an accounting that only recorded the attempt that parsed would
    under-report exactly the calls that went worst."""
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
    """The `call=` seam predates usage capture and its fakes return strings. Accepting
    both shapes is what keeps a signature change from becoming a test rewrite."""
    from jarvis import structured

    assert structured.request("q", validate=lambda d: d["ok"],
                              call=lambda *a, **kw: '{"ok": "yes"}') == "yes"


# -- the report -----------------------------------------------------------------------


def test_the_work_order_total_is_the_worker_plus_jarvis(asked):
    """The number that answers "what did this work order cost". A reader who only ever
    sees the worker's half concludes the OS is free."""
    _, wo = asked

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    # ONE, still, with §10's meter recording a row on every dispatch: a metered look is
    # not a `claude -p` call and is reported only by the bill, in its own class.
    assert unit["os_calls"] == 1
    assert unit["os_cost_usd"] > 0
    assert unit["total_cost_usd"] == pytest.approx(
        round(unit["list_cost_usd"] + unit["os_cost_usd"], 4))
    # And the two accountings stay unmixed: the exact CLI figure has its own field.
    assert unit["os_recorded_cost_usd"] == pytest.approx(FAKE_CALL["cost_usd"])


def test_jarvis_spend_survives_a_pruned_transcript(asked):
    """A worker's spend is read from a file Claude Code prunes on its own schedule. The
    OS's own calls are the OS's own record, so they are still there when it is gone."""
    _, wo = asked

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert not unit["found"]  # the fake writes no transcript for the worker's session
    assert unit["os_calls"] == 1 and unit["measurable"]
    assert unit["total_cost_usd"] == unit["os_cost_usd"]


def test_the_per_call_detail_says_what_the_spend_went_on(asked):
    """The table that answers "why did an order I never touched cost three dollars"."""
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
    """`wo delete` is documented as erasing the work order and its whole history. Spend
    attributed to an id nothing resolves would sit in the fleet total for ever."""
    _, wo = asked
    assert calls(wo_id=wo["id"])

    deleted = ops.delete_work_order(wo["id"], "proj_a")

    assert deleted["deleted"]["agent_calls"] == 2  # the Neo answer and the metered write
    assert calls(wo_id=wo["id"]) == []


# -- the meter over the observability paths --------------------------------------------
#
# §10 of docs/specs/2026-09-24-order-observability.md. A THIRD CLASS beside the worker's
# turns and Jarvis's overhead: what the user spent LOOKING at an order. Its dollars are a
# measured zero, and the measured zero is the point — "debugging is mechanical" becomes a
# number instead of an assertion, and a path that ever gains a model call stops reading
# zero on its own. The meter is NEVER switched off and never consults §10's gate; the
# gate's own tests are in tests/test_context.py.


def collector(rows: list[dict]):
    """A stand-in for `agent_usage.record` — asserts on what WOULD be written, no DB."""
    def fake(kind, **kwargs):
        rows.append({"kind": kind, **kwargs})
        return len(rows)

    return fake


def test_the_observability_kinds_are_their_own_class():
    """Named beside `SUBPROCESS_KINDS`, and every one labelled: an unlabelled kind reaches
    the bill as a bare column name."""
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
    """Wall clock in the envelope and the four token classes written as explicit zeros —
    Neo's ruling on question 767. A zero is a measurement; an absent figure is not."""
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
    """Accounting is an observer (this module's docstring). A report returns the same
    payload byte-for-byte whether or not the meter worked."""
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
    """All five paths, wrapped where they are DEFINED — which is why §7's dashboard routes
    need no edit: they call these same functions."""
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
