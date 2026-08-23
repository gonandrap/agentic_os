"""Running a worker turn in its own transient systemd unit, outside the daemon's cgroup.

Why this exists: a turn is a detached `claude -p` process, but `start_new_session=True`
takes it out of the process GROUP, not out of the cgroup. `systemd --user` defaults to
`KillMode=control-group`, so every `systemctl restart jarvis` SIGTERMs every turn in
flight — a deploy, a crash-restart and `jarvis stop` alike (issue #133). A turn killed
that way writes no result JSON, matches neither self-healing retry class, and settles
`failed` with an attention flag; its dependents strand on a `DEPENDENCY_DEAD_STATUS` and
its parent feature order fails with it. `release.py`'s staged handshake covers exactly
one work order — the shipping one — and leaves every other running order exposed.

`systemd-run --user` puts the turn in its own cgroup under `app.slice`, so restarting
`jarvis.service` does not reach it. The turn keeps running, writes its result file, and
the NEW daemon reaps it on its first tick: `claude_cli.process_alive` case 3 ("no longer
our child, so `/proc` is consulted as a pid-reuse guard") was already written for exactly
this. `KillMode=mixed` on jarvis.service would NOT have done instead — systemd still
SIGKILLs the rest of the cgroup once the main process is gone.

What this module deliberately does NOT do is start the `claude` process. That stays in
`claude_cli.spawn_turn`, the single launch path `tests/test_prompt_cache_ttl.py` guards
over the AST so no new one can appear without forcing the 5-minute prompt cache. This
module supplies the argv prefix that wraps the command, and answers what the daemon asks
afterwards: the unit's main pid, whether it is still up, and how to stop it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger("jarvisd.systemd")

#: Which transport a worker turn is spawned onto. `auto` (the default) decides per
#: spawn; `systemd` and `direct` are the escape hatches — the tests use `direct`, and a
#: `systemd` that fails at spawn time still falls back rather than stalling dispatch.
TRANSPORT_ENV = "JARVIS_TURN_TRANSPORT"
AUTO, SYSTEMD, DIRECT = "auto", "systemd", "direct"

#: Binary overrides, mirroring `claude_cli.CLAUDE_BIN_ENV` — this is how the suite points
#: the systemd path at a fake without a real systemd anywhere near it.
SYSTEMD_RUN_BIN_ENV = "JARVIS_SYSTEMD_RUN_BIN"
SYSTEMCTL_BIN_ENV = "JARVIS_SYSTEMCTL_BIN"

#: Variables systemd sets for ITS OWN bookkeeping of the unit we are running inside.
#: Forwarding them onto a new unit would hand the turn its parent's identity and, worse,
#: its parent's readiness plumbing — `NOTIFY_SOCKET` and the `LISTEN_*` trio are protocol,
#: not configuration. systemd populates the correct values for the new unit itself.
_SYSTEMD_OWNED = frozenset({
    "INVOCATION_ID", "JOURNAL_STREAM", "NOTIFY_SOCKET", "MANAGERPID", "SYSTEMD_EXEC_PID",
    "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "WATCHDOG_PID", "WATCHDOG_USEC",
    "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE",
})

#: A shell-ish name is the only shape `Environment=` can round-trip. Anything else would
#: be rejected by systemd at unit-load time — i.e. the whole turn would fail to start
#: over one junk variable — so it is dropped here instead.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: systemd unit names accept a narrow alphabet; work-order ids and sequence numbers are
#: already within it, but sanitising means a future id scheme cannot break spawning.
#: Stricter than systemd's own rules on purpose — `.` and `:` are legal in a unit name
#: and would still make `jarvis-turn-wo-..-1.service` a confusing thing to read in
#: `systemctl` output, and the `.service` suffix this builds is appended separately.
_UNIT_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def systemd_run_bin() -> str:
    return os.environ.get(SYSTEMD_RUN_BIN_ENV, "systemd-run")


def systemctl_bin() -> str:
    return os.environ.get(SYSTEMCTL_BIN_ENV, "systemctl")


def transport() -> str:
    """The configured transport. An unrecognised value reads as `auto`, never as a
    failure: this variable must not be able to stop the fleet dispatching."""
    value = os.environ.get(TRANSPORT_ENV, "").strip().lower()
    return value if value in (AUTO, SYSTEMD, DIRECT) else AUTO


def available() -> bool:
    """Can this host run a transient user unit at all?

    `XDG_RUNTIME_DIR` is the half people forget: without it `systemd-run --user` has no
    bus to reach the user manager on, and it is absent from cron jobs and from most
    container shells even where the binary is installed.
    """
    return bool(shutil.which(systemd_run_bin()) and os.environ.get("XDG_RUNTIME_DIR"))


def in_service_cgroup() -> bool:
    """Is THIS process inside a systemd `.service` cgroup — i.e. is it killable by a
    `systemctl restart`?

    That, not "is this production", is the condition the fix is actually for. Read off
    `/proc/self/cgroup`, whose last line under cgroup v2 is the unified hierarchy path;
    a daemon started from a shell sits in the session's `.scope` and answers False, which
    is why `jarvis start --foreground` keeps the plain-`Popen` path with no flag to set.
    """
    try:
        lines = [ln for ln in Path("/proc/self/cgroup").read_text().splitlines() if ln]
    except OSError:
        return False
    if not lines:
        return False
    leaf = lines[-1].rsplit(":", 1)[-1].rstrip("/").rsplit("/", 1)[-1]
    return leaf.endswith(".service")


def use_transient_units() -> bool:
    """Should the next turn be spawned into its own unit?

    Auto-detected rather than opted into. An explicit `Environment=` line in the service
    template would be deterministic, but it only takes effect once someone re-runs
    `scripts/install_prod_service.sh` after the deploy — and a fix that silently does
    nothing until an install step nobody remembers is precisely what cost a release when
    `gh` fell off the daemon's PATH (kn-dafd3d17, issue #90). Neo confirmed the trade on
    question 156.
    """
    choice = transport()
    if choice == DIRECT:
        return False
    if choice == SYSTEMD:
        return True
    return available() and in_service_cgroup()


def unit_name(wo_id: str, seq: int) -> str:
    """The transient unit for one turn: unique per (work order, sequence).

    Unique because `systemd-run --unit=` refuses a name already taken, and a retried or
    resumed turn gets a fresh `seq`. The name is still only a name — `wo_turns.unit`
    records what was actually used, so `cancel` stops the real unit rather than one
    re-derived from a convention that may have drifted since.
    """
    return f"jarvis-turn-{_UNIT_SAFE_RE.sub('-', wo_id)}-{seq}.service"


def setenv_args(env: dict[str, str]) -> list[str]:
    """`--setenv=` for every variable the turn needs.

    A transient unit inherits the systemd USER MANAGER's environment, not the caller's:
    verified live, and it means none of `JARVIS_HOME`, `JARVIS_ENV`, `PRODUCTION_CODE` or
    the `Environment=PATH=` the fleet depends on (#90) reaches a turn unless it is passed
    here explicitly. So the whole environment is forwarded, minus what systemd owns.
    """
    args = []
    for key, value in sorted(env.items()):
        if key in _SYSTEMD_OWNED or not _ENV_NAME_RE.match(key):
            continue
        args.append(f"--setenv={key}={value}")
    return args


def run_prefix(unit: str, cwd: Path, outfile: Path, errfile: Path,
               env: dict[str, str], description: str) -> list[str]:
    """The `systemd-run` argv the turn's own command line is appended to.

    `--collect` so a finished unit unloads itself — which means a reap routinely finds
    the unit gone (`LoadState=not-found`), and every reader here treats that as "not
    running" rather than as an error. stdout and stderr go straight to the turn's files
    so nothing has to relay them across a process that is about to exit; `file:` truncates
    on open, matching the `open("w")` the direct path uses. Standard input is `null` for
    the same reason it is `DEVNULL` there: `claude -p` otherwise waits three seconds for
    input that is never coming.
    """
    return [
        systemd_run_bin(), "--user", "--collect", "--quiet",
        f"--unit={unit}",
        f"--description={description}",
        f"--working-directory={cwd}",
        f"--property=StandardOutput=file:{outfile}",
        f"--property=StandardError=file:{errfile}",
        "--property=StandardInput=null",
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
    """The unit's main process — the `claude` itself, which is what gets recorded.

    `systemd-run` returns once the start job is enqueued and the pid is normally there
    immediately (verified live), but a busy manager can lag, and a turn recorded with no
    pid would be reaped as dead by the very next tick. So this waits briefly rather than
    reading once. `MainPID=0` means the unit has no live process: still starting, or
    already finished and collected.
    """
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
    """Is the unit still running? The liveness answer of LAST resort.

    `worker_session.poll` decides on the recorded pid first, exactly as it always has.
    This is only consulted for the narrow case that pid could not be captured, so a turn
    that is demonstrably running is never reaped for want of a number. A collected unit
    reports `inactive`, which is the same answer as a finished one — correct either way.
    """
    state = _show(unit, "ActiveState")
    return state in ("active", "activating", "reloading", "deactivating")


def stop_unit(unit: str) -> bool:
    """Stop a turn's unit, taking its whole cgroup — MCP servers included — with it.

    Returns whether there was anything to stop, so `cancel` can report it the way it
    reports a signalled process group. `--no-block` because cancelling must never hang a
    reconcile tick behind a unit's stop timeout: the job is queued and systemd sees it
    through. Best effort throughout, like the `kill_process_group` it runs beside.
    """
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
