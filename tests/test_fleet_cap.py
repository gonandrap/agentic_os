"""The account, which is not a project (src/jarvis/fleet.py).

On 2026-09-02 every ready child of fo-6269be9a was dispatched at once. Three of the four
paid 51-72 seconds of Opus time to be told the session window was already spent, and when
it reopened all four resumed in the same second — 362k + 361k + 325k + 322k tokens
re-written at the cache-WRITE rate, and four `uv run pytest` runs on one machine.

Two mechanisms, tested separately because they fail separately:
  * a cap on worker turns in flight, fleet-wide, because the limit is the ACCOUNT's
  * a hold on dispatch while Claude is refusing turns, to the reset the refusal named

The refusal itself was already recognised (`worker_session.turn_pause`, and
test_rate_limit_retry.py); what was missing is that ONE work order learning the window is
spent tells the OS nothing about the other three.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

from jarvis import fleet, invariants, ops, worker_session
from jarvis.catalog import DEFAULT_MAX_IN_FLIGHT, CatalogError, load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project

#: The refusal all four workers got, verbatim off their turn results.
REFUSAL = "You've hit your session limit · resets 11:40am (America/Los_Angeles)"

#: The incident, as `.jarvis/turns/<wo>/<seq>.json` recorded it: when the turn ended, and
#: how much API time it had already billed. Three of the four are NOT the free 0ms/$0
#: refusal the usage-limit path was designed around — they ran a minute of Opus first,
#: which is exactly the spend a fleet-wide hold exists to stop the siblings repeating.
INCIDENT = [
    ("wo-0a512472", "2026-09-02T15:23:16Z", 51940, 0.61),
    ("wo-c83d7e93", "2026-09-02T15:23:17Z", 59347, 1.00),
    ("wo-17eca38d", "2026-09-02T15:23:34Z", 71784, 0.97),
    ("wo-a4bd6958", "2026-09-02T16:36:26Z", 0, 0.00),  # the planner, refused before it ran
]


def _epoch(stamp: str) -> float:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def _catalog(tmp_path, projects: dict[str, object], max_in_flight: int | None = None,
             name: str = "catalog-fleet.json"):
    defaults: dict[str, object] = {"model": "sonnet"}
    if max_in_flight is not None:
        defaults["max_in_flight"] = max_in_flight
    path = tmp_path / name
    path.write_text(json.dumps({
        "os": {"defaults": defaults, "notifications": {"sinks": ["log"]}},
        "projects": [{"name": n, "path": str(p), "description": n}
                     for n, p in projects.items()],
    }))
    return path


@pytest.fixture()
def two_projects(tmp_path, claude_json, project):
    """A catalog with a second project, so "fleet-wide" is more than a claim."""
    other = make_git_project(tmp_path, "proj_b")
    claude_json(other)
    return project, other


def _tick(daemon):
    """One tick with the retry pass ON (the daemon runs it every RETRY_EVERY_TICKS)."""
    daemon.tick_count = 0
    daemon.tick()


def _refuse(store: ProjectStore, wo_id: str, ended: float, api_ms: int,
            error: str = REFUSAL):
    """The turn the CLI refused, recorded the way `worker_session.poll` records it.

    `ended_at` is written directly because `finish_turn` stamps the wall clock, and the
    whole point of replaying 2026-09-02 is that the reset is resolved against the moment
    the turn ENDED rather than against the clock of whoever is asking.
    """
    turn = store.create_turn(wo_id, kind="dispatch", prompt="do the thing")
    store.finish_turn(turn["id"], "failed", error=error,
                      terminal_reason="api_error", api_error_status=429)
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (ended - api_ms / 1000.0, ended, turn["id"]))
    return store.latest_turn(wo_id)


# -- the setting -----------------------------------------------------------------------


def test_the_cap_is_fleet_wide_with_no_per_project_override(tmp_path, project):
    """One number, because the account is not divided among projects. A project that
    named its own share would be naming a share of something it does not own."""
    cat = load_catalog(_catalog(tmp_path, {"proj_a": project}, max_in_flight=7))
    assert cat.os.max_in_flight == 7
    assert not hasattr(cat.projects[0], "max_in_flight")


def test_the_cap_has_a_default_and_refuses_a_nonsense_one(tmp_path, project):
    assert load_catalog(_catalog(tmp_path, {"proj_a": project})).os.max_in_flight \
        == DEFAULT_MAX_IN_FLIGHT
    with pytest.raises(CatalogError, match="max_in_flight must be >= 1"):
        load_catalog(_catalog(tmp_path, {"proj_a": project}, max_in_flight=0))


# -- the cap ---------------------------------------------------------------------------


def test_the_cap_holds_back_an_over_limit_dispatch(jarvis_home, fake_claude, tmp_path,
                                                   project):
    """THE THING THAT DID NOT EXIST. Four ready children were dispatched at once because
    nothing counted turns across the fleet; with a cap of 2 the third waits."""
    catalog_path = _catalog(tmp_path, {"proj_a": project}, max_in_flight=2)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    fake_claude.hold_turns()  # so the launched turns stay in flight to be counted

    wos = [ops.create_work_order("proj_a", f"child {n}") for n in range(3)]
    _tick(daemon)

    store = ProjectStore(project)
    assert store.count_running_turns() == 2, "the cap did not bind"
    held = store.get_work_order(wos[2]["id"])
    assert held["status"] == "pending", "the third order should not have been claimed"
    assert not held["needs_attention"], (
        "waiting for a slot is the system working, not a decision the user owes — "
        "the same rule as a dependency-blocked order")
    store.close()


def test_a_held_back_order_is_never_left_claimed_with_no_turn(jarvis_home, fake_claude,
                                                              tmp_path, project):
    """The cap is checked BEFORE the claim. Claimed-then-held would leave the order in
    `dispatching` with no turn, which `settle_work_order` fails as "worker turn never
    started" — a cap that manufactured the failure it exists to prevent."""
    catalog_path = _catalog(tmp_path, {"proj_a": project}, max_in_flight=1)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    fake_claude.hold_turns()

    ops.create_work_order("proj_a", "first")
    held = ops.create_work_order("proj_a", "second")
    _tick(daemon)
    _tick(daemon)

    store = ProjectStore(project)
    assert store.get_work_order(held["id"])["status"] == "pending"
    assert store.latest_turn(held["id"]) is None
    store.close()


def test_the_cap_counts_the_whole_fleet_not_one_project(jarvis_home, fake_claude,
                                                        tmp_path, two_projects):
    """A per-project cap could not have prevented the incident and no arrangement of
    them can: with one order ready in each of two projects and a cap of 1, exactly one
    turn goes out — and the second project's pass must see the first project's launch."""
    a, b = two_projects
    catalog_path = _catalog(tmp_path, {"proj_a": a, "proj_b": b}, max_in_flight=1)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    fake_claude.hold_turns()

    ops.create_work_order("proj_a", "ship a")
    ops.create_work_order("proj_b", "ship b")
    _tick(daemon)

    sa, sb = ProjectStore(a), ProjectStore(b)
    assert sa.count_running_turns() + sb.count_running_turns() == 1
    sa.close()
    sb.close()


def test_a_turn_parked_on_a_question_does_not_spend_a_fleet_slot(tmp_path, project):
    """The unit is a turn in flight, NOT an active work order. `count_active` counts
    `waiting_input`, and an order parked on a Neo question spends no tokens — rationing
    the account against it would ration the fleet against orders that are not using the
    thing that ran out (kn-5c32dde8)."""
    store = ProjectStore(project)
    wo = store.create_work_order("parked on a question", origin="manual")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
    store.finish_turn(turn["id"], "done", result="asked Neo")
    store.set_status(wo["id"], "waiting_input")

    assert store.count_active() == 1
    assert fleet.read(3, {"proj_a": store}).in_flight == 0
    store.close()


# -- the outage ------------------------------------------------------------------------


def test_the_four_refusals_are_one_outage_and_burn_no_retries(tmp_path, project):
    """REPLAYING 2026-09-02. Four turns, the same refusal, three of them already a
    minute deep in Opus time. Every one is a usage-limit pause rather than a transient
    429, all four name the same reopening, and none is on its second attempt."""
    store = ProjectStore(project)
    for wo_id, stamp, api_ms, _cost in INCIDENT:
        store.create_work_order(wo_id, origin="manual", wo_id=wo_id)
        store.set_status(wo_id, "running")
        _refuse(store, wo_id, _epoch(stamp), api_ms)

    pauses = {wo_id: worker_session.turn_pause(store, wo_id)
              for wo_id, *_ in INCIDENT}
    for wo_id, pause in pauses.items():
        assert pause is not None, wo_id
        assert pause.reason == worker_session.PAUSE_USAGE_LIMIT, (
            f"{wo_id} was read as a transient 429; a spend-shaped refusal must never "
            "enter the backoff")
        assert pause.attempts == 1, f"{wo_id} burned a retry"
        assert not pause.exhausted
    # 11:40am America/Los_Angeles on the day of the incident, which every refusal states
    # and every turn ended before.
    reopens = {p.reset_at for p in pauses.values() if p is not None}
    assert len(reopens) == 1, f"the same window resolved to {len(reopens)} moments"

    state = fleet.read(3, {"proj_a": store}, now=_epoch("2026-09-02T16:40:00Z"))
    assert state.outage is not None
    assert state.outage.reopens_at == reopens.pop()
    assert state.blocked(now=_epoch("2026-09-02T16:40:00Z"))
    assert not state.blocked(now=_epoch("2026-09-02T18:41:00Z")), (
        "the hold must end at the reset the refusal named")
    store.close()


def test_one_inbox_entry_for_the_outage_not_four(jarvis_home, tmp_path, project):
    """Four entries saying the same sentence is not four times the information."""
    store = ProjectStore(project)
    central = CentralStore()
    for wo_id, stamp, api_ms, _cost in INCIDENT:
        store.create_work_order(wo_id, origin="manual", wo_id=wo_id)
        store.set_status(wo_id, "running")
        _refuse(store, wo_id, _epoch(stamp), api_ms)

    now = _epoch("2026-09-02T16:40:00Z")
    # Every tick between the refusal and the reset re-derives the same outage.
    said = [fleet.announce(central, fleet.read(3, {"proj_a": store}, now=now))
            for _ in range(5)]
    assert said == [True, False, False, False, False]
    entries = central.unacked_inbox()
    assert len(entries) == 1
    assert "usage limit" in entries[0]["title"]
    assert entries[0]["wo_id"] in {wo_id for wo_id, *_ in INCIDENT}

    # A LATER outage is news again — the receipt is cleared the moment nothing is
    # refused, so a second window does not go unannounced behind the first one's flag.
    after = _epoch("2026-09-02T18:41:00Z")
    assert fleet.announce(central, fleet.read(3, {"proj_a": store}, now=after)) is False
    _refuse(store, "wo-0a512472", after, 0)
    assert fleet.announce(central, fleet.read(3, {"proj_a": store}, now=after)) is True
    store.close()
    central.close()


def test_a_refusal_in_one_project_holds_dispatch_in_another(jarvis_home, fake_claude,
                                                            tmp_path, two_projects,
                                                            settle_turns, monkeypatch):
    """THE SECOND HALF OF THE INCIDENT. One work order learning the window is spent used
    to tell the OS nothing about the others, so each of them paid to find out."""
    # The hold ends at `TurnPause.retry_at`, which is floored at 60s past the refusal so
    # that nothing retries into the same one. Real for the fleet too — the account said
    # no a moment ago — but not what this test is about, and no test can wait it out.
    monkeypatch.setattr(worker_session, "RATE_LIMIT_MIN_DELAY", 0)
    a, b = two_projects
    catalog_path = _catalog(tmp_path, {"proj_a": a, "proj_b": b}, max_in_flight=5)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    sa, sb = ProjectStore(a), ProjectStore(b)

    fake_claude.turns_rate_limited(reset="11:59pm (UTC)")
    first = ops.create_work_order("proj_a", "the one that finds out")
    _tick(daemon)
    assert settle_turns(sa)
    _tick(daemon)
    assert worker_session.turn_pause(sa, first["id"]) is not None

    later = ops.create_work_order("proj_b", "the one that must not repeat it")
    _tick(daemon)
    assert sb.get_work_order(later["id"])["status"] == "pending", (
        "a second project was dispatched into a window already known to be spent")
    assert sb.latest_turn(later["id"]) is None, "it paid to learn what was already known"
    assert not sb.get_work_order(later["id"])["needs_attention"]

    # The window reopens; the held order goes out on the next pass with nobody typing.
    fake_claude.turns_recover()
    sa.conn.execute("UPDATE wo_turns SET error=? WHERE id=?",
                    ("Claude AI usage limit reached|1000000000",
                     sa.latest_turn(first["id"])["id"]))
    _tick(daemon)
    assert sb.latest_turn(later["id"]) is not None, "the hold outlasted the window"
    sa.close()
    sb.close()


def test_an_exhausted_refusal_stops_holding_the_fleet(tmp_path, project):
    """A refusal nobody will retry is on its way to `failed` and asking for the user.
    Holding the account on it would stop the fleet for ever on one mis-parse."""
    store = ProjectStore(project)
    store.create_work_order("stuck", origin="manual", wo_id="wo-stuck")
    store.set_status("wo-stuck", "running")
    ended = _epoch("2026-09-02T15:23:16Z")
    for n in range(worker_session.MAX_RATE_LIMIT_RETRIES + 1):
        _refuse(store, "wo-stuck", ended + n, 0)

    pause = worker_session.turn_pause(store, "wo-stuck")
    assert pause is not None and pause.exhausted
    assert fleet.read(3, {"proj_a": store}, now=ended + 100).outage is None
    store.close()


def test_the_retry_pass_is_staggered_by_the_cap(jarvis_home, fake_claude, tmp_path,
                                                project, settle_turns, monkeypatch):
    """WHERE THE CACHE MONEY WENT. Four siblings refused by one window come due in the
    same second; resuming together re-wrote 1.37M tokens and ran four test suites on one
    machine. Nothing is lost by staggering — a turn held here is picked up by the next
    pass, because the pause is re-derived and stays due."""
    monkeypatch.setattr(worker_session, "RATE_LIMIT_MIN_DELAY", 0)
    # Four dispatched under a cap that does not bind, so all four are really parked —
    # then the cap the resume has to obey.
    ops.start_os(str(_catalog(tmp_path, {"proj_a": project}, max_in_flight=4)),
                 foreground=True)
    open_wide = Daemon(load_catalog(
        _catalog(tmp_path, {"proj_a": project}, max_in_flight=4)))
    store = ProjectStore(project)

    fake_claude.turns_rate_limited(reset="11:59pm (UTC)")
    wos = [ops.create_work_order("proj_a", f"sibling {n}") for n in range(4)]
    _tick(open_wide)
    assert settle_turns(store)
    _tick(open_wide)
    assert all(worker_session.turn_pause(store, wo["id"]) is not None for wo in wos), \
        "every sibling should be parked on the usage limit"

    # The window reopens for all four in the same second, which is what happened at
    # 18:40Z on 2026-09-02.
    fake_claude.turns_recover()
    fake_claude.hold_turns()
    store.conn.execute("UPDATE wo_turns SET error=? WHERE state='failed'",
                       ("Claude AI usage limit reached|1000000000",))
    capped = Daemon(load_catalog(
        _catalog(tmp_path, {"proj_a": project}, max_in_flight=2, name="capped.json")))
    _tick(capped)

    assert store.count_running_turns() == 2, (
        "all four resumed at once — the cap does not reach the retry pass")
    assert sum(worker_session.turn_pause(store, wo["id"]) is not None for wo in wos) == 2, \
        "the two held back must stay parked and due, for the next pass to pick up"
    store.close()


# -- what the user sees ------------------------------------------------------------------


def test_a_held_back_order_says_what_it_is_waiting_for(tmp_path, project):
    """`jarvis status` renders through `status_label`, so saying it here says it
    everywhere. A bare "pending" promises "will start as soon as a slot frees"."""
    store = ProjectStore(project)
    wo = store.create_work_order("waiting", origin="manual")
    row = store.get_work_order(wo["id"])

    assert invariants.status_label(store, row, fleet.Fleet(3, 0)) == "pending"
    at_cap = invariants.status_label(store, row, fleet.Fleet(3, 3))
    assert at_cap == "pending — 3 of 3 worker turns already in flight"

    held = fleet.Fleet(3, 0, fleet.Outage(
        project="proj_a", wo_id="wo-0a512472",
        reopens_at=time.time() + 3600, message=REFUSAL))
    assert "usage window is spent" in invariants.status_label(store, row, held)
    # No fleet view, no fleet clause: a caller holding one project's store cannot know,
    # and a guess would be worse than the bare status.
    assert invariants.status_label(store, row) == "pending"
    store.close()


def test_jarvis_status_names_the_cap_and_does_not_raise_attention(
        jarvis_home, fake_claude, tmp_path, project):
    a_status = None
    catalog_path = _catalog(tmp_path, {"proj_a": project}, max_in_flight=1)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    fake_claude.hold_turns()

    ops.create_work_order("proj_a", "first")
    held = ops.create_work_order("proj_a", "second")
    _tick(daemon)

    a_status = ops.os_status(catalog=load_catalog(catalog_path))
    labels = {wo["id"]: wo["status_label"]
              for wo in a_status["projects"][0]["open_work_orders"]}
    assert labels[held["id"]] == "pending — 1 of 1 worker turns already in flight"
    assert not [item for item in a_status["attention"]
                if item.get("wo_id") == held["id"]], (
        "waiting for a slot must not reach the attention strip")
