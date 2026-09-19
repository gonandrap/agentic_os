"""The scheduler: work orders the OS files without being asked.

docs/superpowers/specs/2026-09-14-the-scheduler.md.

WHAT THESE TESTS ARE BUILT TO AVOID. The scheduler ships DISABLED, so a test that reaches
the daemon without turning it on exercises nothing and still passes — `_enable` is never
a fixture default, and `test_disabled_fleet_files_nothing` is the pin that keeps it
honest. And every property here is about a moment: "a restart does not fire", "one
catch-up not seven", "held for three days". A test that did not MOVE THE CLOCK would take
the same branch twice, get the same answer, and agree with itself on unfixed code
(kn-e6373014) — so `clock` is compulsory in every cadence test below, and the assertions
are on ROWS and ORDER COUNTS rather than on return values.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import invariants, ops, schedule
from jarvis.catalog import CatalogError, ScheduleConfig, load_catalog, parse_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import TERMINAL_STATUSES, WO_ORIGINS, ProjectStore

HOUR = 3600.0
DAY = 24 * HOUR
JOB = schedule.DOCTOR_JOB.id


class _Clock:
    """A hand-advanced `db.now`. See `test_health_sweep._Clock` for why not `freezegun`:
    a bare `monkeypatch.undo()` would revert `jarvis_home` too and every surface would
    then render as an empty OS with no error anywhere."""

    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> float:
        self.at += seconds
        return self.at


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock(time.time())
    monkeypatch.setattr("jarvis.db.now", c)
    return c


def _enable(catalog_file, **overrides) -> None:
    """Turn the scheduler on. NEVER a fixture default — the disabled pin depends on
    reaching the daemon without this call having been made."""
    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": True, **overrides}
    catalog_file.write_text(json.dumps(data))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


def _tick(daemon, store) -> None:
    """One scheduler pass over proj_a, called directly.

    Not through `Daemon.tick`: that pass also dispatches, and a worker launched here
    would settle the order under the very assertions about its status. One test below
    (`test_the_tick_actually_calls_it`) covers the wiring.
    """
    daemon.schedule_tick(daemon.catalog.project("proj_a"), store)


def _scheduled(store) -> list[dict]:
    return [wo for wo in store.list_work_orders(include_hidden=True)
            if wo["origin"] == schedule.ORIGIN]


# -- the cadence, with no store, no catalog and no daemon --------------------------------
#
# `schedule.decide` is the one thing that must never be wrong — "may the OS spend money
# right now" — so it is tested as the pure function it is.


def _state(last_fired_at: float, **over) -> dict:
    return {"job_id": JOB, "last_fired_at": last_fired_at, "last_wo_id": None,
            "held_since": None, "held_reason": None, **over}


def test_not_due_waits():
    d = schedule.decide(_state(1000.0), interval_seconds=DAY, now=1000.0 + HOUR,
                        blocker=None)
    assert d.action == schedule.WAIT


def test_due_with_nothing_in_the_way_fires():
    d = schedule.decide(_state(1000.0), interval_seconds=DAY, now=1000.0 + DAY,
                        blocker=None)
    assert d.action == schedule.FIRE


def test_due_but_previous_order_unsettled_holds_and_says_which():
    d = schedule.decide(_state(1000.0), interval_seconds=DAY, now=1000.0 + DAY,
                        blocker={"id": "wo-abc", "status": "running"})
    assert d.action == schedule.HOLD
    assert "wo-abc" in d.reason and "running" in d.reason


def test_a_week_of_downtime_is_one_catch_up_not_seven(clock, started, store,
                                                     catalog_file):
    """The property `last_fired_at = now` exists for, asserted end to end.

    Advancing by whole intervals instead would look identical on the first tick and
    differ only here — which is why this moves the clock a week rather than a day.
    """
    _enable(catalog_file)
    _tick(started(), store)          # seed the clock without firing
    clock.advance(7 * DAY)
    daemon = started()
    _tick(daemon, store)
    assert len(_scheduled(store)) == 1
    # ...and the backlog does not arrive on the next pass either: the clock was moved to
    # NOW, so the six missed intervals are gone rather than queued. (The one order filed
    # above is `pending`, so it also holds the job — settle it to prove the point is the
    # CLOCK and not the hold.)
    store.update_work_order(_scheduled(store)[0]["id"], status="completed")
    _tick(daemon, store)
    assert len(_scheduled(store)) == 1


# -- seeding: neither enabling nor restarting may fire -----------------------------------


def test_enabling_a_job_does_not_fire_it(clock, started, store, catalog_file):
    _enable(catalog_file)
    _tick(started(), store)
    assert _scheduled(store) == []
    # ...but the clock now exists and is anchored to this moment.
    assert store.schedule_state(JOB)["last_fired_at"] == pytest.approx(clock.at)


def test_a_restart_does_not_fire_it(clock, started, store, catalog_file):
    """Four daemons in a row, an hour apart. A `last_fired_at` that lived in memory —
    or was re-seeded on each boot — would fire on none of these and then on all of them
    the moment somebody looked; one that is absent would fire on the first."""
    _enable(catalog_file)
    for _ in range(4):
        _tick(started(), store)
        clock.advance(HOUR)
    assert _scheduled(store) == []


def test_it_fires_one_interval_after_being_enabled(clock, started, store, catalog_file):
    _enable(catalog_file)
    _tick(started(), store)
    clock.advance(DAY)
    _tick(started(), store)
    orders = _scheduled(store)
    assert len(orders) == 1
    assert orders[0]["status"] == "pending"
    assert store.schedule_state(JOB)["last_wo_id"] == orders[0]["id"]


def test_disabled_fleet_files_nothing(clock, started, store):
    """The pin. No `_enable` call anywhere — if the switch stopped being consulted,
    every other test in this file would still pass and only this one would fail."""
    _tick(started(), store)
    clock.advance(30 * DAY)
    _tick(started(), store)
    assert _scheduled(store) == []
    assert store.schedule_state(JOB) is None, "a disabled job must not even seed a clock"


def test_a_project_can_run_the_sweep_the_fleet_has_off(clock, started, store,
                                                       catalog_file):
    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": False}
    data["projects"][0]["schedule"] = {"enabled": True}
    catalog_file.write_text(json.dumps(data))
    _tick(started(), store)
    clock.advance(DAY)
    _tick(started(), store)
    assert len(_scheduled(store)) == 1


# -- holding: one order per job, ever ----------------------------------------------------


def _fire_once(started, store, clock, catalog_file, **overrides) -> dict:
    _enable(catalog_file, **overrides)
    _tick(started(), store)
    clock.advance(DAY)
    _tick(started(), store)
    return _scheduled(store)[0]


def test_it_never_stacks_a_second_order_behind_an_open_one(clock, started, store,
                                                           catalog_file):
    first = _fire_once(started, store, clock, catalog_file)
    for _ in range(5):
        clock.advance(DAY)
        _tick(started(), store)
    assert [wo["id"] for wo in _scheduled(store)] == [first["id"]]


def test_a_hold_is_recorded_and_keeps_its_first_moment(clock, started, store,
                                                        catalog_file):
    """`held_since` is the number INV-SCHEDULE-HELD judges by, so a hold refreshed on
    every tick would mean the check could never fire however long the job was stuck."""
    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    first_hold = store.schedule_state(JOB)["held_since"]
    assert first_hold == pytest.approx(clock.at)
    clock.advance(DAY)
    _tick(started(), store)
    assert store.schedule_state(JOB)["held_since"] == pytest.approx(first_hold)


def test_holding_does_not_flag_anything(clock, started, store, catalog_file):
    """Neo's ruling: hold SILENTLY. A stack of daily orders is the alarm nobody reads,
    and so is an attention item that says 'still waiting'."""
    first = _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    assert not store.get_work_order(first["id"])["needs_attention"]


def test_a_settled_order_releases_the_job(clock, started, store, catalog_file):
    first = _fire_once(started, store, clock, catalog_file)
    store.update_work_order(first["id"], status="completed")
    clock.advance(DAY)
    _tick(started(), store)
    assert len(_scheduled(store)) == 2


def test_a_deleted_order_does_not_strand_the_job_for_ever(clock, started, store,
                                                           catalog_file):
    """Housekeeping must not be able to kill a scheduler. The clock outlives the work
    order it filed — `scheduled_jobs` carries no foreign key for exactly this."""
    first = _fire_once(started, store, clock, catalog_file)
    store.delete_work_order(first["id"])
    clock.advance(DAY)
    _tick(started(), store)
    assert len(_scheduled(store)) == 1


# -- the attention budget: one visible receipt per job -----------------------------------


def test_yesterdays_receipt_is_hidden_as_todays_is_filed(clock, started, store,
                                                          catalog_file):
    first = _fire_once(started, store, clock, catalog_file)
    store.update_work_order(first["id"], status="completed")
    clock.advance(DAY)
    _tick(started(), store)
    assert store.get_work_order(first["id"])["hidden"]
    visible = [wo for wo in store.list_work_orders() if wo["origin"] == schedule.ORIGIN]
    assert len(visible) == 1 and visible[0]["id"] != first["id"]


def test_a_flagged_receipt_is_left_where_it_is(clock, started, store, catalog_file):
    """Hiding is EARNED. A previous order still asking the user for something keeps its
    place on the listing, whatever the cadence would prefer."""
    first = _fire_once(started, store, clock, catalog_file)
    store.update_work_order(first["id"], status="completed", needs_attention=1,
                            attention_reason="the doctor found six things")
    clock.advance(DAY)
    _tick(started(), store)
    assert not store.get_work_order(first["id"])["hidden"]


def test_a_receipt_holding_an_open_assumption_is_left_where_it_is(clock, started, store,
                                                                   catalog_file):
    first = _fire_once(started, store, clock, catalog_file)
    store.add_assumption(first["id"], "I read INV-FOO as cosmetic")
    store.update_work_order(first["id"], status="completed")
    clock.advance(DAY)
    _tick(started(), store)
    assert not store.get_work_order(first["id"])["hidden"]


# -- labelling ---------------------------------------------------------------------------


def test_a_scheduled_order_says_so_on_its_face(clock, started, store, catalog_file):
    wo = _fire_once(started, store, clock, catalog_file)
    assert wo["origin"] == "schedule"
    assert "schedule" in WO_ORIGINS
    from jarvis.cli import ORIGIN_BADGE
    assert "schedule" in ORIGIN_BADGE, "a listing that cannot name the origin cannot " \
                                       "answer 'why am I paying for this'"


def test_it_is_governed_like_any_other_work_order():
    """NOT in UNGOVERNED_ORIGINS: the daemon dispatches it with a full briefing, so it
    owes `jarvis wo finish` exactly as a typed order does."""
    from jarvis.project_store import UNGOVERNED_ORIGINS
    assert schedule.ORIGIN not in UNGOVERNED_ORIGINS


# -- INV-SCHEDULE-HELD -------------------------------------------------------------------


def _held_violations(store):
    return [v for v in invariants.check_schedule_progresses(store)]


def _status_held(store, project="proj_a"):
    """What `jarvis status` says about this project's scheduler."""
    st = ops.os_status()
    return next(p for p in st["projects"] if p["name"] == project)["schedule_held"]


def test_a_hold_inside_tolerance_is_silent(clock, started, store, catalog_file):
    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)          # held from here
    clock.advance(2 * DAY)           # two intervals of the three allowed
    assert _held_violations(store) == []


def test_a_hold_past_tolerance_is_reported_and_names_the_order(clock, started, store,
                                                                catalog_file):
    first = _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    clock.advance(4 * DAY)
    found = _held_violations(store)
    assert len(found) == 1
    assert found[0].invariant == "INV-SCHEDULE-HELD"
    assert found[0].wo_id == first["id"]
    assert not found[0].repaired, "the remedy is a judgement about that work, not a write"


def _switch_off(catalog_file, **block) -> None:
    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": False, **block}
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)


def test_switching_the_scheduler_off_silences_BOTH_surfaces(clock, started, store,
                                                            catalog_file):
    """A mechanism turned off, with rows still on disk, must not alarm for ever —
    `check_health_sweep_produces_judgements`' lesson, one mechanism over.

    BOTH surfaces, because a hold survives being switched off: `held_since` is cleared by
    a firing and by nothing else, and a project told to stop scheduling will never reach
    one. The invariant guarding it and `jarvis status` not guarding it is the OS
    contradicting itself about a mechanism the user has already turned off — and the
    status line is the one they would see every day.
    """
    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    assert store.schedule_state(JOB)["held_since"], "the row must still say held"
    clock.advance(10 * DAY)

    _switch_off(catalog_file)
    assert _held_violations(store) == []
    assert _status_held(store) == []


def test_dropping_a_job_from_the_roster_silences_both_surfaces_too(clock, started, store,
                                                                   catalog_file):
    """The other half of "still supposed to run": `enabled` may stay true while the job
    itself leaves `jobs`. A filter that read only the switch would keep printing the held
    line for a job the project no longer runs."""
    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    clock.advance(10 * DAY)

    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": True, "jobs": []}
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)
    assert _held_violations(store) == []
    assert _status_held(store) == []


def test_status_surfaces_the_hold_before_the_invariant_does(clock, started, store,
                                                             catalog_file):
    """Neo's condition on holding silently: a job parked behind an unsettled order must
    not be an invisibly dead scheduler."""
    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    held = _status_held(store)
    assert [h["job_id"] for h in held] == [JOB]
    assert held[0]["reason"]


def test_status_says_nothing_about_a_job_that_is_ticking_along(clock, started, store,
                                                               catalog_file):
    _fire_once(started, store, clock, catalog_file)
    assert _status_held(store) == []


def _drop_from_catalog(catalog_file, name="proj_a", **os_block) -> None:
    """Leave the project REGISTERED in the central store but absent from the catalog.

    The real shape: a project onboarded once and later removed from the catalog file.
    `ops.registered_project_paths()` still knows it — `jarvis start` never unregisters —
    so `os_status` still opens its store and reads its rows.
    """
    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": True, **os_block}
    data["projects"] = [p for p in data["projects"] if p["name"] != name]
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)


def test_a_project_the_catalog_no_longer_lists_is_silent_on_BOTH_surfaces(clock, started,
                                                                          store,
                                                                          catalog_file):
    """THE TWO SURFACES MUST RESOLVE CONFIG THE SAME WAY, OR THEY CONTRADICT EACH OTHER.

    They used to resolve separately: `os_status` by project NAME with a disabled fallback,
    `check_schedule_progresses` by resolved PATH with `catalog.os.schedule`'s. A project
    in the central store but absent from the catalog therefore got OPPOSITE answers —
    silent on one surface, and on the other a hold that can never clear, because
    `Daemon.tick` iterates `catalog.projects` and will never fire this project's jobs
    again.

    `os.schedule.enabled` is left TRUE here on purpose: that is the only setting under
    which the old inherit-the-fleet-default fallback speaks at all, so a test with it off
    would pass on the unfixed code.
    """
    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    assert store.schedule_state(JOB)["held_since"], "the row must still say held"
    clock.advance(10 * DAY)

    _drop_from_catalog(catalog_file)
    assert _held_violations(store) == []
    assert _status_held(store) == []


def test_both_surfaces_go_through_one_resolver(clock, started, store, catalog_file):
    """Structural, and it is the only assertion that survives someone re-splitting them.

    The behaviour above can be made to pass twice over by writing the same rule in two
    places — which is what the code did before, and it drifted. This pins that there is
    ONE call: monkeypatching `ops.schedule_config_at` to claim the scheduler is off must
    silence both surfaces, because neither has its own way of asking.
    """
    import jarvis.ops as ops_mod

    _fire_once(started, store, clock, catalog_file)
    clock.advance(DAY)
    _tick(started(), store)
    clock.advance(10 * DAY)
    assert _held_violations(store) and _status_held(store), "both must speak first"

    real = ops_mod.schedule_config_at
    ops_mod.schedule_config_at = lambda path, catalog=None: ScheduleConfig()
    try:
        assert _held_violations(store) == []
        assert _status_held(store) == []
    finally:
        ops_mod.schedule_config_at = real


def test_the_resolver_matches_by_path_not_by_name(clock, started, store, catalog_file):
    """A project registered under one name and catalogued under another is the SAME
    project — the store is opened by path and the invariant only ever holds a path, so
    name is not a key both surfaces can use."""
    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": True}
    data["projects"][0]["name"] = "renamed_in_the_catalog"
    catalog_file.write_text(json.dumps(data))

    path = ops.registered_project_paths()["proj_a"]
    cfg = ops.schedule_config_at(path, load_catalog(catalog_file))
    assert cfg.enabled, "matched by path, so the rename is invisible to the resolver"


# -- the upgrade: a project database that predates `scheduled_jobs` -----------------------


def test_an_old_database_gets_the_table_back_on_open(clock, started, catalog_file):
    """THE ONLY PATH HERE THAT REACHES EVERY EXISTING INSTALL ON DAY ONE.

    Every other test in this file runs against a store the new code created, so all of
    them would pass on a build that never shipped the `CREATE TABLE` at all. What a live
    fleet does on upgrade is open a database written before `scheduled_jobs` existed —
    and `os_status` reads that table for EVERY project unconditionally, with no `enabled`
    guard, so a table that did not come back would take out `jarvis status` and the
    dashboard for the whole fleet rather than only for a project running the scheduler.

    Simulated by dropping the table rather than by checking in a fixture database: the
    drop is verified to bite first (`no such table`), so the recreate is demonstrably
    what saves it and not a swallowed exception somewhere downstream.

    THE SCHEDULER IS ENABLED throughout. `check_schedule_progresses` returns before it
    touches the table when it is off, so the disabled form of this test would assert
    nothing at all about the table — exactly the empty-either-way reading that
    `test_skip_os_drops_the_fleet_wide_checks_and_keeps_the_project_ones` was rewritten
    to avoid.
    """
    import sqlite3

    _enable(catalog_file)
    started()
    path = ops.registered_project_paths()["proj_a"]

    old = ProjectStore(path)
    try:
        with old.conn:
            old.conn.execute("DROP TABLE scheduled_jobs")
        # The drop BITES: that is what makes the assertions below about the recreate.
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            old.list_schedule_states()
    finally:
        old.close()

    # `os_status` FIRST, and deliberately: it is the fleet-wide blast radius. It opens
    # its own store per project and has no `except` around the read, so a table that did
    # not come back takes out `jarvis status` and the dashboard for every project, not
    # only for one running the scheduler.
    st = ops.os_status()
    proj = next(p for p in st["projects"] if p["name"] == "proj_a")
    assert proj["schedule_held"] == []

    reopened = ProjectStore(path)
    try:
        assert reopened.list_schedule_states() == []
        assert list(invariants.check_schedule_progresses(reopened)) == []
    finally:
        reopened.close()


def test_an_old_database_still_schedules_after_the_upgrade(clock, started, catalog_file):
    """The other half: the recreated table is a working clock, not merely a quiet one.

    A `CREATE TABLE` that ran but left the job unable to seed would look identical to a
    healthy upgrade on the test above — nothing raises, `schedule_held` is empty — and
    would only show up as a fleet that silently stopped scheduling after a release.
    """
    _enable(catalog_file)
    started()
    path = ops.registered_project_paths()["proj_a"]
    old = ProjectStore(path)
    try:
        with old.conn:
            old.conn.execute("DROP TABLE scheduled_jobs")
    finally:
        old.close()

    store = ProjectStore(path)
    try:
        _tick(started(), store)          # seeds the clock on the recreated table
        assert _scheduled(store) == []
        clock.advance(DAY)
        _tick(started(), store)
        assert len(_scheduled(store)) == 1
    finally:
        store.close()


# -- the doctor rider --------------------------------------------------------------------


def test_the_owner_runs_the_os_checks_and_nobody_else_does():
    owner = schedule.DOCTOR_JOB.describe(
        schedule.JobContext(project="jarvis_os", owns_os=True))
    other = schedule.DOCTOR_JOB.describe(
        schedule.JobContext(project="my_webapp", owns_os=False))
    assert "--skip-os" not in owner
    assert "jarvis doctor my_webapp --repair --skip-os" in other


def test_os_owner_is_the_project_containing_the_running_install(tmp_path):
    from pathlib import Path
    pkg_root = Path(schedule.__file__).resolve().parents[2]
    assert schedule.os_owner([("elsewhere", tmp_path), ("mine", pkg_root)]) == "mine"


def test_os_owner_falls_back_to_one_project_and_always_the_same_one(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert schedule.os_owner([("a", a), ("b", b)]) == "a"
    assert schedule.os_owner([("a", a), ("b", b)]) == "a"
    assert schedule.os_owner([]) is None


def test_the_daily_run_repairs():
    """Ruled on, not a default: the daemon already applies these repairs every reconcile
    tick, so a daily run that repairs does nothing the OS would not have done anyway
    (`ops.run_doctor`)."""
    body = schedule.DOCTOR_JOB.describe(schedule.JobContext(project="proj_a"))
    assert "--repair" in body


def test_the_description_does_not_enumerate_the_checks():
    """THE EXTENSIBILITY GUARD, and the reason this file exists at all: two sibling
    orders on issue #164 are adding checks to what this run covers. A description that
    listed the invariants would have to be rewritten by each of them, and would be
    quietly wrong in between."""
    body = schedule.DOCTOR_JOB.describe(schedule.JobContext(project="proj_a"))
    named = [c.__name__ for c in (*invariants.INVARIANTS, *invariants.SLOW_INVARIANTS,
                                  *invariants.OS_INVARIANTS)]
    assert len(named) > 10, "the check registry moved; this guard is no longer reading it"
    leaked = [n for n in named if n in body]
    assert leaked == [], f"the daily run hard-codes what it checks: {leaked}"
    # ...and the worker is told not to put them in the record either.
    assert "Do not list the checks that ran" in body


def test_the_run_files_work_rather_than_doing_it():
    body = schedule.DOCTOR_JOB.describe(schedule.JobContext(project="proj_a"))
    assert "jarvis wo create proj_a" in body
    assert "--depends-on" in body, "a fix that has to ship is two orders, in order"
    assert "jarvis wo list proj_a" in body, \
        "it runs every day: the same fault will still be there tomorrow"


def test_only_the_owner_project_gets_a_run_that_carries_the_os_checks(clock, tmp_path,
                                                                     jarvis_home,
                                                                     fake_claude,
                                                                     claude_json,
                                                                     catalog_file):
    """`owns_os=owner == project.name` NEEDS TWO PROJECTS TO MEAN ANYTHING.

    Every other daemon test here runs the single-project fixture, where `proj_a` is the
    owner by `os_owner`'s fallback — so `owns_os` is True on every firing and an inverted
    flag would file `--skip-os` for the whole fleet with every one of them green. This is
    the only test that can tell the two apart, and it asserts the TEXT the worker will
    actually receive rather than the flag, because the text is all the worker sees.
    """
    from jarvis.testing import make_git_project

    second = make_git_project(tmp_path, "proj_b")
    claude_json(second)
    data = json.loads(catalog_file.read_text())
    data["os"]["schedule"] = {"enabled": True}
    data["projects"].append({"name": "proj_b", "path": str(second),
                             "description": "the one that does not own the OS"})
    catalog_file.write_text(json.dumps(data))
    ops.start_os(str(catalog_file), foreground=True)

    # Pin WHO the owner is rather than relying on the fallback's ordering: this test is
    # about the branch, and it must fail when the branch inverts, not when the fallback
    # happens to pick differently.
    monkeypatched = "proj_b"
    original = Daemon._os_owner
    Daemon._os_owner = lambda self: monkeypatched
    try:
        paths = ops.registered_project_paths()
        filed = {}
        for _ in range(2):          # seed, then fire one interval later
            for name in ("proj_a", "proj_b"):
                store = ProjectStore(paths[name])
                try:
                    daemon = Daemon(load_catalog(catalog_file))
                    daemon.schedule_tick(daemon.catalog.project(name), store)
                    filed[name] = _scheduled(store)
                finally:
                    store.close()
            clock.advance(DAY)
    finally:
        Daemon._os_owner = original

    assert len(filed["proj_a"]) == 1 and len(filed["proj_b"]) == 1
    owner_body = filed["proj_b"][0]["description"]
    other_body = filed["proj_a"][0]["description"]
    assert "jarvis doctor proj_b --repair" in owner_body
    assert "--skip-os" not in owner_body
    assert "jarvis doctor proj_a --repair --skip-os" in other_body


# -- run_doctor(include_os=...) -----------------------------------------------------------


def test_skip_os_drops_the_fleet_wide_checks_and_keeps_the_project_ones(started,
                                                                       monkeypatch):
    """BOTH OS-level sources are faked into failing first.

    A healthy fixture reports nothing at either level, so a test that merely asserted
    `scoped["os"] == []` would pass on code that ignored the flag entirely — it would be
    reading an empty list either way. The flag only has an observable effect when there
    is something to drop.
    """
    marker = invariants.Violation(invariant="INV-FAKE-OS", detail="the dashboard is down")
    stranded = invariants.Violation(invariant="INV-FAKE-MARKER", detail="release stuck")
    monkeypatch.setattr(invariants, "check_os", lambda: [marker])
    monkeypatch.setattr(invariants, "check_release_marker", lambda: [stranded])

    everything = ops.run_doctor()
    assert everything["include_os"] is True
    assert [v["invariant"] for v in everything["os"]] == ["INV-FAKE-OS"]
    assert any(p["project"] == "(os)" for p in everything["projects"])

    scoped = ops.run_doctor(include_os=False)
    assert scoped["include_os"] is False
    assert scoped["os"] == []
    assert not any(p["project"] == "(os)" for p in scoped["projects"])
    # ...and the project checks are untouched: this is a narrower sweep, not a quieter one.
    assert [p["project"] for p in scoped["projects"]] == ["proj_a"]
    assert everything["violations"] - scoped["violations"] == 2


def test_the_cli_flag_reaches_it(started, monkeypatch):
    """`--skip-os` is the only way the scheduled worker can ask for this, so a flag
    parsed and dropped would leave the whole §6 decision unimplemented in practice."""
    from jarvis import cli

    seen = {}
    monkeypatch.setattr(ops, "run_doctor",
                        lambda **kw: seen.update(kw) or {"violations": 0, "repair": False,
                                                         "include_os": kw["include_os"],
                                                         "os": [], "projects": []})
    cli.main(["doctor", "proj_a", "--skip-os", "--repair"])
    assert seen["include_os"] is False and seen["repair"] is True
    cli.main(["doctor", "proj_a"])
    assert seen["include_os"] is True


# -- configuration ------------------------------------------------------------------------


def test_an_unknown_job_id_is_refused_at_boot():
    with pytest.raises(CatalogError) as e:
        parse_catalog({"os": {"schedule": {"jobs": ["daily-doctpr"]}}, "projects": []})
    assert "daily-doctpr" in str(e.value) and JOB in str(e.value)


def test_a_project_inherits_every_key_it_does_not_name():
    cat = parse_catalog({
        "os": {"schedule": {"enabled": True, "interval_hours": 6,
                            "held_alarm_intervals": 9}},
        "projects": [{"name": "p", "path": ".", "schedule": {"interval_hours": 12}}],
    })
    p = cat.project("p").schedule
    assert (p.enabled, p.interval_hours, p.held_alarm_intervals) == (True, 12, 9)


def test_jobs_replaces_rather_than_merges():
    """One field, field-level inheritance — `seat_models`' rule (kn-6ca2bcd9). A project
    naming `jobs` owns the whole roster."""
    cat = parse_catalog({
        "os": {"schedule": {"jobs": [JOB]}},
        "projects": [{"name": "p", "path": ".", "schedule": {"jobs": []}}],
    })
    assert cat.project("p").schedule.jobs == ()


def test_the_interval_cannot_be_zero():
    with pytest.raises(CatalogError):
        parse_catalog({"os": {"schedule": {"interval_hours": 0}}, "projects": []})


def test_it_ships_off():
    assert ScheduleConfig().enabled is False
    assert ScheduleConfig().jobs == schedule.JOB_IDS, \
        "the roster default is what makes turning it on ONE setting and not two"


def test_the_whole_block_is_a_safety_key():
    """Enabling the scheduler and shortening its interval are the same act at two
    magnitudes, and both are invisible until the morning they start spending."""
    assert ops.safety_key("os.schedule.enabled")
    assert ops.safety_key("projects.proj_a.schedule.interval_hours")


# -- the wiring ---------------------------------------------------------------------------


def test_the_tick_actually_calls_it(clock, started, store, catalog_file, monkeypatch):
    """Everything above calls `schedule_tick` directly. This is the one assertion that
    it is reached at all — a pass wired to no cadence is dead code that tests green."""
    _enable(catalog_file)
    daemon = started()
    seen = []
    monkeypatch.setattr(Daemon, "schedule_tick",
                        lambda self, project, st: seen.append(project.name))
    daemon.tick_count = 0
    daemon.tick()
    assert seen == ["proj_a"]


def test_the_cadence_reaches_the_pass_once_per_window(clock, started, catalog_file,
                                                      monkeypatch):
    """Driven through `Daemon.tick`, never recomputed here.

    The version of this test that computed `n % SCHEDULE_EVERY_TICKS == 1` itself and
    asserted the length of its own list passed with the gate deleted — it tested
    arithmetic, not the daemon. This runs three whole windows of real ticks and counts
    how many reach the pass, so deleting the gate (every tick) or breaking it (no tick)
    both fail.
    """
    from jarvis.daemon import SCHEDULE_EVERY_TICKS

    _enable(catalog_file)
    daemon = started()
    seen = []
    monkeypatch.setattr(Daemon, "schedule_tick",
                        lambda self, project, st: seen.append(self.tick_count))
    daemon.tick_count = 0
    for _ in range(SCHEDULE_EVERY_TICKS * 3):
        daemon.tick()
    assert len(seen) == 3, f"reached the pass on ticks {seen}"
    # ...and the spacing is the window, not three in a row at the start.
    assert [b - a for a, b in zip(seen, seen[1:])] == [SCHEDULE_EVERY_TICKS] * 2


def test_terminal_statuses_are_what_release_a_job():
    """A blocker is "not settled". If a status left TERMINAL_STATUSES, a job would hold
    behind it for ever without anything saying so."""
    assert set(TERMINAL_STATUSES) == {"completed", "cancelled", "failed"}
