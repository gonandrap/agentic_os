"""A compaction is settled against what it achieved"""

from __future__ import annotations

import json

from jarvis import claude_cli, invariants, ops, timeline, usage, worker_session
from jarvis.project_store import COMPACT_TURN, ProjectStore
from tests.test_compaction import (  # noqa: F401 — pytest fixtures
    BIG, _age_last_turn, _deliver, _running_wo, _turns, fleet,
)


def _compaction_events(store: ProjectStore, wo_id: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.list_events(wo_id, limit=None)
            if e["kind"] == "compacted"]


def _compact_now(fleet, settle_turns, wo_id: str) -> dict:
    """Cold boundary, queued message: the delivery pass compacts first"""
    store = fleet["store"]
    ops.send_message(wo_id, "go on")
    _age_last_turn(store, wo_id, usage.WRITE_TTL_SECONDS + 60)
    _deliver(fleet, wo_id)
    assert store.latest_turn(wo_id)["kind"] == COMPACT_TURN, "the premise"
    assert settle_turns(store)
    # Where a paused order really is: `running`
    store.set_status(wo_id, "running")
    return store.latest_turn(wo_id)


# -- ok-but-nothing-compacted


def test_a_compaction_that_left_a_boundary_records_what_it_achieved(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    turn = _compact_now(fleet, settle_turns, wo["id"])

    assert turn["state"] == "done"
    assert worker_session.compaction_record(turn) == {"pre": 3003, "post": 900}
    payload = _compaction_events(store, wo["id"])[-1]
    assert payload["outcome"] == worker_session.COMPACTION_DONE and payload["ok"]
    # The next floor test reasons about the conversation that exists, not the pre-compaction peak
    assert worker_session.measured_context(store, wo["id"]) == 900


def test_a_compaction_that_summarised_nothing_is_not_a_successful_one(
        fleet, fake_claude, settle_turns):
    """wo-299daf2e turn 9: ok, $0, and no boundary in the transcript"""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    fake_claude.compactions_summarise_nothing()

    turn = _compact_now(fleet, settle_turns, wo["id"])

    assert turn["state"] == "failed"
    assert turn["error"] == worker_session.NOTHING_COMPACTED
    assert worker_session.compaction_record(turn) == {}
    payload = _compaction_events(store, wo["id"])[-1]
    assert payload["outcome"] == worker_session.COMPACTION_NOTHING
    assert payload["ok"] is False
    label, detail = timeline._describe("compacted", payload)
    assert label == "Compaction summarised nothing" and "retried" in detail
    # A pause the OS relaunches by itself, so the queued message waits behind it
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.resumable
    assert pause.reason == worker_session.PAUSE_TRANSIENT
    assert store.queued_messages(wo["id"])


def test_the_retry_of_a_compaction_that_summarised_nothing_runs_it_again(
        fleet, fake_claude, settle_turns):
    store, daemon = fleet["store"], fleet["daemon"]
    wo = _running_wo(fleet, settle_turns)
    fake_claude.compactions_summarise_nothing()
    turn = _compact_now(fleet, settle_turns, wo["id"])
    fake_claude.compactions_summarise_nothing(on=False)
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (turn["ended_at"] - 3600, turn["id"]))

    daemon.retry_paused_turns(fleet["project"], store)

    assert _turns(store, wo["id"])[-1] == (COMPACT_TURN, claude_cli.COMPACT_PROMPT)
    assert len([k for k, _ in _turns(store, wo["id"]) if k == COMPACT_TURN]) == 2
    assert settle_turns(store)
    assert store.latest_turn(wo["id"])["state"] == "done"


def test_no_compacted_event_renders_bare():
    """Every shape the event has ever had says WHICH compaction this was"""
    shapes = [{"trigger": "auto"}, {"ok": True}, {"ok": True, "outcome": "unverified"},
              {"ok": False, "error": "x"}, {"ok": True, "before": 10, "after": 2}]
    for payload in shapes:
        label, detail = timeline._describe("compacted", payload)
        assert (label, detail) != ("Conversation compacted", ""), payload


# -- retry-resends-a-compaction


def _paused_compaction_that_landed(fleet, settle_turns) -> dict:
    """A compaction turn that compacted, then was recorded `failed` by the transport"""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    turn = _compact_now(fleet, settle_turns, wo["id"])
    assert turn["state"] == "done", "the premise"
    store.conn.execute(
        "UPDATE wo_turns SET state='failed', error=?, ended_at=?, compaction_json=NULL "
        "WHERE id=?",
        ("API Error: 500 Internal server error.", turn["ended_at"] - 3600, turn["id"]))
    pause = worker_session.turn_pause(store, wo["id"])
    assert pause is not None and pause.due() and pause.turn["kind"] == COMPACT_TURN
    return store.get_work_order(wo["id"])


def test_a_compaction_that_already_landed_is_never_re_sent(fleet, settle_turns):
    """wo-2d259874, wo-f302361d, wo-4beada49: a retry of `/compact` summarised the summary"""
    store, daemon = fleet["store"], fleet["daemon"]
    wo = _paused_compaction_that_landed(fleet, settle_turns)
    before = len(store.list_turns(wo["id"]))

    daemon.retry_paused_turns(fleet["project"], store)

    assert len(store.list_turns(wo["id"])) == before, "nothing was relaunched"
    turn = store.latest_turn(wo["id"])
    assert turn["state"] == "done", "the record now says the compaction happened"
    assert worker_session.compacted_to(turn) == 900
    assert store.last_event_of_kind(wo["id"], "compaction_landed") is not None
    # …and the message queued behind it goes out on the next delivery pass
    _deliver(fleet, wo["id"])
    assert _turns(store, wo["id"])[-1] == ("message", "go on")


def test_retry_of_a_landed_compaction_returns_nothing_to_relaunch(fleet, settle_turns):
    store = fleet["store"]
    wo = _paused_compaction_that_landed(fleet, settle_turns)
    pause = worker_session.turn_pause(store, wo["id"])
    assert worker_session.retry(store, fleet["project"], wo, pause) is None


# -- a-done-turn-is-relaunched


def test_a_budget_top_up_does_not_relaunch_a_compaction_that_completed(
        fleet, settle_turns):
    """wo-9f00e3b5: the latest turn was a DONE compaction and a message was queued behind it"""
    from jarvis import budget as budget_mod

    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _compact_now(fleet, settle_turns, wo["id"])
    assert store.queued_messages(wo["id"]), "the premise: message 2219, still queued"
    store.set_status(wo["id"], budget_mod.EXHAUSTED)
    turns = len(store.list_turns(wo["id"]))

    out = ops.set_work_order_budget(wo["id"], 80.0)

    assert out["resumed"] and "queued message" in out["note"], out
    assert len(store.list_turns(wo["id"])) == turns, "nothing was relaunched"
    _deliver(fleet, wo["id"])
    assert _turns(store, wo["id"])[-1] == ("message", "go on")
    assert [k for k, _ in _turns(store, wo["id"])].count(COMPACT_TURN) == 1


def test_a_budget_top_up_does_not_relaunch_a_worker_turn_that_completed(
        fleet, settle_turns):
    """Any kind: re-sending the nudge into a finished conversation burns a cold turn"""
    from jarvis import budget as budget_mod

    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    store.set_status(wo["id"], budget_mod.EXHAUSTED)
    turns = len(store.list_turns(wo["id"]))

    out = ops.set_work_order_budget(wo["id"], 80.0)

    assert out["resumed"] and "nothing to relaunch" in out["note"], out
    assert len(store.list_turns(wo["id"])) == turns
    assert store.get_work_order(wo["id"])["status"] == "running"


# -- compaction-has-no-post-condition


def _chain(store: ProjectStore, wo_id: str, *, first: str = "done") -> None:
    """Two compaction turns at adjacent seqs, the first one landed"""
    a = store.create_turn(wo_id, kind=COMPACT_TURN, prompt="/compact")
    store.finish_turn(a["id"], first)
    store.set_turn_compaction(a["id"], {"pre": 111_596, "post": 9_987})
    b = store.create_turn(wo_id, kind=COMPACT_TURN, prompt="/compact")
    store.finish_turn(b["id"], "done")
    store.set_turn_compaction(b["id"], {"pre": 3_578, "post": 10_954})


def _compaction_alarms(store: ProjectStore, wo_id: str) -> list[dict]:
    return [a for a in store.alarms_of(wo_id) if a["kind"].startswith("compaction-")]


def test_a_chained_compaction_raises_exactly_one_alarm_and_the_ack_holds(
        fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _chain(store, wo["id"])

    found = list(invariants.check_compactions_hold(store))

    assert [v.invariant for v in found] == ["INV-COMPACTION-CHAINED"]
    alarms = _compaction_alarms(store, wo["id"])
    assert [a["kind"] for a in alarms] == [invariants.COMPACTION_CHAINED_ALARM]
    assert "111,596 → 9,987" in alarms[0]["reason"]
    assert "3,578 → 10,954" in alarms[0]["reason"]
    assert store.get_work_order(wo["id"])["needs_attention"]

    ops.ack_attention(wo["id"])
    for _ in range(3):
        assert not list(invariants.check_compactions_hold(store))
    assert len(_compaction_alarms(store, wo["id"])) == 1
    assert not store.get_work_order(wo["id"])["needs_attention"]


def test_a_healthy_order_raises_neither(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _compact_now(fleet, settle_turns, wo["id"])
    _deliver(fleet, wo["id"])
    assert settle_turns(store)

    assert not list(invariants.check_compactions_hold(store))
    assert not _compaction_alarms(store, wo["id"])


def test_a_retried_compaction_that_never_landed_is_not_a_chain(fleet, settle_turns):
    """Adjacent `/compact` rows are a defect only when the first one compacted"""
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    a = store.create_turn(wo["id"], kind=COMPACT_TURN, prompt="/compact")
    store.finish_turn(a["id"], "failed", error=worker_session.NOTHING_COMPACTED)
    store.set_turn_compaction(a["id"], None)
    b = store.create_turn(wo["id"], kind=COMPACT_TURN, prompt="/compact")
    store.finish_turn(b["id"], "done")
    store.set_turn_compaction(b["id"], {"pre": 50_000, "post": 4_000})

    assert not list(invariants.check_compactions_hold(store))


def test_a_compaction_settled_ok_with_nothing_achieved_raises_an_alarm(
        fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    t = store.create_turn(wo["id"], kind=COMPACT_TURN, prompt="/compact")
    store.finish_turn(t["id"], "done")
    store.set_turn_compaction(t["id"], None)

    found = list(invariants.check_compactions_hold(store))

    assert [v.invariant for v in found] == ["INV-COMPACTION-UNPROVEN"]
    assert not list(invariants.check_compactions_hold(store)), "raised once"


def test_doctor_reports_a_chain_without_raising_it(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _chain(store, wo["id"])

    found = [v for v in invariants.check_project(store, repair=False)
             if v.invariant == "INV-COMPACTION-CHAINED"]

    assert len(found) == 1 and "would raise" in found[0].repair
    assert not _compaction_alarms(store, wo["id"])


def test_a_chain_older_than_the_window_is_not_raised(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    _chain(store, wo["id"])
    long_ago = store.latest_turn(wo["id"])["started_at"] - 30 * 86400
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE wo_id=? AND kind=?",
                       (long_ago, wo["id"], COMPACT_TURN))

    assert not list(invariants.check_compactions_hold(store))


# -- wo-show-renders-the-oldest-200-events


def test_the_newest_page_of_a_long_order_is_what_renders(fleet, settle_turns,
                                                         capsys):
    from jarvis import cli

    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    for i in range(500):
        store.add_event(wo["id"], "note", {"i": i})
    total = store.count_events(wo["id"])

    newest = store.list_events(wo["id"], limit=200, newest=True)
    assert len(newest) == 200
    assert json.loads(newest[-1]["payload"]) == {"i": 499}, "the tail is kept"
    assert newest == sorted(newest, key=lambda e: e["ts"]), "still oldest first"

    cli.main(["wo", "show", wo["id"], "--json"])
    detail = json.loads(capsys.readouterr().out)
    assert detail["events_shown"] == 200
    assert detail["events_hidden"] == total - 200

    cli.main(["wo", "show", wo["id"]])
    assert f"{total - 200} earlier events not shown" in capsys.readouterr().out

    cli.main(["wo", "show", wo["id"], "--json", "--events", "0"])
    detail = json.loads(capsys.readouterr().out)
    assert detail["events_hidden"] == 0 and detail["events_shown"] == total


def test_a_long_orders_lost_reply_is_recovered_from_its_newest_stop_hook(
        fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    for i in range(300):
        store.add_event(wo["id"], "note", {"i": i})
    turn = store.create_turn(wo["id"], kind="message", prompt="x")
    store.add_event(wo["id"], "hook:Stop", {"last_assistant_message": "the late reply"})

    assert worker_session._last_assistant_message(store, wo["id"], turn) == \
        "the late reply"


def test_the_floor_reads_the_achieved_size_not_the_peak(fleet, settle_turns):
    store = fleet["store"]
    wo = _running_wo(fleet, settle_turns)
    t = store.create_turn(wo["id"], kind=COMPACT_TURN, prompt="/compact")
    store.finish_turn(t["id"], "done", usage_json=json.dumps({"context_peak": 111_596}))
    store.set_turn_compaction(t["id"], {"pre": 111_596, "post": 9_987})

    assert worker_session.measured_context(store, wo["id"]) == 9_987
    assert BIG  # the fleet's floor; the size it is compared against is the one above
