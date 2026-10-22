"""Running a worker turn in its own transient systemd unit, outside the daemon's cgroup (#133)"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("jarvisd.systemd")

#: Which transport a worker turn is spawned onto
TRANSPORT_ENV = "JARVIS_TURN_TRANSPORT"
AUTO, SYSTEMD, DIRECT = "auto", "systemd", "direct"

#: Binary overrides, mirroring `claude_cli.CLAUDE_BIN_ENV`
SYSTEMD_RUN_BIN_ENV = "JARVIS_SYSTEMD_RUN_BIN"
SYSTEMCTL_BIN_ENV = "JARVIS_SYSTEMCTL_BIN"

#: Variables systemd sets for ITS OWN bookkeeping of the unit we are running inside
_SYSTEMD_OWNED = frozenset({
    "INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "MANAGERPID", "SYSTEMD_EXEC_PID",
    "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "WATCHDOG_PID", "WATCHDOG_USEC",
    "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE",
})

#: A shell-ish name is the only shape `Environment=` can round-trip
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: systemd unit names accept a narrow alphabet
_UNIT_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def systemd_run_bin() -> str:
    return os.environ.get(SYSTEMD_RUN_BIN_ENV, "systemd-run")


def systemctl_bin() -> str:
    return os.environ.get(SYSTEMCTL_BIN_ENV, "systemctl")


def transport() -> str:
    """The configured transport"""
    value = os.environ.get(TRANSPORT_ENV, "").strip().lower()
    return value if value in (AUTO, SYSTEMD, DIRECT) else AUTO


def available() -> bool:
    """Can this host run a transient user unit at all?"""
    return bool(shutil.which(systemd_run_bin()) and os.environ.get("XDG_RUNTIME_DIR"))


def in_service_cgroup() -> bool:
    """Is THIS process inside a systemd `.service` cgroup — i.e. is it killable by a `systemctl restart`?"""
    try:
        lines = [ln for ln in Path("/proc/self/cgroup").read_text().splitlines() if ln]
    except OSError:
        return False
    if not lines:
        return False
    leaf = lines[-1].rsplit(":", 1)[-1].rstrip("/").rsplit("/", 1)[-1]
    return leaf.endswith(".service")


def use_transient_units() -> bool:
    """Should the next turn be spawned into its own unit? (#90)"""
    choice = transport()
    if choice == DIRECT:
        return False
    if choice == SYSTEMD:
        return True
    return available() and in_service_cgroup()


def unit_name(wo_id: str, seq: int) -> str:
    """The transient unit for one turn: unique per (work order, sequence)"""
    return f"jarvis-turn-{_UNIT_SAFE_RE.sub('-', wo_id)}-{seq}.service"


def setenv_args(env: dict[str, str]) -> list[str]:
    """`--setenv=` for every variable the turn needs (#90)"""
    args = []
    for key, value in sorted(env.items()):
        if key in _SYSTEMD_OWNED or not _ENV_NAME_RE.match(key):
            continue
        args.append(f"--setenv={key}={value}")
    return args


def run_prefix(unit: str, cwd: Path, outfile: Path, errfile: Path,
               env: dict[str, str], description: str,
               stdin: Path | None = None) -> list[str]:
    """The `systemd-run` argv the turn's own command line is appended to (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)"""
    return [
        systemd_run_bin(), "--user", "--collect", "--quiet",
        f"--unit={unit}",
        f"--description={description}",
        f"--working-directory={cwd}",
        f"--property=StandardOutput=file:{outfile}",
        f"--property=StandardError=file:{errfile}",
        f"--property=StandardInput=file:{stdin}" if stdin
        else "--property=StandardInput=null",
        *setenv_args(env),
        "--",
    ]


def _show(unit: str, prop: str) -> str:
    argv = [systemctl_bin(), "--user", "show", unit, f"--property={prop}", "--value"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not query %s of %s: %s", prop, unit, e)
        return ""
    return (proc.stdout or "").strip()


def main_pid(unit: str, attempts: int = 20, delay: float = 0.05) -> int | None:
    """The unit's main process — the `claude` itself, which is what gets recorded"""
    for attempt in range(attempts):
        raw = _show(unit, "MainPID")
        try:
            pid = int(raw)
        except ValueError:
            pid = 0
        if pid > 0:
            return pid
        if attempt + 1 < attempts:
            time.sleep(delay)
    return None


def unit_active(unit: str) -> bool:
    """Is the unit still running?"""
    state = _show(unit, "ActiveState")
    return state in ("active", "activating", "reloading", "deactivating")


def uptime_seconds(unit: str) -> float | None:
    """How long since the unit last left `inactive` — `None` when systemd will not say"""
    raw = _show(unit, "InactiveExitTimestampMonotonic")
    try:
        started = int(raw) / 1_000_000          # systemd reports CLOCK_MONOTONIC µs
    except ValueError:
        return None
    if started <= 0:
        return None
    return max(0.0, time.monotonic() - started)


def stop_unit(unit: str) -> bool:
    """Stop a turn's unit, taking its whole cgroup — MCP servers included — with it"""
    if not unit_active(unit):
        return False
    argv = [systemctl_bin(), "--user", "stop", "--no-block", unit]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not stop %s: %s", unit, e)
        return False
    if proc.returncode != 0:
        log.warning("stopping %s failed (%s): %s", unit, proc.returncode,
                    (proc.stderr or "").strip())
        return False
    return True
