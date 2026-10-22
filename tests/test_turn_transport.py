"""Where a worker turn's process lives, and what survives a daemon restart (issue #133)"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from jarvis import claude_cli, ops, systemd_units, worker_session
from jarvis.catalog import load_catalog
from jarvis.project_store import ProjectStore


@pytest.fixture()
def fleet(jarvis_home, fake_claude, fake_systemd, catalog_file, project):
    """A started OS whose worker turns go onto the transient-unit transport"""
    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    return {"project": catalog.projects[0], "store": ProjectStore(project),
            "claude": fake_claude}


def _start(fleet, hold: bool = False) -> dict:
    """Dispatch one work order's first turn"""
    if hold:
        fleet["hold_gate"] = fleet["claude"].hold_turns()
    wo = ops.create_work_order("proj_a", "task")
    turn, _ = worker_session.start(fleet["store"], fleet["project"],
                                   fleet["store"].get_work_order(wo["id"]), "go")
    return turn


# -- choosing the transport


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
    """`jarvis start --foreground` from a shell, and every dev checkout"""
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, "auto")
    monkeypatch.setattr(systemd_units, "available", lambda: True)
    monkeypatch.setattr(systemd_units, "in_service_cgroup", lambda: False)
    assert systemd_units.use_transient_units() is False


def test_auto_stays_direct_without_a_user_bus(monkeypatch) -> None:
    """`XDG_RUNTIME_DIR` is the half that gets forgotten: without it `systemd-run --user` has no bus to..."""
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, "auto")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(systemd_units, "in_service_cgroup", lambda: True)
    assert systemd_units.available() is False
    assert systemd_units.use_transient_units() is False


def test_the_suite_itself_is_pinned_to_the_direct_transport() -> None:
    """The isolation gate's floor, asserted rather than assumed"""
    assert os.environ[systemd_units.TRANSPORT_ENV] == systemd_units.DIRECT
    assert systemd_units.use_transient_units() is False


def test_the_cgroup_probe_reads_the_real_thing() -> None:
    """Not mocked anywhere else in this file, so it gets one test against /proc"""
    assert isinstance(systemd_units.in_service_cgroup(), bool)


# -- what the unit is told


def test_a_unit_name_is_unique_per_work_order_and_sequence() -> None:
    assert systemd_units.unit_name("wo-abc123", 1) == "jarvis-turn-wo-abc123-1.service"
    assert systemd_units.unit_name("wo-abc123", 1) != systemd_units.unit_name("wo-abc123", 2)


def test_a_unit_name_survives_an_id_systemd_would_reject() -> None:
    """`systemd-run --unit=` refuses characters outside its alphabet"""
    assert systemd_units.unit_name("wo/../ evil$", 3) == "jarvis-turn-wo-----evil--3.service"


def test_the_environment_is_forwarded_because_a_unit_inherits_nothing() -> None:
    """The failure this would otherwise ship: a transient unit gets the systemd USER MANAGER's... (#90)"""
    args = systemd_units.setenv_args({
        "JARVIS_HOME": "/state", "PATH": "/snap/bin:/usr/bin",
        "FORCE_PROMPT_CACHING_5M": "1",
    })
    assert args == ["--setenv=FORCE_PROMPT_CACHING_5M=1", "--setenv=JARVIS_HOME=/state",
                    "--setenv=PATH=/snap/bin:/usr/bin"]


def test_systemd_owned_variables_are_not_forwarded() -> None:
    """`NOTIFY_SOCKET` and the `LISTEN_*` trio are protocol"""
    args = systemd_units.setenv_args({
        "INVOCATION_ID": "abc", "NOTIFY_SOCKET": "/run/x", "LISTEN_FDS": "3",
        "JOURNAL_STREAM": "8:1", "KEEP": "yes",
    })
    assert args == ["--setenv=KEEP=yes"]


def test_a_variable_systemd_could_not_load_is_dropped_not_passed() -> None:
    """One junk name in the daemon's environment would otherwise fail the whole unit at load time"""
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


# -- spawning


def _spawn(tmp_path, **kwargs):
    return claude_cli.spawn_turn(
        "hi", cwd=tmp_path, session_id="s-1", outfile=tmp_path / "1.json",
        errfile=tmp_path / "1.err", **kwargs)


def test_a_turn_runs_in_its_own_unit_and_reports_that_units_main_pid(
        fake_claude, fake_systemd, tmp_path) -> None:
    """The pid recorded is the `claude`, NOT the `systemd-run` that enqueued it"""
    spawned = _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    assert spawned.unit == "jarvis-turn-wo-1-1.service"
    unit = fake_systemd.units["jarvis-turn-wo-1-1.service"]
    assert spawned.pid == unit["pid"]
    assert Path(unit["argv"][0]).name == "claude"
    assert fake_claude.wait_calls(lambda c: "-p" in c["argv"]), "the turn never ran"


def test_a_units_main_pid_outlives_the_process_that_ran_it(
        fake_claude, fake_systemd, tmp_path) -> None:
    """The property every pid assertion in this file rests on"""
    unit = "jarvis-turn-wo-2-1.service"
    spawned = _spawn(tmp_path, unit=unit)
    _wait_until(lambda: not systemd_units.unit_active(unit), "the turn never finished")

    assert systemd_units.main_pid(unit) == spawned.pid


def test_a_stopped_units_pid_does_not_outlive_the_unit(
        fake_claude, fake_systemd, tmp_path) -> None:
    """The bound on the one above: `--collect` unloads a stopped unit"""
    fake_claude.hold_turns()
    unit = "jarvis-turn-wo-3-1.service"
    assert _spawn(tmp_path, unit=unit).pid

    assert systemd_units.stop_unit(unit)

    assert systemd_units.main_pid(unit, attempts=1) is None
    assert not systemd_units.unit_active(unit)


def test_the_turn_in_a_unit_still_buys_the_five_minute_cache_write(
        fake_claude, fake_systemd, tmp_path, monkeypatch) -> None:
    """`--setenv` is now the only way the flag reaches a worker"""
    monkeypatch.delenv("FORCE_PROMPT_CACHING_5M", raising=False)
    _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert calls, "the fake claude was never invoked"
    assert calls[-1]["cache_env"] == {"FORCE_PROMPT_CACHING_5M": "1"}


def test_the_cli_arm_puts_no_setenv_on_this_transport_either(
        fake_claude, fake_systemd, tmp_path, monkeypatch) -> None:
    """`systemd_units.run_prefix` builds `--setenv` from the same `env` dict"""
    monkeypatch.setenv("FORCE_PROMPT_CACHING_5M", "1")
    unit = "jarvis-turn-wo-2-1.service"
    _spawn(tmp_path, unit=unit, cache={})

    setenv = fake_systemd.units[unit]["setenv"]
    assert [k for k in claude_cli.CACHE_TTL_KEYS if k in setenv] == [], setenv
    calls = fake_claude.wait_calls(lambda c: "-p" in c["argv"])
    assert calls and calls[-1]["cache_env"] == {}


def test_a_systemd_that_refuses_still_gets_the_turn_started(
        fake_claude, fake_systemd, tmp_path) -> None:
    """A transport problem must never be why the fleet stops dispatching"""
    fake_systemd.fail("no bus")

    spawned = _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    assert spawned.unit is None, "a failed unit must not be recorded as if it existed"
    assert spawned.pid
    assert fake_claude.wait_calls(lambda c: "-p" in c["argv"]), "the turn never ran"


def test_the_direct_path_records_no_unit(fake_claude, tmp_path) -> None:
    """The suite's default transport, and the shape every pre-existing row has"""
    spawned = _spawn(tmp_path, unit="jarvis-turn-wo-1-1.service")

    assert spawned.unit is None
    assert spawned.pid


# -- the record, and reaching the turn through it


def test_the_turn_row_records_the_unit_it_actually_ran_in(fleet, fake_systemd) -> None:
    turn = _start(fleet)

    assert turn["unit"] == systemd_units.unit_name(turn["wo_id"], turn["seq"])
    assert turn["unit"] in fake_systemd.units
    assert turn["pid"] == fake_systemd.units[turn["unit"]]["pid"]


def test_the_daemons_environment_reaches_the_worker_through_the_unit(
        fleet, fake_systemd, jarvis_home) -> None:
    """End to end against a fake that really drops what it is not handed: a worker in a transient unit..."""
    turn = _start(fleet)

    setenv = fake_systemd.units[turn["unit"]]["setenv"]
    assert setenv["JARVIS_HOME"] == str(jarvis_home)
    assert "INVOCATION_ID" not in setenv


def test_cancelling_stops_the_unit_as_well_as_the_process_group(
        fleet, fake_systemd) -> None:
    """Signalling the process group is not enough once the turn is in a cgroup of its own: the MCP servers..."""
    turn = _start(fleet, hold=True)

    result = worker_session.cancel(fleet["store"], turn["wo_id"])

    assert result["unit"] == turn["unit"]
    stops = [c for c in _ctl_calls(fake_systemd) if "stop" in c["argv"]]
    assert stops, "the unit was never stopped"
    assert turn["unit"] in stops[-1]["argv"]


def test_a_unit_that_never_reported_a_pid_is_not_reaped_as_dead(
        fleet, monkeypatch) -> None:
    """The one case the pid cannot answer"""
    monkeypatch.setattr(systemd_units, "main_pid", lambda unit, **kw: None)
    store = fleet["store"]
    turn = _start(fleet, hold=True)
    assert turn["pid"] is None and turn["unit"]
    # Past the "spawned this instant" grace, so only the unit can save it
    store.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?",
                       (time.time() - 120, turn["id"]))

    assert worker_session.poll(store) == []
    assert store.get_turn(turn["id"])["state"] == "running"


def test_a_finished_units_turn_is_reaped_normally(fleet) -> None:
    """`--collect` means the unit is GONE by the time the reap runs"""
    store = fleet["store"]
    turn = _start(fleet)
    _wait_for_result(Path(turn["outfile"]))

    settled = _poll_until_settled(store)

    assert [t["id"] for t in settled] == [turn["id"]]
    assert store.get_turn(turn["id"])["state"] == "done"


# -- surviving the thing the issue is about


def test_a_turn_is_not_in_the_daemons_process_group(fleet) -> None:
    """The regression in miniature"""
    turn = _start(fleet, hold=True)

    assert os.getpgid(turn["pid"]) != os.getpgid(os.getpid())


@pytest.mark.skipif(not systemd_units.available(),
                    reason="no systemd --user on this host")
@pytest.mark.skipif(os.environ.get("JARVIS_REAL_SYSTEMD") != "1",
                    reason="set JARVIS_REAL_SYSTEMD=1 to spawn real transient units")
def test_against_real_systemd_the_turn_lands_outside_our_cgroup(tmp_path,
                                                                monkeypatch) -> None:
    """Opt-in, because it registers a real unit on the machine running it"""
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, systemd_units.SYSTEMD)
    # A stub rather than /bin/sleep: the turn argv is `claude`'s
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


# -- helpers


def _ctl_calls(fake_systemd) -> list[dict]:
    path = fake_systemd.dir / "ctl-calls.jsonl"
    return ([json.loads(line) for line in path.read_text().splitlines()]
            if path.exists() else [])


def _wait_until(done: Callable[[], bool], message: str,
                timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if done():
            return
        time.sleep(0.02)
    raise AssertionError(message)


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


# -- the transport is DECLARED, not assumed


def _worker_env(project, *, wo: dict | None = None, **worker) -> dict:
    """The env block of the settings file a real dispatch writes"""
    from jarvis.catalog import ProjectSpec, WorkerDefaults
    from jarvis.dispatch import _write_worker_settings

    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=WorkerDefaults(**worker))
    out = _write_worker_settings(spec, wo or {"id": "wo-tt01", "title": "t"})
    return json.loads(out.read_text())["env"]


def test_worker_settings_declare_headless_transport(project, jarvis_home) -> None:
    assert _worker_env(project)[claude_cli.TURN_TRANSPORT_ENV] \
        == claude_cli.TRANSPORT_HEADLESS


def test_transport_value_comes_from_claude_cli_constant(project, jarvis_home) -> None:
    """The argument the file already makes for `PROMPT_CACHE_5M_ENV`: the launcher and the settings file..."""
    import inspect

    from jarvis import dispatch

    assert claude_cli.TRANSPORT_HEADLESS == "headless"
    source = inspect.getsource(dispatch._write_worker_settings)
    assert '"headless"' not in source and "'headless'" not in source


def test_background_spawn_declares_background_transport(project, monkeypatch) -> None:
    """The other transport names itself too"""
    seen: dict = {}

    def fake_run(args, cwd=None, timeout=120, env_extra=None):
        seen["env_extra"] = env_extra
        return "job id: bg-1"

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    claude_cli.spawn_background("do it", project, "[WO wo-tt01] t")

    assert seen["env_extra"][claude_cli.TURN_TRANSPORT_ENV] \
        == claude_cli.TRANSPORT_BACKGROUND


def test_worker_settings_carry_the_require_crew_switch(project, jarvis_home) -> None:
    """The switch the crew refusal reads; off by default since spec 2026-10-22"""
    assert _worker_env(project)["JARVIS_REQUIRE_CREW"] == "0"
    assert _worker_env(project, require_crew=True)["JARVIS_REQUIRE_CREW"] == "1"


def test_worker_settings_carry_the_work_orders_kind(project, jarvis_home) -> None:
    for kind in ("worker", "planner", "manager"):
        env = _worker_env(project, wo={"id": "wo-tt02", "title": "t", "kind": kind})
        assert env["JARVIS_WO_KIND"] == kind
    # absent in the row is the ordinary worker
    assert _worker_env(project)["JARVIS_WO_KIND"] == "worker"
    assert _worker_env(project, wo={"id": "wo-tt03", "title": "t",
                                    "kind": None})["JARVIS_WO_KIND"] == "worker"


def test_the_hook_reads_the_keys_dispatch_writes(project, jarvis_home) -> None:
    """The two halves cannot drift: what `_write_worker_settings` writes is exactly what `hooks` looks up"""
    from jarvis import hooks

    env = _worker_env(project)
    for key in (hooks.TURN_TRANSPORT_ENV, hooks.REQUIRE_CREW_ENV, hooks.WO_KIND_ENV):
        assert key in env, key


def test_env_extra_reaches_the_subprocess_environment(monkeypatch, tmp_path) -> None:
    """`spawn_background`'s transport declaration is only true if `_run` forwards it"""
    seen: dict = {}

    class _Proc:
        returncode = 0
        stdout = "job id: bg-1"
        stderr = ""

    def fake_subprocess_run(argv, **kw):
        seen["env"] = kw["env"]
        return _Proc()

    monkeypatch.setattr(claude_cli.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(claude_cli, "claude_bin", lambda: "claude")
    claude_cli.spawn_background("do it", tmp_path, "[WO wo-tt01] t")

    assert seen["env"][claude_cli.TURN_TRANSPORT_ENV] == claude_cli.TRANSPORT_BACKGROUND
    # and the ambient environment still rides along, or the turn loses PATH
    assert "PATH" in seen["env"]


# -- a brief too big for argv


#: Past `MAX_ARG_STRLEN`, not merely past the door's own threshold
OVER_EXECVE = 131_072 + 1


def test_a_big_brief_reaches_a_transient_unit_on_stdin(
        fake_claude, fake_systemd, tmp_path) -> None:
    """`StandardInput=file:` is the transient unit's counterpart of the `stdin=` the direct path passes"""
    prompt = "z" * OVER_EXECVE + "\nTHE_UNIT_BRIEF_MARKER"

    claude_cli.spawn_turn(prompt, cwd=tmp_path, session_id="s-unit",
                          outfile=tmp_path / "1.json", errfile=tmp_path / "1.err",
                          unit="jarvis-turn-wo-9-1.service")

    promptfile = tmp_path / "1.prompt"
    run = fake_systemd.runs[-1]["argv"]
    assert f"--property=StandardInput=file:{promptfile}" in run
    assert "--property=StandardInput=null" not in run
    unit = fake_systemd.units["jarvis-turn-wo-9-1.service"]
    assert "--" not in unit["argv"], f"the prompt is still in argv; argv={unit['argv']}"
    record = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"])[-1]
    assert record["prompt"] == prompt


def test_the_prompt_file_outlives_the_spawn_and_dies_with_the_reap(fleet) -> None:
    """A context manager here would unlink the file before the detached `claude` read it"""
    store = fleet["store"]
    prompt = "w" * OVER_EXECVE
    wo = ops.create_work_order("proj_a", "task")
    turn, _ = worker_session.start(store, fleet["project"],
                                   store.get_work_order(wo["id"]), prompt)

    promptfile = Path(turn["outfile"]).with_suffix(".prompt")
    assert promptfile.exists(), "unlinked before the turn could read it"
    _wait_for_result(Path(turn["outfile"]))

    assert _poll_until_settled(store), "the turn never settled"
    assert not promptfile.exists(), "the reap left the prompt file behind"


def test_reaping_a_turn_that_never_spawned_does_not_raise(fleet) -> None:
    """A turn row exists before the process does, so `outfile` can still be empty"""
    store = fleet["store"]
    wo = ops.create_work_order("proj_a", "never spawned")
    turn = store.create_turn(wo["id"], kind="dispatch", prompt="work")
    assert turn["outfile"] == ""

    settled = worker_session._reap(store, turn, "proj_a")

    assert settled["state"] == "failed"


def test_a_fake_claude_with_no_argv_prompt_never_blocks_on_a_terminal(
        fake_claude, tmp_path) -> None:
    """`resolved_prompt` reads stdin for any `-p` call carrying no prompt in argv"""
    leader, follower = os.openpty()
    try:
        subprocess.run(  # the pin is that it RETURNS at all
            [os.environ["JARVIS_CLAUDE_BIN"], "-p", "--output-format", "json"],
            stdin=follower, capture_output=True, text=True, timeout=20, cwd=tmp_path)
    finally:
        os.close(follower)
        os.close(leader)

    assert fake_claude.calls[-1]["prompt"] == ""
