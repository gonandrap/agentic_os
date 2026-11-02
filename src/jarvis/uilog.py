"""The dashboard's own log — and, more to the point, the OS's ability to read it back"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import db
from .paths import logs_dir

#: Rotate at 512 KiB, keeping one previous file
MAX_BYTES = 512 * 1024

#: How far back `recent_errors` reads
TAIL_BYTES = 64 * 1024

#: How recent a dashboard error has to be to still be worth the user's attention
ERROR_WINDOW_SECONDS = 24 * 3600

_STAMP = "%Y-%m-%d %H:%M:%S"
_HEADER = re.compile(
    r"^(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[ERROR\] "
    r"(?P<method>\S+) (?P<path>\S+) — (?P<exc>[A-Za-z_][\w.]*): (?P<msg>.*)$"
)


@dataclass(frozen=True)
class UiError:
    """One unhandled dashboard failure, parsed back out of `ui.log`"""

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


#: `os_state` keys the wedge self-heal keeps (spec §4)
UI_HEALTHZ_OK_AT = "ui_healthz_ok_at"
UI_WEDGE_RESTARTS = "ui_wedge_restarts"
UI_WEDGE_CAPPED_AT = "ui_wedge_capped_at"


def stack_dump_path() -> Path:
    """Every thread's Python stack, written by the dashboard about ITSELF (docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md)"""
    return logs_dir() / "ui-stacks.log"


def wedge_stamp_path() -> Path:
    """The small JSON the daemon and `jarvis doctor` read back: when the dashboard first tripped"""
    return logs_dir() / "ui-wedge.json"


# -- writing

def _append(path: Path, text: str) -> None:
    """Append, rotating first if the file has grown past `MAX_BYTES`"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a") as f:
            f.write(text)
    except Exception:  # noqa: BLE001 — logging must never mask the original failure
        pass


def record_error(method: str, path: str, exc: BaseException) -> None:
    """Write one `[ERROR]` entry, header line plus indented traceback"""
    stamp = time.strftime(_STAMP, time.localtime(db.now()))
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    body = "".join(f"    {line}\n" for line in tb.rstrip("\n").splitlines())
    # Newlines in the exception message would forge a second entry; flatten them
    message = " ".join(str(exc).splitlines()) or "(no message)"
    _append(ui_log_path(),
            f"{stamp} [ERROR] {method} {path} — {type(exc).__name__}: {message}\n{body}")


def record_access(method: str, path: str, status: int, duration_ms: float) -> None:
    """Write one access line"""
    stamp = time.strftime(_STAMP, time.localtime(db.now()))
    _append(access_log_path(),
            f"{stamp} [{status}] {method} {path} {duration_ms:.0f}ms\n")


def dump_stacks(note: str = "") -> Path | None:
    """Append every live thread's Python stack to `stack_dump_path()`"""
    path = stack_dump_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        stamp = time.strftime(_STAMP, time.localtime(db.now()))
        with path.open("a") as f:
            f.write(f"\n{stamp} [WEDGE] {note}\n")
            f.flush()
            try:
                import faulthandler

                faulthandler.dump_traceback(file=f, all_threads=True)
            except Exception:  # noqa: BLE001 — fall back to the pure-Python dump
                import sys

                for ident, frame in sys._current_frames().items():
                    f.write(f"\nThread {ident}:\n")
                    f.write("".join(traceback.format_stack(frame)))
        return path
    except Exception:  # noqa: BLE001 — see above
        return None


def register_stack_signal() -> bool:
    """`kill -USR1 <pid>` dumps every thread's stack to stderr (the journal), no ptrace needed (#848)"""
    import faulthandler
    import signal
    import sys

    if not hasattr(signal, "SIGUSR1"):
        return False
    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    return True


def record_wedge(*, limiter: dict[str, int], uptime_seconds: float,
                 version: str = "", reason: str = "threadpool wedged",
                 ) -> dict[str, object]:
    """Dump the stacks and (re)write the wedge stamp"""
    stamp: dict[str, object] = {
        "since": time.time(), "at": time.time(), "limiter": dict(limiter),
        "uptime_seconds": round(uptime_seconds, 1), "version": version,
        "dump": str(stack_dump_path()), "reason": reason,
    }
    previous = read_wedge()
    if previous and previous.get("since"):
        stamp["since"] = previous["since"]
    note = (f"{reason} — limiter borrowed={limiter.get('borrowed')} "
            f"available={limiter.get('available')} total={limiter.get('total')} "
            f"waiting={limiter.get('waiting')} uptime={int(uptime_seconds)}s "
            f"version={version or 'unknown'}")
    dump = dump_stacks(note)
    if dump is None:
        stamp["dump"] = ""
    try:
        path = wedge_stamp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(stamp, indent=2) + "\n")
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — a failed stamp must not break the 503
        pass
    return stamp


def read_wedge() -> dict[str, object] | None:
    """The wedge stamp, or None when the dashboard has never tripped"""
    try:
        return json.loads(wedge_stamp_path().read_text())
    except (OSError, ValueError):
        return None


# -- reading

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


def read_errors(cursor: str = "",
                within_seconds: float = ERROR_WINDOW_SECONDS,
                ) -> tuple[list[UiError], str]:
    """Errors written since `cursor` and newer than `within_seconds`, plus the cursor to resume from"""
    p = ui_log_path()
    if not p.exists():
        return [], ""
    st = p.stat()
    with p.open("rb") as f:
        head = f.read(_HEAD_BYTES)
        offset = _resume_at(cursor, st, head)
        f.seek(offset)
        raw = f.read()
    cutoff = db.now() - within_seconds
    errors = [e for e in _parse(raw.decode("utf-8", errors="replace"))
              if e.ts >= cutoff]
    # The cursor advances past the entries the filter dropped: they have been read and dismissed
    return errors, _cursor(offset + len(raw), st, head)


_HEAD_BYTES = 512


def _fingerprint(st: os.stat_result, head: bytes) -> str:
    """Identity of the file, stable under appends and not under replacement"""
    first_line = head.split(b"\n", 1)[0]
    return f"{st.st_ino}-{hashlib.sha1(first_line).hexdigest()[:12]}"


def _cursor(offset: int, st: os.stat_result, head: bytes) -> str:
    return f"{offset}:{_fingerprint(st, head)}"


def _resume_at(cursor: str, st: os.stat_result, head: bytes) -> int:
    """Byte offset the cursor points at, or 0 if it no longer describes this file"""
    raw_offset, _, fingerprint = cursor.partition(":")
    try:
        offset = int(raw_offset)
    except ValueError:
        return 0
    if fingerprint != _fingerprint(st, head) or offset > st.st_size:
        return 0
    return offset


_ACCESS = re.compile(
    r"^(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(?P<status>\d{3})\] "
    r"(?P<method>\S+) (?P<path>\S+) \d+ms$"
)


def _tail(p: Path) -> str:
    if not p.exists():
        return ""
    with p.open("rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL_BYTES))
        return f.read().decode("utf-8", errors="replace")


def _last_success() -> dict[tuple[str, str], float]:
    """Newest non-error response per (method, path) in the access log's tail"""
    last: dict[tuple[str, str], float] = {}
    for line in _tail(access_log_path()).splitlines():
        m = _ACCESS.match(line)
        if not m or int(m["status"]) >= 400:
            continue
        try:
            ts = time.mktime(time.strptime(m["when"], _STAMP))
        except ValueError:
            continue
        key = (m["method"], m["path"].split("?", 1)[0])
        last[key] = max(ts, last.get(key, 0.0))
    return last


def recent_errors(within_seconds: float = ERROR_WINDOW_SECONDS,
                  limit: int = 5) -> tuple[list[UiError], int]:
    """The `limit` newest errors inside the window, and how many the window holds"""
    cutoff = db.now() - within_seconds
    found = [e for e in _parse(_tail(ui_log_path())) if e.ts >= cutoff]
    # An error whose route has since served a success is fixed, not standing
    healed = _last_success() if found else {}
    found = [e for e in found if healed.get((e.method, e.path), 0.0) <= e.ts]
    return found[-limit:][::-1], len(found)
