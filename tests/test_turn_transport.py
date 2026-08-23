"""Where a worker turn's process lives, and what survives a daemon restart (issue #133).

The bug this file exists for is not visible from inside a turn: `start_new_session=True`
takes the process out of the process GROUP but leaves it in `jarvis.service`'s cgroup, and
`systemd --user` defaults to `KillMode=control-group`, so restarting the daemon unit
SIGTERMs every turn in flight. A killed turn writes no result JSON, matches neither
self-healing retry class, and settles `failed` with an attention flag — taking its
dependents and its parent feature order with it.

So these tests are about the SEAM rather than about the symptom: which transport gets
chosen, what the transient unit is told (its environment above all — a unit inherits the
systemd user manager's, not the daemon's), what is recorded so a later `cancel` can reach
it, and that a systemd which refuses never stops a turn from starting.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from jarvis import claude_cli, ops, systemd_units, worker_session
from jarvis.catalog import load_catalog
from jarvis.project_store import ProjectStore


@pytest.fixture()
def fleet(jarvis_home, fake_claude, fake_systemd, catalog_file, project):
    """A started OS whose worker turns go onto the transient-unit transport."""
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    return {"project": catalog.projects[0], "store": ProjectStore(project),
            "claude": fake_claude}


def _start(fleet, hold: bool = False) -> dict:
    """Dispatch one work order's first turn.

    `hold=True` parks the fake `claude` on a gate file so the turn is still running when
    the assertion looks at it — every test about cancelling, reaping or process groups
    needs that, because the fake otherwise finishes faster than the test can turn round.
    The gate reaches the worker through `--setenv` like everything else, which is itself
    part of what is under test.
    """
    if hold:
        fleet["hold_gate"] = fleet["claude"].hold_turns()
    wo = ops.create_work_order("proj_a", "task")
    return worker_session.start(fleet["store"], fleet["project"],
                                fleet["store"].get_work_order(wo["id"]), "go")


# -- choosing the transport ------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("direct", False),
    ("systemd", True),
    ("DIRECT", False),          # case is not the user's problem
    ("nonsense", True),         # unrecognised → auto, which here detects yes
    ("", True),                 # unset → auto, likewise
])
def test_the_transport_variable_decides_or_defers(monkeypatch, value, expected) -> None:
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, value)
    monkeypatch.setattr(systemd_units, "available", lambda: True)
    monkeypatch.setattr(systemd_units, "in_service_cgroup", lambda: True)
    assert systemd_units.use_transient_units() is expected


def test_auto_stays_direct_outside_a_service_cgroup(monkeypatch) -> None:
    """`jarvis start --foreground` from a shell, and every dev checkout.

    Nothing restarts that daemon as a unit, so there is nothing to survive — and putting
    a dev turn in a transient unit would leave it running after the shell that started
    the OS is gone.
    """
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, "auto")
    monkeypatch.setattr(systemd_units, "available", lambda: True)
    monkeypatch.setattr(systemd_units, "in_service_cgroup", lambda: False)
    assert systemd_units.use_transient_units() is False


def test_auto_stays_direct_without_a_user_bus(monkeypatch) -> None:
    """`XDG_RUNTIME_DIR` is the half that gets forgotten: without it `systemd-run --user`
    has no bus to reach the manager on, however installed the binary is."""
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, "auto")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(systemd_units, "in_service_cgroup", lambda: True)
    assert systemd_units.available() is False
    assert systemd_units.use_transient_units() is False


def test_the_suite_itself_is_pinned_to_the_direct_transport() -> None:
    """The isolation gate's floor, asserted rather than assumed.

    Without it the auto-detection would be RIGHT and that is the problem: this suite is
    usually run BY a Jarvis worker, whose process sits in the daemon's `.service` cgroup,
    so every fake-`claude` turn in every test would register a real transient unit on the
    developer's machine.
    """
    assert os.environ[systemd_units.TRANSPORT_ENV] == systemd_units.DIRECT
    assert systemd_units.use_transient_units() is False


def test_the_cgroup_probe_reads_the_real_thing() -> None:
    """Not mocked anywhere else in this file, so it gets one test against /proc.

    Only that it answers a bool without raising: whether THIS process is in a `.service`
    depends on who ran the suite (a Jarvis worker is; a developer's shell is not).
    """
    assert isinstance(systemd_units.in_service_cgroup(), bool)


# -- what the unit is told -------------------------------------------------------------


def test_a_unit_name_is_unique_per_work_order_and_sequence() -> None:
    assert systemd_units.unit_name("wo-abc123", 1) == "jarvis-turn-wo-abc123-1.service"
    assert systemd_units.unit_name("wo-abc123", 1) != systemd_units.unit_name("wo-abc123", 2)


def test_a_unit_name_survives_an_id_systemd_would_reject() -> None:
    """`systemd-run --unit=` refuses characters outside its alphabet, and a turn that
    cannot be named is a turn that cannot start."""
    assert systemd_units.unit_name("wo/../ evil$", 3) == "jarvis-turn-wo-----evil--3.service"


def test_the_environment_is_forwarded_because_a_unit_inherits_nothing() -> None:
    """The failure this would otherwise ship: a transient unit gets the systemd USER
    MANAGER's environment, not the daemon's, so `JARVIS_HOME`, `JARVIS_ENV` and the
    `Environment=PATH=` the fleet depends on (#90) all vanish unless carried explicitly.
    """
    args = systemd_units.setenv_args({
        "JARVIS_HOME": "/state", "PATH": "/snap/bin:/usr/bin",
        "FORCE_PROMPT_CACHING_5M": "1",
    })
    assert args == ["--setenv=FORCE_PROMPT_CACHING_5M=1", "--setenv=JARVIS_HOME=/state",
                    "--setenv=PATH=/snap/bin:/usr/bin"]


def test_systemd_owned_variables_are_not_forwarded() -> None:
    """`NOTIFY_SOCKET` and the `LISTEN_*` trio are protocol, not configuration: handing
    the parent unit's readiness plumbing to a child unit is how a service ends up
    reporting somebody else's state. systemd sets the right values itself."""
    args = systemd_units.setenv_args({
        "INVOCATION_ID": "abc", "NOTIFY_SOCKET": "/run/x", "LISTEN_FDS": "3",
        "JOURNAL_STREAM": "8:1", "KEEP": "yes",
    })
    assert args == ["--setenv=KEEP=yes"]


def test_a_variable_systemd_could_not_load_is_dropped_not_passed() -> None:
    """One junk name in the daemon's environment would otherwise fail the whole unit at
    load time — i.e. cost the turn, not the variable."""
    assert systemd_units.setenv_args({"not a name": "x", "2BAD": "y", "OK_1": "z"}) \
        == ["--setenv=OK_1=z"]


def test_the_run_prefix_carries_the_files_and_the_working_directory(tmp_path) -> None:
    prefix = systemd_units.run_prefix(
        "jarvis-turn-wo-1-1.service", cwd=tmp_path, outfile=tmp_path / "1.json",
        errfile=tmp_path / "1.err", env={"A": "b"}, description="a turn")
    assert prefix[-1] == "--", "the command must be fenced off from the variadic options"
    assert "--collect" in prefix, "a finished unit has to unload itself"
    assert f"--property=StandardOutput=file:{tmp_path / '1.json'}" in prefix
    assert f"--property=StandardError=file:{tmp_path / '1.err'}" in prefix
    assert f"--working-directory={tmp_path}" in prefix
    assert "--property=StandardInput=null" in prefix


# -- spawning ---------------------------------------------------------------------------


def _spawn(tmp_path, **kwargs):
    return claude_cli.spawn_turn(
        "hi", cwd=tmp_path, session_id="s-1", outfile=tmp_path / "1.json",
        errfile=tmp_path / "1.err", **kwargs)


def test_a_turn_runs_in_its_own_unit_and_reports_that_units_main_pid(
        fake_claude, fake_systemd, tmp_path) -> None:
    """The pid recorded is the `claude`, NOT the `systemd-run` that enqueued it.

    This is the load-bearing detail of the whole change: `systemd-run` exits immediately,
    so recording its pid would have every turn reaped as dead on the daemon's next tick.
    """
    spawned = _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    assert spawned.unit == "jarvis-turn-wo-1-1.service"
    unit = fake_systemd.units["jarvis-turn-wo-1-1.service"]
    assert spawned.pid == unit["pid"]
    assert Path(unit["argv"][0]).name == "claude"
    assert fake_claude.wait_calls(lambda c: "-p" in c["argv"]), "the turn never ran"


def test_the_turn_in_a_unit_still_buys_the_five_minute_cache_write(
        fake_claude, fake_systemd, tmp_path, monkeypatch) -> None:
    """`--setenv` is now the only way the flag reaches a worker, so the property
    `tests/test_prompt_cache_ttl.py` guards has to be re-proved on this transport."""
    monkeypatch.delenv("FORCE_PROMPT_CACHING_5M", raising=False)
    _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert calls, "the fake claude was never invoked"
    assert calls[-1]["cache_env"] == {"FORCE_PROMPT_CACHING_5M": "1"}


def test_a_systemd_that_refuses_still_gets_the_turn_started(
        fake_claude, fake_systemd, tmp_path) -> None:
    """A transport problem must never be why the fleet stops dispatching."""
    fake_systemd.fail("no bus")

    spawned = _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    assert spawned.unit is None, "a failed unit must not be recorded as if it existed"
    assert spawned.pid
    assert fake_claude.wait_calls(lambda c: "-p" in c["argv"]), "the turn never ran"


def test_the_direct_path_records_no_unit(fake_claude, tmp_path) -> None:
    """The suite's default transport, and the shape every pre-existing row has."""
    spawned = _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    assert spawned.unit is None
    assert spawned.pid


# -- the record, and reaching the turn through it ----------------------------------------


def test_the_turn_row_records_the_unit_it_actually_ran_in(fleet, fake_systemd) -> None:
    turn = _start(fleet)

    assert turn["unit"] == systemd_units.unit_name(turn["wo_id"], turn["seq"])
    assert turn["unit"] in fake_systemd.units
    assert turn["pid"] == fake_systemd.units[turn["unit"]]["pid"]


def test_the_daemons_environment_reaches_the_worker_through_the_unit(
        fleet, fake_systemd, jarvis_home) -> None:
    """End to end against a fake that really drops what it is not handed: a worker in a
    transient unit still gets JARVIS_HOME, so the `jarvis` sub-commands it runs read the
    same state the daemon does rather than the user's real fleet."""
    turn = _start(fleet)

    setenv = fake_systemd.units[turn["unit"]]["setenv"]
    assert setenv["JARVIS_HOME"] == str(jarvis_home)
    assert "INVOCATION_ID" not in setenv


def test_cancelling_stops_the_unit_as_well_as_the_process_group(
        fleet, fake_systemd) -> None:
    """Signalling the process group is not enough once the turn is in a cgroup of its
    own: the MCP servers it started are in that cgroup too, and only systemd reaches
    them."""
    turn = _start(fleet, hold=True)

    result = worker_session.cancel(fleet["store"], turn["wo_id"])

    assert result["unit"] == turn["unit"]
    stops = [c for c in _ctl_calls(fake_systemd) if "stop" in c["argv"]]
    assert stops, "the unit was never stopped"
    assert turn["unit"] in stops[-1]["argv"]


def test_a_unit_that_never_reported_a_pid_is_not_reaped_as_dead(
        fleet, monkeypatch) -> None:
    """The one case the pid cannot answer.

    If `MainPID` comes back empty the turn is still running — it just cannot be tracked
    by number — and reaping it would report "the turn's process ended without writing a
    result" about a process that is very much alive. `poll` asks the unit instead.
    """
    monkeypatch.setattr(systemd_units, "main_pid", lambda unit, **kw: None)
    store = fleet["store"]
    turn = _start(fleet, hold=True)
    assert turn["pid"] is None and turn["unit"]
    # Past the "spawned this instant" grace, so only the unit can save it.
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (time.time() - 120, turn["id"]))

    assert worker_session.poll(store) == []
    assert store.get_turn(turn["id"])["state"] == "running"


def test_a_finished_units_turn_is_reaped_normally(fleet) -> None:
    """`--collect` means the unit is GONE by the time the reap runs, which every reader
    has to treat as "not running" rather than as an error."""
    store = fleet["store"]
    turn = _start(fleet)
    _wait_for_result(Path(turn["outfile"]))

    settled = _poll_until_settled(store)

    assert [t["id"] for t in settled] == [turn["id"]]
    assert store.get_turn(turn["id"])["state"] == "done"


# -- surviving the thing the issue is about ----------------------------------------------


def test_a_turn_is_not_in_the_daemons_process_group(fleet) -> None:
    """The regression in miniature.

    Killing by CGROUP is what a daemon-unit restart does, and no fake can stage that.
    What a fake CAN stage is the property underneath it: the turn is not in this
    process's group, so a group-wide signal does not reach it — and `systemd-run` is what
    puts it in a cgroup of its own on top of that.
    """
    turn = _start(fleet, hold=True)

    assert os.getpgid(turn["pid"]) != os.getpgid(os.getpid())


@pytest.mark.skipif(not systemd_units.available(),
                    reason="no systemd --user on this host")
@pytest.mark.skipif(os.environ.get("JARVIS_REAL_SYSTEMD") != "1",
                    reason="set JARVIS_REAL_SYSTEMD=1 to spawn real transient units")
def test_against_real_systemd_the_turn_lands_outside_our_cgroup(tmp_path,
                                                                monkeypatch) -> None:
    """Opt-in, because it registers a real unit on the machine running it.

    This is the assertion the whole change turns on and the one no fake can make: the
    turn's cgroup is not this process's, so restarting the unit this process lives in
    cannot reach it.
    """
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, systemd_units.SYSTEMD)
    # A stub rather than /bin/sleep: the turn argv is `claude`'s, and anything that
    # rejects it exits before systemd can report a MainPID — which reads as a transport
    # failure when it is only a bad stand-in.
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\nsleep 30\n")
    stub.chmod(0o755)
    monkeypatch.setenv(claude_cli.CLAUDE_BIN_ENV, str(stub))
    unit = systemd_units.unit_name(f"wo-real{os.getpid()}", 1)
    spawned = claude_cli.spawn_turn(
        "60", cwd=tmp_path, session_id="s-1", outfile=tmp_path / "o.json",
        errfile=tmp_path / "o.err", unit=unit)
    try:
        assert spawned.unit == unit and spawned.pid
        mine = Path("/proc/self/cgroup").read_text().strip()
        theirs = Path(f"/proc/{spawned.pid}/cgroup").read_text().strip()
        assert theirs != mine
        assert unit.removesuffix(".service") in theirs
    finally:
        systemd_units.stop_unit(unit)


# -- helpers -----------------------------------------------------------------------------


def _ctl_calls(fake_systemd) -> list[dict]:
    path = fake_systemd.dir / "ctl-calls.jsonl"
    return ([json.loads(line) for line in path.read_text().splitlines()]
            if path.exists() else [])


def _has_result(outfile: Path) -> bool:
    return outfile.exists() and bool(outfile.read_text().strip())


def _wait_for_result(outfile: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _has_result(outfile):
            return
        time.sleep(0.02)
    raise AssertionError(f"{outfile} never got a result")


def _poll_until_settled(store, timeout: float = 15.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        settled = worker_session.poll(store)
        if settled:
            return settled
        time.sleep(0.05)
    return []
