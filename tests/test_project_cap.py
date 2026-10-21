"""`max_concurrent` counts turns that are executing, not records that are waiting (#134)"""

from __future__ import annotations

import json

import pytest

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore, resume_spends_slot


@pytest.fixture()
def boot(tmp_path, jarvis_home, fake_claude, project):
    """The OS against a catalog this test wrote, so `max_concurrent` is the subject"""
    def _boot(max_concurrent: int = 2) -> Daemon:
        path = tmp_path / f"catalog-{max_concurrent}.json"
        path.write_text(json.dumps({
            "os": {
                "defaults": {"model": "sonnet", "max_concurrent": max_concurrent,
                             "max_in_flight": 50},
                "notifications": {"sinks": ["log"]},
            },
            "projects": [
                {"name": "proj_a", "path": str(project), "description": "test project"},
            ],
        }))
        ops.start_os(str(path), foreground=True)
        return Daemon(load_catalog(path))

    return _boot


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _parked(store: ProjectStore, title: str, status: str) -> dict:
    """A work order with a finished turn behind it, parked in `status`"""
    wo = ops.create_work_order("proj_a", title, description="something to do")
    store.set_status(wo["id"], "running", session_id=f"s-{wo['id']}")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
    store.finish_turn(turn["id"], "done", result="asked a question")
    store.set_status(wo["id"], status)
    return store.get_work_order(wo["id"])


def _turns(store: ProjectStore, wo_id: str) -> int:
    return len(store.list_turns(wo_id))


# -- 1. what spends a slot


def test_only_an_executing_turn_spends_a_slot(boot, store):
    """The narrowing itself, one status at a time, against its own control"""
    boot()
    for status in ("waiting_input", "validating"):
        wo = _parked(store, f"parked in {status}", status)
        assert store.count_active() == 0, f"{status} must not spend a slot"
        store.set_status(wo["id"], "completed")

    for status in ("dispatching", "running"):
        wo = _parked(store, f"live in {status}", status)
        assert store.count_active() == 1, f"{status} must spend a slot"
        store.set_status(wo["id"], "completed")


def test_a_full_project_still_claims_past_parked_orders(boot, store):
    """The user-visible bug in #134: five records, one turn, and nothing dispatches"""
    daemon = boot(max_concurrent=2)
    spec = daemon.catalog.project("proj_a")
    _parked(store, "parked on Neo", "waiting_input")
    _parked(store, "in the panel", "validating")

    work = ops.create_work_order("proj_a", "new work", description="something")
    daemon.dispatch_pending(spec, store)

    assert store.get_work_order(work["id"])["status"] != "pending"

    # The control: two orders actually running, and the same call claims nothing
    _parked(store, "running one", "running")
    _parked(store, "running two", "running")
    store.set_status(work["id"], "completed")
    blocked = ops.create_work_order("proj_a", "more work", description="something")
    daemon.dispatch_pending(spec, store)

    assert store.get_work_order(blocked["id"])["status"] == "pending"


# -- 2. the herd, held at delivery


def test_rejected_rounds_do_not_all_resume_at_once(boot, store):
    """THE test in this file"""
    daemon = boot(max_concurrent=2)
    spec = daemon.catalog.project("proj_a")
    rejected = [_parked(store, f"round {i} rejected", "validating") for i in range(4)]
    for wo in rejected:
        store.queue_message(wo["id"], "the panel rejected this: fix the tests")

    daemon.deliver_messages(spec, store)

    resumed = [wo for wo in rejected
               if store.get_work_order(wo["id"])["status"] == "running"]
    assert len(resumed) == 2, "the cap let more than a full project's worth resume"
    assert store.count_active() == 2

    held = [wo for wo in rejected if wo not in resumed]
    for wo in held:
        row = store.get_work_order(wo["id"])
        assert row["status"] == "validating", "a held order must not move"
        assert not row["needs_attention"], "being held is not raised"
        assert _turns(store, wo["id"]) == 1, "no turn went out"
    assert {m["wo_id"] for m in store.queued_messages()} == {wo["id"] for wo in held}, \
        "a held message stays queued, so the next tick delivers it"


def test_the_oldest_waiting_conversation_takes_the_free_slot(boot, store):
    """Chronological, because `queued_messages` is: holding must not reorder a queue"""
    daemon = boot(max_concurrent=1)
    spec = daemon.catalog.project("proj_a")
    first = _parked(store, "asked first", "waiting_input")
    second = _parked(store, "asked second", "waiting_input")
    store.queue_message(first["id"], "answer for the first")
    store.queue_message(second["id"], "answer for the second")

    daemon.deliver_messages(spec, store)

    assert store.get_work_order(first["id"])["status"] == "running"
    assert store.get_work_order(second["id"])["status"] == "waiting_input"


def test_a_held_message_goes_out_as_soon_as_a_slot_frees(boot, store):
    """Held, not dropped — the property that makes the hold safe to be silent about"""
    daemon = boot(max_concurrent=1)
    spec = daemon.catalog.project("proj_a")
    busy = _parked(store, "already running", "running")
    parked = _parked(store, "waiting on Neo", "waiting_input")
    store.queue_message(parked["id"], "Neo says: yes")

    daemon.deliver_messages(spec, store)
    assert store.get_work_order(parked["id"])["status"] == "waiting_input"

    store.set_status(busy["id"], "completed")
    daemon.deliver_messages(spec, store)

    assert store.get_work_order(parked["id"])["status"] == "running"
    assert store.queued_messages() == []


def test_an_order_already_holding_a_slot_is_never_held(boot, store):
    """It is the order the cap is counting: charging it again would refuse to resume the very work the..."""
    daemon = boot(max_concurrent=1)
    spec = daemon.catalog.project("proj_a")
    running = _parked(store, "mid-conversation", "running")
    store.queue_message(running["id"], "one more thing")

    assert store.count_active() == 1, "the project is full"
    daemon.deliver_messages(spec, store)

    assert store.queued_messages() == []
    assert _turns(store, running["id"]) == 2


def test_a_manager_is_exempt_from_the_hold_as_well_as_from_the_count(boot, store):
    """The two must agree"""
    daemon = boot(max_concurrent=1)
    spec = daemon.catalog.project("proj_a")
    _parked(store, "a child, running", "running")
    fo = ops.create_feature_order("proj_a", "a feature", description="do the thing")
    manager = store.create_manager_order(fo["id"])
    store.update_work_order(manager["id"], session_id="s-manager")
    turn = store.create_turn(manager["id"], kind="dispatch", prompt="coordinate")
    store.finish_turn(turn["id"], "done", result="waiting")
    store.set_status(manager["id"], "waiting_input")
    store.queue_message(manager["id"], "your child finished")

    assert store.count_active() == 1, "the project is full and the manager is not in it"
    daemon.deliver_messages(spec, store)

    assert store.queued_messages() == []
    assert store.get_work_order(manager["id"])["status"] == "running"
    assert resume_spends_slot(store.get_work_order(manager["id"])) is False


# -- 3. the second door: the retry sweep


def test_an_auth_parked_order_is_held_by_the_project_cap(boot, store, signin):
    """`retry_paused_turns` resumes without dispatching, so the cap has to be re-checked here too"""
    from jarvis import worker_session

    daemon = boot(max_concurrent=1)
    spec = daemon.catalog.project("proj_a")
    _parked(store, "already running", "running")

    parked = ops.create_work_order("proj_a", "signed out mid-turn",
                                   description="something")
    store.set_status(parked["id"], "running", session_id="s-auth")
    turn = store.create_turn(parked["id"], kind="dispatch", prompt="go")
    store.finish_turn(turn["id"], "failed", error="Invalid API key · Please run /login",
                      terminal_reason="api_error")
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (1000.0, turn["id"]))
    store.set_status(parked["id"], "waiting_input")
    signin(at=2000.0)

    pause = worker_session.turn_pause(store, parked["id"])
    assert pause is not None and pause.resumable and pause.due(), "the sweep would act"

    daemon.retry_paused_turns(spec, store)
    assert _turns(store, parked["id"]) == 1, "held: no relaunch while the project is full"
    assert store.get_work_order(parked["id"])["status"] == "waiting_input"

    for wo in store.list_work_orders(statuses=("running",)):
        store.set_status(wo["id"], "completed")  # the slot frees

    daemon.retry_paused_turns(spec, store)
    assert _turns(store, parked["id"]) == 2, "and picked up by the next pass"


def test_a_usage_parked_order_is_not_charged_for_the_slot_it_holds(boot, store):
    """The pairing"""
    from jarvis import worker_session

    daemon = boot(max_concurrent=1)
    spec = daemon.catalog.project("proj_a")
    refused = ops.create_work_order("proj_a", "refused by the window",
                                    description="something")
    store.set_status(refused["id"], "running", session_id="s-refused")
    turn = store.create_turn(refused["id"], kind="dispatch", prompt="go")
    store.finish_turn(
        turn["id"], "failed",
        error="You've hit your session limit · resets 11:40am (America/Los_Angeles)",
        terminal_reason="api_error", api_error_status=429)
    store.conn.execute("UPDATE wo_turns SET ended_at=? WHERE id=?",
                       (1000.0, turn["id"]))

    assert store.count_active() == 1, "the project is full, and this order is why"
    pause = worker_session.turn_pause(store, refused["id"])
    assert pause is not None and pause.due()

    daemon.retry_paused_turns(spec, store)

    assert _turns(store, refused["id"]) == 2, "its own slot is the one it resumes into"
