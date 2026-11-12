"""PTY sessions behind the onboarding help panel (docs/superpowers/specs/2026-11-12-onboarding-help-terminal-panel.md §1, §0.2, §0.3)"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import json
import os
import secrets
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from .. import paths

IDLE_SECONDS = 15 * 60
SCROLLBACK = 256 * 1024
READ_CHUNK = 64 * 1024
QUEUE_MAX = 256
KILL_GRACE = 2.0
PR_SET_PDEATHSIG = 1
_LIBC = ctypes.CDLL(None, use_errno=True) if sys.platform.startswith("linux") else None


class HelpPtyError(Exception):
    """The helper's PTY could not start"""


def _child_setup(parent: int) -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    if _LIBC is not None:
        _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        if os.getppid() != parent:  # parent died before prctl took effect
            os._exit(1)


def _cmdline(pid: int) -> list[str] | None:
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        return raw.decode(errors="replace").split("\0")[:-1] or None
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    return [out] if out else None


def _same_process(pid: int, argv: list[str]) -> bool:
    """Pid still runs the recorded argv; a shebang binary shows its interpreter first"""
    live = _cmdline(pid)
    if not live or not argv:
        return False
    if len(live) == 1 and len(argv) > 1:  # `ps` gives one joined string
        return live[0].endswith(" ".join(argv))
    return live[-len(argv):] == argv


def _put(q: asyncio.Queue, item: bytes | None) -> None:
    # A slow subscriber loses its oldest chunk; the reader never waits
    if q.full():
        q.get_nowait()
    q.put_nowait(item)


class HelpSession:
    def __init__(self, owner: HelpSessions, draft: str, proc: subprocess.Popen,
                 master: int, record: Path) -> None:
        self.token = secrets.token_urlsafe(24)
        self.draft, self.pid, self.alive = draft, proc.pid, True
        self._owner, self._proc, self._master, self._record = owner, proc, master, record
        self._scrollback = bytearray()
        self._queues: list[asyncio.Queue] = []
        self._attached: asyncio.Queue | None = None
        self._pending = bytearray()
        self._last_active = time.monotonic()
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master, READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:  # EIO: the slave side is gone
            data = b""
        if not data:
            self._owner._spawn_task(self._owner._end(self))
            return
        self._scrollback += data
        del self._scrollback[:-SCROLLBACK]
        for q in self._queues:
            _put(q, data)

    def write(self, data: bytes) -> None:
        self._last_active = time.monotonic()
        if not self.alive:
            return
        if not self._pending:
            try:
                data = data[os.write(self._master, data):]
            except BlockingIOError:
                pass
            except OSError:
                return
            if not data:
                return
            self._loop.add_writer(self._master, self._on_writable)
        self._pending += data

    def _on_writable(self) -> None:
        try:
            del self._pending[:os.write(self._master, self._pending)]
        except BlockingIOError:
            return
        except OSError:
            self._pending.clear()
        if not self._pending:
            self._loop.remove_writer(self._master)

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = (max(1, min(500, int(n))) for n in (rows, cols))
        with contextlib.suppress(OSError):
            fcntl.ioctl(self._master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def attach(self) -> tuple[bytes, asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        if not self.alive:
            q.put_nowait(None)
        self._queues.append(q)
        self._attached = q
        self._last_active = time.monotonic()
        return bytes(self._scrollback), q

    def detach(self, queue: asyncio.Queue) -> None:
        if queue in self._queues:
            self._queues.remove(queue)
        if queue is self._attached:
            self._attached = None
            self._last_active = time.monotonic()

    def _idle(self, idle_seconds: float) -> bool:
        return (self._attached is None
                and time.monotonic() - self._last_active >= idle_seconds)

    def _feed(self, launch: dict) -> None:
        focus = launch.get("focus") or ""
        text = (f"[context] The user is now on section {launch.get('section', '')}"
                + (f", asking about {focus}" if focus else "") + ".\n\n" + launch["prompt"])
        # Bracketed paste: a multi-line context lands as one message
        self.write(b"\x1b[200~" + text.encode() + b"\x1b[201~\r")


class HelpSessions:
    """One helper PTY per draft; built inside the running event loop"""

    def __init__(self, idle_seconds: float = IDLE_SECONDS, owner_dir: Path | None = None) -> None:
        self.idle_seconds = idle_seconds
        self.owner_dir = owner_dir or paths.jarvis_home() / "ui" / "help-pty"
        self._sessions: dict[str, HelpSession] = {}
        self._tasks: set[asyncio.Task] = set()
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_loop())

    def _spawn_task(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def get(self, draft: str) -> HelpSession | None:
        s = self._sessions.get(draft)
        if s is None or not s.alive or s._proc.poll() is not None:
            return None
        return s

    async def start_or_feed(self, draft: str, launch: dict) -> tuple[HelpSession, bool]:
        s = self.get(draft)
        if s is not None:
            s._feed(launch)
            return s, False
        stale = self._sessions.get(draft)
        if stale is not None:
            await self._end(stale)
        # On the loop's thread: PDEATHSIG fires when the spawning THREAD exits
        s = self._spawn(draft, launch)
        self._sessions[draft] = s
        return s, True

    def _spawn(self, draft: str, launch: dict) -> HelpSession:
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            parent = os.getpid()
            proc = subprocess.Popen(launch["argv"], cwd=launch["cwd"], env=launch["env"],
                                    stdin=slave, stdout=slave, stderr=slave,
                                    preexec_fn=lambda: _child_setup(parent))
        except OSError as e:
            os.close(master)
            raise HelpPtyError(f"could not start {launch['argv'][0]}: {e}") from e
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        record = self.owner_dir / f"{os.getpid()}-{proc.pid}.json"
        try:
            self.owner_dir.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"dashboard_pid": os.getpid(), "pid": proc.pid,
                                          "argv": list(launch["argv"])}))
        except OSError as e:
            _kill_now(proc)
            os.close(master)
            raise HelpPtyError(f"could not record helper pid in {self.owner_dir}: {e}") from e
        return HelpSession(self, draft, proc, master, record)

    async def _end(self, s: HelpSession) -> None:
        """Kill, reap, tell subscribers, forget; idempotent"""
        if not s.alive:
            return
        s.alive = False
        loop = asyncio.get_running_loop()
        loop.remove_reader(s._master)
        loop.remove_writer(s._master)
        await _terminate(s._proc)
        os.close(s._master)
        for q in s._queues:
            _put(q, None)
        s._record.unlink(missing_ok=True)
        if self._sessions.get(s.draft) is s:
            del self._sessions[s.draft]

    async def close(self, draft: str) -> bool:
        s = self._sessions.get(draft)
        if s is None or not s.alive:
            return False
        await self._end(s)
        return True

    async def close_all(self) -> None:
        self._idle_task.cancel()
        for s in list(self._sessions.values()):
            await self._end(s)
        for t in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, self.idle_seconds / 2))
            for s in list(self._sessions.values()):
                if s._idle(self.idle_seconds) or s._proc.poll() is not None:
                    await self._end(s)

    def live_pids(self) -> list[int]:
        return [s.pid for s in self._sessions.values() if s.alive]

    def reap_stale(self) -> list[int]:
        """Kill helpers whose dashboard is dead; blocks up to KILL_GRACE per kill, so run it off the loop"""
        killed = []
        for rec in sorted(self.owner_dir.glob("*.json")) if self.owner_dir.is_dir() else []:
            try:
                data = json.loads(rec.read_text())
                owner, pid, argv = int(data["dashboard_pid"]), int(data["pid"]), data["argv"]
            except (OSError, ValueError, KeyError, TypeError):
                rec.unlink(missing_ok=True)
                continue
            if _pid_alive(owner):
                continue  # another live dashboard's helper
            with contextlib.suppress(OSError):
                if os.getpgid(pid) == pid and _same_process(pid, argv):
                    os.killpg(pid, signal.SIGTERM)
                    deadline = time.monotonic() + KILL_GRACE
                    while _same_process(pid, argv) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if _same_process(pid, argv):
                        os.killpg(pid, signal.SIGKILL)
                    killed.append(pid)
            rec.unlink(missing_ok=True)
        return killed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_now(proc: subprocess.Popen) -> None:
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


async def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM the group, SIGKILL after KILL_GRACE, reap; never blocks the loop"""
    for sig, grace in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, KILL_GRACE)):
        if proc.poll() is not None:
            break
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, sig)
        deadline = time.monotonic() + grace
        while proc.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
