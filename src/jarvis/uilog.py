"""The dashboard's own log — and, more to the point, the OS's ability to read it back.

`$JARVIS_HOME/logs/ui.log` gets one entry per unhandled dashboard failure;
`ui-access.log` gets one line per request. Both live next to `jarvisd.log` and the
databases, which is the whole reason this module exists: before it, a UI traceback
existed only in the systemd journal, so a 500 on the OS's own web UI was invisible to
`jarvis status`, to `jarvis doctor`, to the inbox, and to every agent that can read the
state directory but not `journalctl`. An error in the dashboard is now as visible to the
OS as an error in the daemon.

Writing is only half of that. The daemon raises an inbox item for errors it has not seen
yet, `os_status` counts recent ones and `jarvis doctor` reports them — all of which need
to *parse* the log, so the format is part of the contract, not an implementation detail:

    2026-07-27 14:03:11 [ERROR] GET /wo/proj_a/wo-4fdb20ba — KeyError: 'proj_a'
        Traceback (most recent call last):
        ...

The header line is the only line that starts at column 0; every traceback line is
indented, so `_HEADER` can never match inside a traceback body however creative the
exception message is. Timestamps are local time, matching `notifications.log` and
`jarvisd.log` — these files are read by a human first.

Both files rotate at `MAX_BYTES` into a single `.1` sibling, so a dashboard stuck in a
crash loop cannot fill the state directory.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from .paths import logs_dir

#: Rotate at 512 KiB, keeping one previous file. A crash loop writes tracebacks fast.
MAX_BYTES = 512 * 1024

#: How far back `recent_errors` reads. Bounded so `jarvis status` (and `/api/status`,
#: which the dashboard polls every 15s) never pays for the whole file. An entry older
#: than this many bytes has almost certainly aged out of the reporting window too.
TAIL_BYTES = 64 * 1024

#: How recent a dashboard error has to be to still be worth the user's attention.
#: Reporting is window-based rather than acknowledged-based on purpose: there is no
#: "I have seen the UI error" verb, and a signal that needs a manual clear is a signal
#: that gets ignored. The inbox item raised by the daemon is the one-shot alert; this
#: window is the standing "the dashboard is broken right now" indicator, and it expires
#: on its own once the dashboard has been quiet for a day.
ERROR_WINDOW_SECONDS = 24 * 3600

_STAMP = "%Y-%m-%d %H:%M:%S"
_HEADER = re.compile(
    r"^(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[ERROR\] "
    r"(?P<method>\S+) (?P<path>\S+) — (?P<exc>[A-Za-z_][\w.]*): (?P<msg>.*)$"
)


@dataclass(frozen=True)
class UiError:
    """One unhandled dashboard failure, parsed back out of `ui.log`."""

    ts: float
    when: str
    method: str
    path: str
    exc_type: str
    message: str
    traceback: str = ""

    @property
    def summary(self) -> str:
        return f"{self.method} {self.path} — {self.exc_type}: {self.message}"

    def as_dict(self) -> dict[str, object]:
        return {"ts": self.ts, "when": self.when, "method": self.method,
                "path": self.path, "exc_type": self.exc_type,
                "message": self.message}


def ui_log_path() -> Path:
    return logs_dir() / "ui.log"


def access_log_path() -> Path:
    return logs_dir() / "ui-access.log"


# -- writing ------------------------------------------------------------------------

def _append(path: Path, text: str) -> None:
    """Append, rotating first if the file has grown past `MAX_BYTES`.

    Every caller is on a request path, so this must never raise: a logger that can take
    the dashboard down is worse than no logger. Rotation loses the byte watermark the
    daemon keeps, which `read_errors` handles by restarting from zero.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a") as f:
            f.write(text)
    except Exception:  # noqa: BLE001 — logging must never mask the original failure
        pass


def record_error(method: str, path: str, exc: BaseException) -> None:
    """Write one `[ERROR]` entry, header line plus indented traceback."""
    stamp = time.strftime(_STAMP, time.localtime())
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    body = "".join(f"    {line}\n" for line in tb.rstrip("\n").splitlines())
    # Newlines in the exception message would forge a second entry; flatten them.
    message = " ".join(str(exc).splitlines()) or "(no message)"
    _append(ui_log_path(),
            f"{stamp} [ERROR] {method} {path} — {type(exc).__name__}: {message}\n{body}")


def record_access(method: str, path: str, status: int, duration_ms: float) -> None:
    """Write one access line.

    This is what was missing when "I clicked the link and got an internal server error"
    had to be placed in time: without it there is no record of which deep links were
    followed, in what order, or which of them failed.
    """
    stamp = time.strftime(_STAMP, time.localtime())
    _append(access_log_path(),
            f"{stamp} [{status}] {method} {path} {duration_ms:.0f}ms\n")


# -- reading ------------------------------------------------------------------------

def _parse(text: str) -> list[UiError]:
    errors: list[UiError] = []
    body: list[str] = []
    for line in text.splitlines():
        m = _HEADER.match(line)
        if m:
            body = []
            try:
                ts = time.mktime(time.strptime(m["when"], _STAMP))
            except ValueError:  # pragma: no cover - regex already pins the shape
                continue
            errors.append(UiError(ts=ts, when=m["when"], method=m["method"],
                                  path=m["path"], exc_type=m["exc"],
                                  message=m["msg"]))
        elif errors and line.startswith("    "):
            body.append(line[4:])
            errors[-1] = UiError(**{**errors[-1].__dict__,
                                    "traceback": "\n".join(body)})
    return errors


def read_errors(cursor: str = "") -> tuple[list[UiError], str]:
    """Errors written since `cursor`, plus the cursor to resume from.

    The daemon stores the returned cursor so each error is announced exactly once —
    without it, one standing failure would re-notify on every five-second tick.

    The cursor is opaque: a byte offset *plus* the file's identity (inode and a hash of
    its first bytes). A bare offset is not enough, because rotation can leave a new file
    of much the same size and the daemon would then silently seek past real errors. Any
    mismatch restarts from the top, so the failure mode is re-announcing a handful of
    entries rather than going quiet — the right direction for an alerting path.
    """
    p = ui_log_path()
    if not p.exists():
        return [], ""
    st = p.stat()
    with p.open("rb") as f:
        head = f.read(_HEAD_BYTES)
        offset = _resume_at(cursor, st, head)
        f.seek(offset)
        raw = f.read()
    return (_parse(raw.decode("utf-8", errors="replace")),
            _cursor(offset + len(raw), st, head))


_HEAD_BYTES = 512


def _fingerprint(st: os.stat_result, head: bytes) -> str:
    """Identity of the file, stable under appends and not under replacement.

    The *first line* rather than the first N bytes: appending changes how much of the
    file the head read returns, so hashing raw bytes would make every append look like
    a new file and re-announce the whole log. The first line only changes when the
    file is truncated or rotated away, which is exactly the event this has to catch.
    """
    first_line = head.split(b"\n", 1)[0]
    return f"{st.st_ino}-{hashlib.sha1(first_line).hexdigest()[:12]}"


def _cursor(offset: int, st: os.stat_result, head: bytes) -> str:
    return f"{offset}:{_fingerprint(st, head)}"


def _resume_at(cursor: str, st: os.stat_result, head: bytes) -> int:
    """Byte offset the cursor points at, or 0 if it no longer describes this file."""
    raw_offset, _, fingerprint = cursor.partition(":")
    try:
        offset = int(raw_offset)
    except ValueError:
        return 0
    if fingerprint != _fingerprint(st, head) or offset > st.st_size:
        return 0
    return offset


def recent_errors(within_seconds: float = ERROR_WINDOW_SECONDS,
                  limit: int = 5) -> tuple[list[UiError], int]:
    """The `limit` newest errors inside the window, and how many the window holds.

    Reads only the last `TAIL_BYTES`, so the first entry in that slice may be truncated
    mid-traceback; a partial header simply fails to match and is skipped.
    """
    p = ui_log_path()
    if not p.exists():
        return [], 0
    with p.open("rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL_BYTES))
        raw = f.read()
    cutoff = time.time() - within_seconds
    found = [e for e in _parse(raw.decode("utf-8", errors="replace"))
             if e.ts >= cutoff]
    return found[-limit:][::-1], len(found)
