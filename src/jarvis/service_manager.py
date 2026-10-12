"""Which init system owns production's two services, and launchd's answers for them (docs/superpowers/specs/2026-10-12-service-queries-on-a-mac.md)"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import time

from . import systemd_units

log = logging.getLogger("jarvisd.service")

#: Same name and precedence as `scripts/install_prod_service.sh`, so installer and daemon agree
MANAGER_ENV = "JARVIS_SERVICE_MANAGER"
SYSTEMD, LAUNCHD = "systemd", "launchd"

#: Binary override, mirroring `systemd_units.systemctl_bin()` — the seam tests point at a fake
LAUNCHCTL_BIN_ENV = "JARVIS_LAUNCHCTL_BIN"

#: systemd unit -> launchd label; literals because `release` imports this module
LABELS = {"jarvis.service": "com.jarvis.daemon", "jarvis-ui.service": "com.jarvis.ui"}

#: `[[DD-]HH:]MM:SS`, the only format `ps -o etime=` emits
_ETIME_RE = re.compile(r"^(?:(?P<days>\d+)-)?(?:(?P<hours>\d+):)?(?P<mins>\d+):"
                       r"(?P<secs>\d+)$")

#: Seconds the detached restart waits so this daemon can exit cleanly first
_DETACH_SLEEP = 3

#: `launchctl print` states of a loaded job that is between processes, not stopped
_LAUNCHD_PENDING = ("waiting", "spawn scheduled")


def manager() -> str:
    """`JARVIS_SERVICE_MANAGER`, else launchd on Darwin and systemd elsewhere — junk reads as unset"""
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
    """The launchd domain target, the same one the scripts build as `gui/$UID/<label>`"""
    return f"gui/{os.getuid()}/{label(unit)}"


def _parse_print(text: str) -> dict[str, str]:
    """Top-level `key = value` lines of `launchctl print` into a dict"""
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
    """`[[DD-]HH:]MM:SS` -> seconds, `None` on anything else"""
    m = _ETIME_RE.match(text.strip())
    if not m:
        return None
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    return days * 86400.0 + hours * 3600.0 + int(m.group("mins")) * 60.0 + \
        int(m.group("secs"))


def _print(unit: str) -> dict[str, str] | None:
    """`launchctl print <target>`, or `None` when the job is not loaded — the normal answer for a stopped service"""
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
    """Is the unit loaded and owned by its manager? A booted-out launchd label is not"""
    if manager() != LAUNCHD:
        return systemd_units.unit_active(unit)
    info = _print(unit)
    if info is None:
        return False
    return info.get("state", "") in ("running", *_LAUNCHD_PENDING)


def unit_transitioning(unit: str) -> bool:
    """Is the unit mid-restart — loaded, but between processes? (issue #1194)"""
    if manager() != LAUNCHD:
        return systemd_units.unit_transitioning(unit)
    info = _print(unit)
    return info is not None and info.get("state", "") in _LAUNCHD_PENDING


def uptime_seconds(unit: str) -> float | None:
    """How long the unit's current process has run — `None` when its manager will not say"""
    if manager() != LAUNCHD:
        return systemd_units.uptime_seconds(unit)
    started = start_time(unit)
    return None if started is None else max(0.0, time.time() - started)


def start_time(unit: str) -> float | None:
    """Epoch start of the launchd job's process, `now - etime` so it is never earlier than the truth"""
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
    """`launchctl kickstart -k`, the same restart the install and ship scripts use"""
    argv = [launchctl_bin(), "kickstart", "-k", target(unit)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        log.warning("%s failed (%s): %s", argv[0], proc.returncode,
                    (proc.stderr or "").strip())


def restart_detached(unit: str, tag: str) -> None:
    """Restart `unit` from a new session launchd cannot take down with this job's process group"""
    cmd = f"sleep {_DETACH_SLEEP}; {launchctl_bin()} kickstart -k {target(unit)}"
    log.info("release %s: queued detached restart of %s", tag, label(unit))
    subprocess.Popen(["/bin/sh", "-c", cmd],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def restart_hint(unit: str) -> str:
    """The restart command to print at a human, for the host they actually have"""
    if manager() != LAUNCHD:
        return f"systemctl --user restart {unit}"
    return f"launchctl kickstart -k {target(unit)}"
