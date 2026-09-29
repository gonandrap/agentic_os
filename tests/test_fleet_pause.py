"""The user's brake, and the recovery after a usage-window reopen (issue #843).

2026-09-28 21:00 PDT: the window reopened and eleven work-order turns resumed within a
minute, each re-writing a cold 100-170k-token conversation — 2.25M cache-write tokens in
two minutes, the window spent again inside ten, and the same stampede waiting at every
reopen after it. The only brake the user had was `systemctl stop` and `kill`.

Four mechanisms, tested separately because they fail separately:
  * `jarvis pause` / `jarvis resume` — nothing starts a turn unless allow-listed
  * the ramp — after a reopen, a small in-flight cap for a while, whatever the steady cap
  * the breaker — a window spent again soon after reopening slows the next ramp, loudly
  * compaction before a cold relaunch — tests/test_compaction.py
"""

from __future__ import annotations

import json

import pytest

from jarvis import fleet, ops, worker_session
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


def _catalog(tmp_path, project, max_in_flight: int = 15, name: str = "cat.json"):
    path = tmp_path / name
    path.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet", "max_in_flight": max_in_flight},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project), "description": "a"}],
    }))
    return path


def _tick(daemon):
    daemon.tick_count = 0  # the retry pass runs on this tick
    daemon.tick()


@pytest.fixture()
def os_up(jarvis_home, fake_claude, tmp_path, project):
    cat = _catalog(tmp_path, project)
    ops.start_os(str(cat), foreground=True)
    return {"daemon": Daemon(load_catalog(cat)), "store": ProjectStore(project),
            "central": CentralStore(), "cat": cat}


def _park_on_usage_limit(store, wos, fake_claude, settle_turns, daemon, monkeypatch):
    """Every order dispatched and refused by the window; then the window reopens."""
    monkeypatch.setattr(worker_session, "RATE_LIMIT_MIN_DELAY", 0)
    fake_claude.turns_rate_limited(reset="11:59pm (UTC)")
    _tick(daemon)
    assert settle_turns(store)
    _tick(daemon)
    assert all(worker_session.turn_pause(store, w["id"]) is not None for w in wos)
    fake_claude.turns_recover()
    fake_claude.hold_turns()
    store.conn.execute("UPDATE wo_turns SET error=? WHERE state='failed'",
                       ("Claude AI usage limit reached|1000000000",))


# -- the pause --------------------------------------------------------------------------


def test_a_paused_fleet_dispatches_only_what_is_allowed(os_up, fake_claude):
    store, central, daemon = os_up["store"], os_up["central"], os_up["daemon"]
    fake_claude.hold_turns()
    a = ops.create_work_order("proj_a", "picked")
    b = ops.create_work_order("proj_a", "waits")
    ops.pause_fleet(reason="token burn", allow=[a["id"]])

    _tick(daemon)

    assert store.get_work_order(a["id"])["status"] != "pending"
    assert store.get_work_order(b["id"])["status"] == "pending"
    label = ops.os_status()
    assert label["fleet"]["paused"] and label["fleet"]["allow"] == [a["id"]]


def test_a_paused_fleet_with_nothing_allowed_starts_nothing(os_up, fake_claude):
    store, daemon = os_up["store"], os_up["daemon"]
    fake_claude.hold_turns()
    wo = ops.create_work_order("proj_a", "task")
    ops.pause_fleet()

    _tick(daemon)

    assert store.get_work_order(wo["id"])["status"] == "pending"
    assert store.count_running_turns() == 0


def test_a_pause_holds_a_due_relaunch_and_says_so(os_up, fake_claude, settle_turns,
                                                  monkeypatch):
    store, daemon = os_up["store"], os_up["daemon"]
    wos = [ops.create_work_order("proj_a", f"w{n}") for n in range(2)]
    _park_on_usage_limit(store, wos, fake_claude, settle_turns, daemon, monkeypatch)
    ops.pause_fleet(allow=[wos[0]["id"]])

    _tick(daemon)

    assert worker_session.turn_pause(store, wos[0]["id"]) is None, \
        "the allow-listed order resumes"
    assert worker_session.turn_pause(store, wos[1]["id"]) is not None, \
        "the other stays parked and due"
    held = [e for e in store.events_of_kind(wos[1]["id"], "retry_held")]
    assert held and json.loads(held[-1]["payload"])["cause"] == "fleet_paused"


def test_a_pause_holds_a_message_delivery(os_up, fake_claude, settle_turns):
    store, daemon = os_up["store"], os_up["daemon"]
    wo = ops.create_work_order("proj_a", "task")
    _tick(daemon)
    assert settle_turns(store)
    store.set_status(wo["id"], "running")
    ops.pause_fleet()
    turns = len(store.list_turns(wo["id"]))
    ops.send_message(wo["id"], "keep going")

    daemon.deliver_messages(os_up["daemon"].catalog.projects[0], store,
                            fleet.attach(fleet.read(15, {"proj_a": store}),
                                         os_up["central"]))

    assert len(store.list_turns(wo["id"])) == turns
    assert [m["content"] for m in store.queued_messages(wo["id"])] == ["keep going"]


def test_resume_all_lifts_it_and_resume_needs_a_pause(os_up):
    wo = ops.create_work_order("proj_a", "task")
    with pytest.raises(ops.OpsError):
        ops.resume_fleet([wo["id"]])
    ops.pause_fleet()
    assert ops.resume_fleet([wo["id"]])["allow"] == [wo["id"]]
    assert ops.resume_fleet(everything=True) == {"paused": False}
    assert fleet.load_pause(os_up["central"]) is None


def test_a_typo_is_refused_rather_than_allowing_nothing(os_up):
    ops.pause_fleet()
    with pytest.raises(ops.OpsError):
        ops.resume_fleet(["wo-doesnotexist"])


def test_an_unreadable_pause_record_fails_closed(os_up):
    os_up["central"].set_state(fleet.PAUSE_KEY, "{not json")
    pause = fleet.load_pause(os_up["central"])
    assert pause is not None and not pause.allows("wo-anything")


# -- the ramp and the breaker -----------------------------------------------------------


def test_a_reopen_starts_a_ramp_that_binds_under_a_wide_cap(os_up, fake_claude,
                                                            settle_turns, monkeypatch):
    """THE INCIDENT: eleven resumed under a cap of fifteen. The cap was not the bug —
    a reopen is the one moment the whole backlog is due at once."""
    store, daemon, central = os_up["store"], os_up["daemon"], os_up["central"]
    wos = [ops.create_work_order("proj_a", f"w{n}") for n in range(5)]
    _park_on_usage_limit(store, wos, fake_claude, settle_turns, daemon, monkeypatch)
    # The outage was announced while the window was shut; this tick sees it lift.
    assert central.get_state(fleet.OUTAGE_ANNOUNCED_KEY)

    _tick(daemon)

    assert central.get_state(fleet.REOPENED_KEY), "the reopen was recorded"
    assert store.count_running_turns() == fleet.RAMP_CAP, (
        "every due relaunch resumed at once — the ramp does not bind")
    parked = sum(worker_session.turn_pause(store, w["id"]) is not None for w in wos)
    assert parked == len(wos) - fleet.RAMP_CAP


def test_the_ramp_ends(os_up):
    central = os_up["central"]
    central.set_state(fleet.REOPENED_KEY, "1000")
    assert fleet.ramp(central, now=1000 + 60) is not None
    assert fleet.ramp(central, now=1000 + fleet.RAMP_SECONDS + 1) is None


def test_a_window_spent_again_soon_after_reopening_trips_the_breaker(os_up):
    central = os_up["central"]
    central.set_state(fleet.REOPENED_KEY, "1000")
    state = fleet.Fleet(15, 0, fleet.Outage(project="proj_a", wo_id="wo-x",
                                            reopens_at=99999, message="limit"),
                        at=1000 + 600)

    assert fleet.announce(central, state)

    assert central.get_state(fleet.BREAKER_KEY)
    critical = central.unacked_inbox(level="critical")
    assert critical and "spent again 10 min after it reopened" in critical[0]["title"]
    # The next ramp is slower and longer.
    central.set_state(fleet.REOPENED_KEY, "200000")
    slow = fleet.ramp(central, now=200000 + fleet.RAMP_SECONDS + 1)
    assert slow is not None and slow.cap == fleet.BREAKER_RAMP_CAP and slow.tripped


def test_a_window_that_lasted_clears_the_breaker(os_up):
    central = os_up["central"]
    central.set_state(fleet.REOPENED_KEY, "1000")
    central.set_state(fleet.BREAKER_KEY, "900")
    state = fleet.Fleet(15, 0, fleet.Outage(project="proj_a", wo_id="wo-x",
                                            reopens_at=99999, message="limit"),
                        at=1000 + fleet.BREAKER_SECONDS + 60)

    fleet.announce(central, state)

    assert not central.get_state(fleet.BREAKER_KEY)
