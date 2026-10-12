"""Which init system owns production's two services, and how to ask launchd about them.

The shell half of this question was already answered — `scripts/install_prod_service.sh`
and `scripts/shipit.sh` resolve `JARVIS_SERVICE_MANAGER`, else `uname -s`. The Python
half spelled `systemctl` as a literal in three places, so on a Mac the dashboard
self-heal returned `down` for every wedge, a staged release could never apply, and the
boot verification parked every one of them `failed_verification`. This module gives
Python the same answer, from the same variable, with the same precedence, in ONE place.

It must NOT import `release` at module scope (`release` imports it), so the two unit
names appear here as literals; `tests/test_service_manager.py` pins them against
`release.DAEMON_UNIT`/`release.UI_UNIT` so the map cannot drift.

Deliberately NOT the worker-turn transport: `systemd_units` is the cgroup escape, launchd
has no transient-unit equivalent, and its `unit_active` answers about per-turn unit names
that have no launchd label at all. Spec:
docs/superpowers/specs/2026-10-12-service-queries-on-a-mac.md
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import time

from . import systemd_units

log = logging.getLogger("jarvisd.service")

#: Which init system runs `jarvis.service` and `jarvis-ui.service`. Same name and same
#: precedence as `scripts/install_prod_service.sh:48-55`, so the installer and the daemon
#: can never disagree about which service to restart.
MANAGER_ENV = "JARVIS_SERVICE_MANAGER"
SYSTEMD, LAUNCHD = "systemd", "launchd"

#: Binary override, mirroring `systemd_units.systemctl_bin()` — this is the seam the suite
#: points at a fake, and the only reason a test can exercise the launchd branch without
#: touching the user's real services.
LAUNCHCTL_BIN_ENV = "JARVIS_LAUNCHCTL_BIN"

#: systemd unit -> launchd label. The UNIT NAME stays canonical everywhere in the code
#: (`release.UI_UNIT`, `release.DAEMON_UNIT`); the label is derived at the edge, here.
LABELS = {"jarvis.service": "com.jarvis.daemon", "jarvis-ui.service": "com.jarvis.ui"}

#: `[[DD-]HH:]MM:SS`, the only format `ps -o etime=` emits.
_ETIME_RE = re.compile(r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<mins>\d+):"
                       r"(?P<secs>\d+)$")

#: How long a staged restart may take to come back before the detached child gives up on
#: letting this daemon exit cleanly first — `release.py:240`'s reason, kept.
_DETACH_SLEEP = 3


def manager() -> str:
    """Which init system owns the two production services.

    An unrecognised value reads as the `uname` answer, never as a failure: this variable
    must not be able to stop the daemon booting, exactly as `systemd_units.transport()`
    reads a junk value as `auto`.
    """
    value = os.environ.get(MANAGER_ENV, "").strip().lower()
    if value in (SYSTEMD, LAUNCHD):
        return value
    return LAUNCHD if platform.system() == "Darwin" else SYSTEMD


def launchctl_bin() -> str:
    return os.environ.get(LAUNCHCTL_BIN_ENV, "launchctl")


def label(unit: str) -> str:
    try:
        return LABELS[unit]
    except KeyError:
        raise ValueError(f"no launchd label for unit {unit!r}") from None


def target(unit: str) -> str:
    """The launchd domain target, the same one shell builds as `DOMAIN="gui/$UID"`."""
    return f"gui/{os.getuid()}/{label(unit)}"


def _parse_print(text: str) -> dict[str, str]:
    """Tab-indented `key = value` lines into a dict. Pure, so it is testable against the
    output captured from the live production Mac (spec "The problem")."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(" = ")
        if not sep:
            continue
        key = key.strip()
        if key and "{" not in value:
            out.setdefault(key, value.strip())
    return out


def _parse_etime(text: str) -> float | None:
    """`[[DD-]HH:]MM:SS` -> seconds. None on anything else."""
    m = _ETIME_RE.match(text.strip())
    if not m:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    return days * 86400.0 + hours * 3600.0 + int(m.group("mins")) * 60.0 + \
        int(m.group("secs"))


def _print(unit: str) -> dict[str, str] | None:
    """`launchctl print <target>`, or None when the job is not loaded.

    A non-zero exit is the NORMAL answer for a stopped service, so it logs at DEBUG:
    logging it at WARNING would re-create the `could not query ActiveState` line this
    module exists to remove, with a different spelling.
    """
    argv = [launchctl_bin(), "print", target(unit)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("could not run %s: %s", argv[0], e)
        return None
    if proc.returncode != 0:
        log.debug("launchctl print %s: exit %s: %s", target(unit), proc.returncode,
                  (proc.stderr or "").strip())
        return None
    return _parse_print(proc.stdout or "")


def unit_active(unit: str) -> bool:
    """Is the unit loaded and owned by its manager?

    The systemd branch delegates byte-identically to `systemd_units.unit_active`: that is
    the function the Linux production host has been answering with, and a second
    definition of "active" here would be one more thing to keep in step.

    `waiting` and `spawn scheduled` count the way the systemd branch counts `activating`,
    so a `KeepAlive` job between crashes reads as active and `check_ui_health` kickstarts
    it rather than calling it `down`. The case `down` exists for is `launchctl bootout`,
    which makes the label unfindable (spec §1.1).
    """
    if manager() != LAUNCHD:
        return systemd_units.unit_active(unit)
    info = _print(unit)
    if info is None:
        return False
    return info.get("state", "") in ("running", "waiting", "spawn scheduled")


def start_time(unit: str) -> float | None:
    """When the unit's main process started, as a unix epoch — or None.

    `now - floor(etime)`, so the computed start is always LATER than the true start and
    never earlier; the one-second truncation can only make a genuine restart pass
    `verify_on_boot`, never fail it (spec §3). NOT `ps -o lstart=`: locale and timezone
    prose, the same trap `release.unit_start_time` documents for
    `ExecMainStartTimestamp`.
    """
    info = _print(unit)
    pid = (info or {}).get("pid", "")
    if not pid.isdigit():
        return None
    try:
        proc = subprocess.run(["ps", "-p", pid, "-o", "etime="],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("could not read etime of pid %s: %s", pid, e)
        return None
    if proc.returncode != 0:
        return None
    elapsed = _parse_etime(proc.stdout or "")
    return None if elapsed is None else time.time() - elapsed


def restart(unit: str) -> None:
    """`kickstart -k` restarts in place, and is what `install_prod_service.sh:149` and
    `shipit.sh:529` already use — so the daemon's restart and the human's are one
    operation."""
    argv = [launchctl_bin(), "kickstart", "-k", target(unit)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        log.warning("%s failed (%s): %s", argv[0], proc.returncode,
                    (proc.stderr or "").strip())


def restart_detached(unit: str, tag: str) -> None:
    """Restart `unit` from a process launchd cannot take down with it.

    `Popen`, not `run`: the point is that the restart outlives this process.
    `start_new_session=True` is load-bearing — launchd takes the job's whole process group
    down when it kickstarts it, so a child left in our group dies before its sleep ends
    (the same reason `install_prod_service.sh:152` and `shipit.sh:580` use `nohup … &`).
    `tag` has no unit name to slug into, so it goes in the log line (spec §1.3).
    """
    cmd = f"sleep {_DETACH_SLEEP}; {launchctl_bin()} kickstart -k {target(unit)}"
    log.info("release %s: queued detached restart of %s", tag, label(unit))
    subprocess.Popen(["/bin/sh", "-c", cmd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def restart_hint(unit: str) -> str:
    """The restart command to print at a human — on the host they actually have."""
    if manager() != LAUNCHD:
        return f"systemctl --user restart {unit}"
    return f"launchctl kickstart -k {target(unit)}"
