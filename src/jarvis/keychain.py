"""A generic-password item from the macOS login Keychain, where Claude Code keeps its sign-in on a Mac"""

from __future__ import annotations

import calendar
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

log = logging.getLogger("jarvisd.keychain")

#: Binary override, mirroring `systemd_units.SYSTEMCTL_BIN_ENV` — the seam tests point at a fake
SECURITY_BIN_ENV = "JARVIS_SECURITY_BIN"

#: `"mdat"<timedate>=0x… "20261027082717Z\000"`, the item's last-modified time in UTC
_MDAT_RE = re.compile(r'"mdat"<timedate>=\S+\s+"(\d{14})Z')


@dataclass
class Item:
    secret: str
    #: Epoch seconds the item last changed, or None when `security` would not say
    modified_at: float | None


def security_bin() -> str:
    return os.environ.get(SECURITY_BIN_ENV, "/usr/bin/security")


def _security(*args: str) -> str | None:
    """stdout of one `security` call, `None` when it is missing, fails or the item is absent"""
    argv = [security_bin(), *args]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("could not run %s: %s", argv[0], e)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _parse_mdat(text: str) -> float | None:
    m = _MDAT_RE.search(text)
    if not m:
        return None
    try:
        return float(calendar.timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S")))
    except ValueError:
        return None


def generic_password(service: str) -> Item | None:
    """The item's secret and modified time, or `None` off a Mac or when there is no such item"""
    secret = _security("find-generic-password", "-s", service, "-w")
    if secret is None:
        return None
    attrs = _security("find-generic-password", "-s", service) or ""
    return Item(secret=secret.rstrip("\n"), modified_at=_parse_mdat(attrs))
