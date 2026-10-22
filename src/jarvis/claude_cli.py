"""Thin wrapper around the `claude` CLI"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import systemd_units


log = logging.getLogger("jarvisd.claude")

#: `claude` location override, mirroring bugreport's GH_BIN_ENV
CLAUDE_BIN_ENV = "JARVIS_CLAUDE_BIN"

#: Buy the 5-minute prompt cache (write 1.25x) rather than the one-hour one (2x) (docs/superpowers/specs/2026-08-22-the-five-minute-write-everywhere.md)
PROMPT_CACHE_5M_ENV = {"FORCE_PROMPT_CACHING_5M": "1"}

#: Buy the ONE-HOUR write instead, for the `worker.cache_ttl` arm that asks for it (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
PROMPT_CACHE_1H_ENV_KEY = "ENABLE_PROMPT_CACHING_1H"

#: Every variable this decision OWNS
CACHE_TTL_KEYS = (*PROMPT_CACHE_5M_ENV, PROMPT_CACHE_1H_ENV_KEY)


#: WHICH TRANSPORT this session's turns run on, declared rather than assumed (docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md)
TURN_TRANSPORT_ENV = "JARVIS_TURN_TRANSPORT"
TRANSPORT_HEADLESS = "headless"      # spawn_turn: `claude -p`, one-shot
TRANSPORT_BACKGROUND = "background"  # spawn_background: `claude --bg`, supervisor-owned


def cache_env(explicit: dict[str, str] | None = None) -> dict[str, str]:
    """The 5-minute cache flag, overlaid by whatever a caller asked for EXPLICITLY"""
    return {**PROMPT_CACHE_5M_ENV, **(explicit or {})}


#: Above this many BYTES a system prompt goes to the CLI in a file rather than in argv (docs/superpowers/specs/2026-09-13-a-round-the-panel-can-afford.md)
SYSTEM_PROMPT_ARGV_LIMIT = 64 * 1024

#: Above this many BYTES a background turn's prompt goes by FILE: `--bg` reads no stdin (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)
BG_PROMPT_ARGV_LIMIT = 1024

#: Above this many BYTES the USER prompt goes to the CLI on STDIN rather than in argv (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)
PROMPT_ARGV_LIMIT = 64 * 1024

#: The BACKSTOP ceiling on `prompt + system_prompt` for one OS-side call, in chars (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
DEFAULT_MAX_OS_PROMPT_CHARS = 400_000

#: A floor guard just above the measured worst case: a ceiling below this silently disables... (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
MAX_OS_PROMPT_CHARS_MIN = 300_000

#: The live ceiling
MAX_OS_PROMPT_CHARS = DEFAULT_MAX_OS_PROMPT_CHARS


def set_max_os_prompt_chars(limit: int) -> None:
    """Arm the backstop from the catalog, at startup"""
    global MAX_OS_PROMPT_CHARS
    MAX_OS_PROMPT_CHARS = int(limit)


class ClaudeCliError(RuntimeError):
    pass


class InputTooLargeError(ClaudeCliError):
    """`execve` refused the argv: one argument was past `MAX_ARG_STRLEN` (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)"""


class PromptTooLargeError(ClaudeCliError):
    """The combined prompt was past `MAX_OS_PROMPT_CHARS` and the call was not made (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""

    def __init__(self, message: str, *, prompt_chars: int, system_prompt_chars: int,
                 ceiling: int) -> None:
        super().__init__(message)
        self.prompt_chars = prompt_chars
        self.system_prompt_chars = system_prompt_chars
        self.ceiling = ceiling


class AttributionRefused(RuntimeError):
    """A caller's `records_itself` declaration was not one it could make (docs/superpowers/specs/2026-09-25-attribution-is-not-a-callers-choice.md)"""


@contextlib.contextmanager
def _prompt_file(text: str) -> Iterator[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="jarvis-system-",
                                     encoding="utf-8", delete=True) as fh:
        fh.write(text)
        fh.flush()
        yield fh.name


@contextlib.contextmanager
def _system_prompt_arg(system_prompt: str | None, *,
                       keep_default_context: bool = False) -> Iterator[list[str]]:
    """The flags that carry a system prompt, and the temporary file when it needs one (docs/superpowers/specs/2026-09-25-a-headless-call-that-starts-from-nothing.md)"""
    if not system_prompt:
        yield []
        return
    raw = system_prompt.encode()
    if keep_default_context:
        if len(raw) <= SYSTEM_PROMPT_ARGV_LIMIT:
            yield ["--append-system-prompt", system_prompt]
            return
        with _prompt_file(system_prompt) as name:
            yield ["--append-system-prompt-file", name]
        return
    if len(raw) <= SYSTEM_PROMPT_ARGV_LIMIT:
        yield ["--system-prompt", system_prompt]
        return
    # No `--system-prompt-file` exists on CLI 2.1.282
    cut = SYSTEM_PROMPT_ARGV_LIMIT
    while cut > 0 and raw[cut] & 0xC0 == 0x80:  # never cut a UTF-8 sequence in half
        cut -= 1
    with _prompt_file(raw[cut:].decode()) as name:
        yield ["--system-prompt", raw[:cut].decode(),
               "--append-system-prompt-file", name]


@contextlib.contextmanager
def _empty_mcp_config() -> Iterator[list[str]]:
    """`--mcp-config` naming a server-free file: `--strict-mcp-config` alone still leaves the user's..."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="jarvis-mcp-",
                                     encoding="utf-8", delete=True) as fh:
        json.dump({"mcpServers": {}}, fh)
        fh.flush()
        yield ["--mcp-config", fh.name]


def claude_bin() -> str:
    return os.environ.get(CLAUDE_BIN_ENV, "claude")


def available() -> bool:
    return shutil.which(claude_bin()) is not None


def version() -> str:
    out = _run(["--version"], timeout=30)
    return out.strip()


def _run(args: list[str], cwd: Path | None = None, timeout: int = 120,
         env_extra: dict[str, str] | None = None,
         stdin_text: str | None = None) -> str:
    """One `claude` call, waited for (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)"""
    env = os.environ.copy()
    # `env_extra` is caller intent and must win over the cache default; ambient env loses
    env.update(cache_env(env_extra))
    try:
        proc = subprocess.run(
            [claude_bin(), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            input=stdin_text,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ClaudeCliError(f"`{claude_bin()}` not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClaudeCliError(f"`claude {' '.join(args[:3])}...` timed out after {timeout}s") from e
    except OSError as e:
        # AFTER `FileNotFoundError`, which is an `OSError` and keeps its own message
        if e.errno == errno.E2BIG:
            raise InputTooLargeError(
                f"`{claude_bin()}` could not be started: the input was too large for the "
                f"command line ({sum(len(a.encode()) for a in args)} bytes of arguments)"
            ) from e
        raise ClaudeCliError(f"could not start `{claude_bin()}`: {e}") from e
    if proc.returncode != 0:
        raise _cli_failure(args, proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout


def _is_signal_rc(rc: int) -> bool:
    """Was this exit a signal death, in EITHER of the two spellings? (#1039)"""
    return rc < 0 or rc in (137, 143)


def _cli_failure(args: list[str], rc: int, stdout: str, stderr: str) -> ClaudeCliError:
    """The exception a non-zero `claude` exit deserves, CLASSIFIED and untruncated (#235)"""
    detail = _result_field(stdout) or stderr.strip() or stdout.strip()
    limit = usage_limit(detail)
    if limit is not None:
        return UsageLimitError(limit)
    # AFTER the window and against the UNPREFIXED text: spec... (docs/superpowers/specs/2026-09-26-the-panel-must-not-mistake-an-auth-failure-for-a-verdict.md §2.)
    auth = auth_failure(detail)
    if auth is not None:
        return AuthFailureError(auth)
    # AFTER both of those and ONLY on an empty detail: spec §1 (docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md)
    if not detail and _is_signal_rc(rc):
        return KilledError(Killed(
            message=f"claude {' '.join(args[:4])}... was killed by a signal (rc={rc}) "
                    f"and wrote nothing",
            rc=rc))
    return ClaudeCliError(
        f"claude {' '.join(args[:4])}... failed (rc={rc}): {detail[:500]}")


def _result_field(stdout: str) -> str:
    """`result` out of an `--output-format json` envelope, or "" for anything else"""
    try:
        data = json.loads(stdout)
    except ValueError:  # JSONDecodeError is a subclass; plain text is the common case
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("result") or "").strip()


# Claude Code's session-state vocabulary
ACTIVE_STATES = frozenset({"working", "starting", "queued"})
BLOCKED_STATES = frozenset({"blocked"})
FINISHED_STATES = frozenset({"done", "failed", "cancelled"})


@dataclass
class BgSession:
    """A background session as reported by `claude agents --json`"""
    id: str
    session_id: str
    cwd: str
    name: str
    state: str  # working | blocked | done | failed | cancelled | ...
    kind: str = "background"
    started_at: float | None = None

    @property
    def is_active(self) -> bool:
        """The agent is making progress on its own — nothing is wanted from the user"""
        return self.state in ACTIVE_STATES

    @property
    def is_blocked(self) -> bool:
        """The agent stopped mid-turn on a permission prompt or a question"""
        return self.state in BLOCKED_STATES

    @property
    def is_finished(self) -> bool:
        """The turn ended, whether it succeeded, failed or was cancelled"""
        return self.state in FINISHED_STATES


def list_background_sessions(cwd: Path | None = None, include_done: bool = True,
                             timeout: int = 60) -> list[BgSession]:
    """The Claude sessions the user has open"""
    args = ["agents", "--json"]
    if include_done:
        args.append("--all")
    if cwd is not None:
        args += ["--cwd", str(cwd)]
    out = _run(args, timeout=timeout)
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"unparseable `claude agents --json` output: {out[:200]}") from e
    sessions = []
    for item in data:
        sessions.append(
            BgSession(
                id=item.get("id", ""),
                session_id=item.get("sessionId", ""),
                cwd=item.get("cwd", ""),
                name=item.get("name", ""),
                state=item.get("state", "unknown"),
                kind=item.get("kind", "background"),
                started_at=item.get("startedAt"),
            )
        )
    return sessions


_JOB_ID_RE = re.compile(r"claude stop ([0-9a-f]{6,})")


def _briefing_args(
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    settings_file: Path | None = None,
    add_dirs: list[Path] | None = None,
    autocompact_window: int | None = None,
    agent: str | None = None,
) -> list[str]:
    """The flags that constitute a worker's briefing, in one place"""
    args: list[str] = []
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]
    if settings_file:
        args += ["--settings", str(settings_file)]
    if autocompact_window:
        # `--autocompact <auto|tokens>`, single-valued
        args += ["--autocompact", str(autocompact_window)]
    if agent:
        # The LEAD session's persona, not a subagent type (docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md)
        args += ["--agent", agent]
    for d in add_dirs or []:
        args += ["--add-dir", str(d)]
    return args


def spawn_background(
    prompt: str,
    cwd: Path,
    name: str,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    worktree: str | None = None,
    settings_file: Path | None = None,
    resume_session_id: str | None = None,
    add_dirs: list[Path] | None = None,
    autocompact_window: int | None = None,
    agent: str | None = None,
) -> str | None:
    """Spawn a native Claude Code background session; returns the job id if the CLI reported one"""
    args: list[str] = ["--bg", "--name", name]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if worktree:
        args += ["--worktree", worktree]
    args += _briefing_args(model, effort, permission_mode, append_system_prompt,
                           settings_file, add_dirs, autocompact_window, agent)
    # `--` fences the prompt off from option parsing
    args.append("--")
    args.append(prompt)
    # This transport is supervisor-owned (docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md)
    out = _run(args, cwd=cwd, timeout=120,
               env_extra={TURN_TRANSPORT_ENV: TRANSPORT_BACKGROUND})
    m = _JOB_ID_RE.search(out or "")
    return m.group(1) if m else None


def stop_session(bg_id: str) -> bool:
    """Release a background session from the supervisor (`claude stop <id>`)"""
    try:
        _run(["stop", bg_id], timeout=30)
        return True
    except ClaudeCliError:
        return False


# -- worker turns on a background session (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)

#: `agents --json` `status` of a session with nothing left to do; anything else is `busy`
BG_IDLE = "idle"

#: How long `stop_bg` waits for the process to leave the roster before giving up
BG_STOP_WAIT_SECONDS = 15.0

#: How long `spawn_bg_turn` waits for a just-backgrounded session to be listed
BG_LIST_WAIT_SECONDS = 15.0

_BACKGROUNDED_RE = re.compile(r"backgrounded\s+·\s+([0-9a-f]{6,})")


@dataclass(frozen=True)
class BgEntry:
    """One session of `claude agents --json --all`, with what liveness needs"""

    id: str
    session_id: str
    #: None when the session has no process: stopped, crashed, or never started
    pid: int | None
    #: `busy` / `idle`, present only while there is a process
    status: str
    state: str
    cwd: str = ""

    @property
    def alive(self) -> bool:
        return self.pid is not None

    @property
    def idle(self) -> bool:
        """Settled: no turn running and no background task of its own in flight (fact 10)"""
        return self.alive and self.status == BG_IDLE


def bg_roster(timeout: int = 60) -> dict[str, BgEntry]:
    """Every background session, keyed by its full session id"""
    out = _run(["agents", "--json", "--all"], timeout=timeout)
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError as e:
        raise ClaudeCliError(f"unparseable `claude agents --json` output: {out[:200]}") from e
    roster: dict[str, BgEntry] = {}
    for item in data if isinstance(data, list) else []:
        sid = str(item.get("sessionId") or "")
        if not sid or item.get("kind", "background") != "background":
            continue
        pid = item.get("pid")
        roster[sid] = BgEntry(id=str(item.get("id") or sid[:8]), session_id=sid,
                              pid=int(pid) if isinstance(pid, int) else None,
                              status=str(item.get("status") or ""),
                              state=str(item.get("state") or ""),
                              cwd=str(item.get("cwd") or ""))
    return roster


def _bg_prompt(prompt: str, promptfile: Path | None) -> str:
    """The prompt as argv carries it: `--bg` reads no stdin, so an oversized one goes by file"""
    if len(prompt.encode()) <= BG_PROMPT_ARGV_LIMIT or promptfile is None:
        return prompt
    promptfile.parent.mkdir(parents=True, exist_ok=True)
    promptfile.write_text(prompt, encoding="utf-8")
    return (f"Your full instructions for this turn are in {promptfile} — read that "
            f"whole file first and follow it exactly as if it were this message.")


def _short_id(out: str) -> str:
    m = _BACKGROUNDED_RE.search(out or "") or _JOB_ID_RE.search(out or "")
    if not m:
        raise ClaudeCliError(f"`claude --bg` reported no session id: {(out or '')[:300]}")
    return m.group(1)


def spawn_bg_turn(prompt: str, cwd: Path, name: str, worktree: str | None = None,
                  promptfile: Path | None = None, **briefing: Any) -> BgEntry:
    """Open a worker's background session with its first prompt; returns its roster entry"""
    args = ["--bg", "-n", name]
    if worktree:
        args += ["--worktree", worktree]
    # `max_budget_usd` is `--print`-only: `worker_session.poll` enforces the cap instead
    briefing.pop("max_budget_usd", None)
    args += _briefing_args(**briefing)
    args += ["--", _bg_prompt(prompt, promptfile)]
    short = _short_id(_run(args, cwd=cwd, timeout=120))
    # `--bg` mints the id (fact 1); the roster is where the full one is read
    deadline = time.monotonic() + BG_LIST_WAIT_SECONDS
    while True:
        for entry in bg_roster().values():
            if entry.id == short:
                return entry
        if time.monotonic() >= deadline:
            raise ClaudeCliError(f"background session {short} never appeared in "
                                 f"`claude agents`")
        time.sleep(0.25)


def _pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_bg(short: str, wait: float = BG_STOP_WAIT_SECONDS) -> bool:
    """`claude stop`, then wait until the PROCESS is gone — the roster drops its pid ~0.6s earlier (fact 8)"""
    before = next((e for e in bg_roster().values() if e.id == short), None)
    try:
        _run(["stop", short], timeout=30)
    except ClaudeCliError:
        pass  # already stopped is as good; the roster below is the verdict
    deadline = time.monotonic() + wait
    while True:
        entry = next((e for e in bg_roster().values() if e.id == short), None)
        if ((entry is None or not entry.alive)
                and not _pid_running(before.pid if before else None)):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def resume_bg_turn(session_id: str, prompt: str, cwd: Path,
                   promptfile: Path | None = None) -> BgEntry:
    """Send the next prompt to a worker's own session: stopped first, resumed BARE (facts 5-8)"""
    short = session_id[:8]
    entry = bg_roster().get(session_id)
    if entry is not None and entry.alive and not stop_bg(entry.id):
        raise ClaudeCliError(f"background session {entry.id} would not stop; not "
                             f"resuming it, which would fork a copy")
    # NO flags: any flag, or a live original, starts a copy under a new id
    out = _run(["--bg", "--resume", session_id, "--", _bg_prompt(prompt, promptfile)],
               cwd=cwd, timeout=120)
    got = _short_id(out)
    if got != short:
        # A copy is a second conversation: take it down and refuse rather than drift onto it
        stop_session(got)
        remove_bg(got)
        raise ClaudeCliError(f"resuming {short} started a copy ({got}) instead: "
                             f"{out.strip()[:300]}")
    resumed = bg_roster().get(session_id)
    return resumed or BgEntry(id=short, session_id=session_id, pid=None, status="",
                              state="")


def respawn_bg(short: str) -> bool:
    """Restart a session's process under the same id, sending nothing (fact 11)"""
    try:
        _run(["respawn", short], timeout=60)
        return True
    except ClaudeCliError:
        return False


def remove_bg(short: str) -> str | None:
    """`claude rm`: the session, and its worktree when that is safe; its report, or None on failure"""
    try:
        return (_run(["rm", short], timeout=60) or "").strip()
    except ClaudeCliError:
        return None


# -- worker turns (the transport every worker conversation runs on)


@dataclass
class TurnResult:
    """The parsed outcome of one headless turn (`claude -p --output-format json`)"""

    ok: bool
    result: str = ""
    session_id: str = ""
    error: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    subtype: str = ""
    #: Why the CLI's query loop stopped (`terminal_reason`), verbatim
    terminal_reason: str = ""
    #: The HTTP status of the API error, when the failure was one
    api_error_status: int | None = None
    #: Compact usage envelope derived from the result JSON (see `derive_turn_usage`)
    usage: dict[str, Any] | None = None


#: Which reading of a result envelope produced a stored `usage_json`
USAGE_SCHEMA_VERSION = 3

#: The four token classes, as this envelope names them and as the two accountings in a result JSON...
_MODEL_TOKEN_KEYS = {
    "input": "inputTokens",
    "cache_write": "cacheCreationInputTokens",
    "cache_read": "cacheReadInputTokens",
    "output": "outputTokens",
}
_USAGE_TOKEN_KEYS = {
    "input": "input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "output": "output_tokens",
}
_TOKEN_CLASSES = tuple(_MODEL_TOKEN_KEYS)


def _reported_of(envelope: dict[str, Any] | None) -> dict[str, Any] | None:
    """The CUMULATIVE reading a stored envelope was derived from, or None"""
    if not isinstance(envelope, dict):
        return None
    # THE VERSION IS ASKED FIRST, and the `reported` block cannot overrule it
    if (envelope.get("usage_v") or 1) < 2:
        return None
    reported = envelope.get("reported")
    if isinstance(reported, dict):
        return reported
    return {
        "session_id": envelope.get("session_id") or "",
        "total_cost_usd": envelope.get("total_cost_usd"),
        "by_model": {m.get("model") or "": {c: m.get(c) or 0 for c in _TOKEN_CLASSES}
                     | {"cost_usd": m.get("cost_usd")}
                     for m in (envelope.get("by_model") or [])},
    }


def _sum_classes(by_model: dict[str, Any]) -> dict[str, int]:
    return {c: sum(m.get(c) or 0 for m in by_model.values()) for c in _TOKEN_CLASSES}


def _continues_previous(reported: dict[str, Any], previous: dict[str, Any] | None,
                        own: dict[str, int]) -> bool:
    """Is this envelope's `modelUsage` a running total that already contains `previous`? (#470)"""
    if previous is None:
        return False
    here, before_id = reported.get("session_id") or "", previous.get("session_id") or ""
    if here and before_id and here != before_id:
        return False
    before = _sum_classes(previous.get("by_model") or {})
    now = _sum_classes(reported.get("by_model") or {})
    if any(now[c] < before[c] for c in _TOKEN_CLASSES):
        return False
    floor = sum(own.values())
    if not floor:
        return True
    return sum(now[c] - before[c] for c in _TOKEN_CLASSES) >= floor


def derive_turn_usage(data: dict[str, Any],
                      previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The turn's exact accounting, compacted from one result envelope (docs/superpowers/findings/2026-09-19-the-turn-that-billed-the-session.md)"""
    raw_usage = data.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    models = {name: mu for name, mu in (data.get("modelUsage") or {}).items()
              if isinstance(mu, dict)}
    if not usage and not models:
        return None
    cache_creation = usage.get("cache_creation") or {}
    iterations = [i for i in (usage.get("iterations") or []) if isinstance(i, dict)]
    contexts = [
        (i.get("input_tokens") or 0)
        + (i.get("cache_read_input_tokens") or 0)
        + (i.get("cache_creation_input_tokens") or 0)
        for i in iterations
    ] or [
        # No per-call breakdown: the whole-turn totals still bound the context
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    ]
    windows = [mu["contextWindow"] for mu in models.values() if mu.get("contextWindow")]
    own = {cls: usage.get(key) or 0 for cls, key in _USAGE_TOKEN_KEYS.items()}
    reported = {
        "session_id": data.get("session_id") or "",
        "total_cost_usd": data.get("total_cost_usd"),
        "by_model": {
            name: {cls: mu.get(key) or 0 for cls, key in _MODEL_TOKEN_KEYS.items()}
            | {"cost_usd": mu.get("costUSD")}
            for name, mu in models.items()
        },
    }
    before = _reported_of(previous)
    continues = _continues_previous(reported, before, own)
    # THE DOLLARS ARE CLASSIFIED ON THEIR OWN, before any of this is spent — see `_cost_delta`
    cost, cost_per_turn = _cost_delta(
        reported["total_cost_usd"],
        (before or {}).get("total_cost_usd") if continues else None,
        bool((before or {}).get("cost_per_turn")) if continues else False)
    reported["cost_per_turn"] = cost_per_turn
    prior = (before or {}).get("by_model") or {}
    by_model = [
        {
            "model": name,
            # A model absent from the previous reading is new to the session
            **_model_delta(counts, prior.get(name) if continues else None,
                           cost_per_turn),
            "context_window": models[name].get("contextWindow"),
        }
        for name, counts in reported["by_model"].items()
    ]
    if by_model:
        totals = {c: sum(m[c] for m in by_model) for c in _TOKEN_CLASSES}
        version = USAGE_SCHEMA_VERSION
    else:
        totals = dict(own)
        version = 1
    return {
        "usage_v": version,
        "total_cost_usd": cost,
        **totals,
        # WHAT THE ENVELOPE SAID, before the delta
        "reported": reported,
        "continues": continues,
        "cache_1h": cache_creation.get("ephemeral_1h_input_tokens") or 0,
        "cache_5m": cache_creation.get("ephemeral_5m_input_tokens") or 0,
        # NOT the number of API calls, and it never was — see the note above
        "api_calls": None,
        "iterations_sampled": len(iterations),
        "num_turns": data.get("num_turns"),
        "context_peak": max(contexts),
        "context_window": max(windows) if windows else None,
        "duration_api_ms": data.get("duration_api_ms"),
        "cost_by_model": {m["model"]: m["cost_usd"] for m in by_model
                          if m.get("cost_usd") is not None},
        "by_model": by_model,
    }


def _model_delta(counts: dict[str, Any], before: dict[str, Any] | None,
                 cost_per_turn: bool = False) -> dict[str, Any]:
    """One model's share of a turn: its reading less the previous turn's"""
    if before is None or any((counts.get(c) or 0) < (before.get(c) or 0)
                             for c in _TOKEN_CLASSES):
        return {c: counts.get(c) or 0 for c in _TOKEN_CLASSES} | {
            "cost_usd": counts.get("cost_usd")}
    return {c: (counts.get(c) or 0) - (before.get(c) or 0)
            for c in _TOKEN_CLASSES} | {
        "cost_usd": _cost_delta(counts.get("cost_usd"), before.get("cost_usd"),
                                cost_per_turn)[0]}


def _cost_delta(now: float | None, before: float | None,
                per_turn: bool) -> tuple[float | None, bool]:
    """This turn's DOLLARS, decided on the dollars alone, and what they proved"""
    if now is None:
        return None, per_turn
    if before is None:
        return now, per_turn
    if now < before:
        return now, True
    if per_turn:
        return now, True
    return now - before, False


#: The prompt that compacts a resumed session, and the whole of the transport for it (docs/superpowers/specs/2026-09-18-compact-past-the-ttl.md)
COMPACT_PROMPT = "/compact"


def turn_args(
    prompt: str,
    session_id: str,
    resume: bool,
    name: str | None = None,
    worktree: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    settings_file: Path | None = None,
    add_dirs: list[Path] | None = None,
    autocompact_window: int | None = None,
    agent: str | None = None,
    max_budget_usd: float | None = None,
    prompt_via_stdin: bool = False,
) -> list[str]:
    """argv for one worker turn"""
    args = ["-p", "--output-format", "json",
            "--resume" if resume else "--session-id", session_id]
    if name:
        args += ["-n", name]
    if worktree:
        args += ["--worktree", worktree]
    args += _briefing_args(model, effort, permission_mode, append_system_prompt,
                           settings_file, add_dirs, autocompact_window, agent)
    if max_budget_usd is not None:
        # THE ENFORCEMENT POINT for a per-order budget (src/jarvis/budget.py
        args += ["--max-budget-usd", f"{max_budget_usd:.6f}"]
    if prompt_via_stdin:
        # Neither the fence nor the prompt: with no prompt in argv there is nothing to fence (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)
        return args
    # Same fence, same reason as `spawn_background`: `--add-dir` and `--tools` are both variadic and will...
    args += ["--", prompt]
    return args


@dataclass
class SpawnedTurn:
    """What a launched turn is tracked by: always a pid, and a unit when it got one"""

    pid: int | None
    #: The transient systemd unit the turn runs in, or None on the direct path
    unit: str | None = None


def spawn_turn(prompt: str, cwd: Path, session_id: str, outfile: Path,
               errfile: Path, resume: bool = False, unit: str | None = None,
               cache: dict[str, str] | None = None,
               **kwargs: Any) -> SpawnedTurn:
    """Start one worker turn; returns the pid to track it by and the unit it runs in (#133)"""
    outfile.parent.mkdir(parents=True, exist_ok=True)
    # THE PROMPT FILE BELONGS TO THE TURN (docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md)
    big = len(prompt.encode()) > PROMPT_ARGV_LIMIT
    promptfile = outfile.with_suffix(".prompt") if big else None
    if promptfile is not None:
        promptfile.write_text(prompt, encoding="utf-8")
    args = turn_args(prompt, session_id, resume, prompt_via_stdin=big, **kwargs)
    # Stripped first so the `cli` arm's ABSENCE survives a daemon whose own process carries the flag
    env = {k: v for k, v in os.environ.items() if k not in CACHE_TTL_KEYS}
    env.update(cache_env() if cache is None else cache)
    if unit and systemd_units.use_transient_units():
        prefix = systemd_units.run_prefix(
            unit, cwd=cwd, outfile=outfile, errfile=errfile, env=env,
            description=f"jarvis worker turn ({session_id})",
            stdin=promptfile,
        )
        try:
            # No redirection here: the unit's own StandardOutput= writes the result file
            done = subprocess.run([*prefix, claude_bin(), *args], capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("systemd-run unavailable for %s (%s) — spawning directly",
                        unit, e)
        else:
            if done.returncode == 0:
                pid = systemd_units.main_pid(unit)
                if pid:
                    return SpawnedTurn(pid=pid, unit=unit)
                # The unit started but never told us its pid
                log.warning("unit %s started but reported no MainPID", unit)
                return SpawnedTurn(pid=None, unit=unit)
            log.warning("systemd-run failed for %s (%s): %s — spawning directly",
                        unit, done.returncode, (done.stderr or "").strip()[:300])
    try:
        with contextlib.ExitStack() as stack:
            out = stack.enter_context(outfile.open("w"))
            err = stack.enter_context(errfile.open("w"))
            # The child inherits a dup of the fd, so the parent's handle closing here is harmless
            stdin: Any = subprocess.DEVNULL
            if promptfile is not None:
                stdin = stack.enter_context(promptfile.open("rb"))
            proc = subprocess.Popen(
                [claude_bin(), *args],
                cwd=cwd,
                env=env,
                stdin=stdin,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
    except OSError as e:
        if e.errno == errno.E2BIG:
            raise InputTooLargeError(
                f"`{claude_bin()}` could not be started: the input was too large for the "
                f"command line ({sum(len(a.encode()) for a in args)} bytes of arguments)"
            ) from e
        raise ClaudeCliError(f"could not start `{claude_bin()}`: {e}") from e
    return SpawnedTurn(pid=proc.pid)


def read_turn_result(outfile: Path, errfile: Path | None = None,
                     previous: dict[str, Any] | None = None) -> TurnResult | None:
    """Parse a finished turn's output"""
    try:
        raw = outfile.read_text()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    stderr_tail = ""
    if errfile is not None:
        try:
            stderr_tail = errfile.read_text().strip()[-1000:]
        except OSError:
            stderr_tail = ""
    ok = not data.get("is_error")
    # `errors` is the CLI's own list of what went wrong
    errors = " · ".join(str(e) for e in (data.get("errors") or []) if e)
    turn_usage = derive_turn_usage(data, previous)
    return TurnResult(
        ok=ok,
        result=data.get("result") or "",
        session_id=data.get("session_id") or "",
        error="" if ok else (data.get("result") or errors or stderr_tail
                             or "turn reported is_error"),
        # THIS TURN's dollars, not the session's: the same delta the usage envelope carries
        cost_usd=turn_usage["total_cost_usd"] if turn_usage
        else data.get("total_cost_usd"),
        num_turns=data.get("num_turns"),
        subtype=data.get("subtype") or "",
        terminal_reason=data.get("terminal_reason") or "",
        api_error_status=_int_or_none(data.get("api_error_status")),
        # Derived on the failed path too: the usage rides on a failed result JSON just the same (a 429'd turn...
        usage=turn_usage,
    )


def _int_or_none(value: Any) -> int | None:
    """`api_error_status` is declared nullable AND optional, so absent, null and a number are all legal"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -- the usage limit


@dataclass(frozen=True)
class UsageLimit:
    """A turn Claude Code refused because the usage window is spent"""

    message: str
    reset_at: float | None


class UsageLimitError(ClaudeCliError):
    """`claude` refused the call outright: the account's usage window is spent (#235)"""

    def __init__(self, limit: UsageLimit) -> None:
        super().__init__(limit.message)
        self.limit = limit


@dataclass
class Killed:
    """A seat whose process was killed by a signal and said nothing at all"""

    message: str
    rc: int


class KilledError(ClaudeCliError):
    """`claude` was KILLED BY A SIGNAL and wrote nothing: one external event, not a model failure (#1039)"""

    def __init__(self, killed: Killed) -> None:
        super().__init__(killed.message)
        self.killed = killed


class AuthFailureError(ClaudeCliError):
    """`claude` could not authenticate: the OAuth session expired, the login lapsed (#778)"""

    def __init__(self, auth: AuthFailure) -> None:
        super().__init__(auth.message)
        self.auth = auth


#: The legacy shape, and the only one that states the moment unambiguously: "Claude AI usage limit...
_LIMIT_EPOCH_RE = re.compile(r"usage limit reached\s*\|\s*(\d{10,13})", re.IGNORECASE)

#: A spent window, matched by SHAPE rather than by wording: the word "limit" and then a reset
_LIMIT_RE = re.compile(r"\blimits?\b.{0,60}?\bresets?\b", re.IGNORECASE | re.DOTALL)

#: "resets in 2h 15m" — the relative rendering (fast mode)
_RESET_IN_RE = re.compile(
    r"""resets?\s+in\s+
        (?:(?P<days>\d+)\s*d\b\s*)?
        (?:(?P<hours>\d+)\s*h\b\s*)?
        (?:(?P<minutes>\d+)\s*m\b\s*)?
        (?:(?P<seconds>\d+)\s*s\b)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: The absolute renderings: "resets 11:50pm (America/Los_Angeles)" under 24h
_RESET_RE = re.compile(
    r"""resets?\s+(?:at\s+)?
        (?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s*,?\s*
           (?:(?P<year>\d{4})\s*,?\s*)?(?:at\s+)?)?
        (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*
        (?P<ampm>[ap]\.?m\.?)?
        (?:\s*\((?P<tz>[^)]{1,64})\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}


def usage_limit(text: str | None, *, now: float | None = None) -> UsageLimit | None:
    """Read a failed turn's error as a usage-limit refusal, or None if it is not one"""
    if not text:
        return None
    epoch = _LIMIT_EPOCH_RE.search(text)
    if epoch:
        raw = int(epoch.group(1))
        # 13 digits is milliseconds, 10 is seconds; both shapes have been seen
        return UsageLimit(message=_summarise(text),
                          reset_at=float(raw / 1000 if raw > 1e11 else raw))
    if not _LIMIT_RE.search(text):
        return None
    ref = time.time() if now is None else now
    # The relative form first: "resets in 2h 15m" needs no timezone and no calendar
    relative = _RESET_IN_RE.search(text)
    if relative and any(relative.group(g) for g in
                        ("days", "hours", "minutes", "seconds")):
        return UsageLimit(message=_summarise(text),
                          reset_at=ref + _duration(relative))
    reset = _RESET_RE.search(text)
    if reset is None:
        return None
    return UsageLimit(message=_summarise(text), reset_at=_reset_moment(reset, ref))


def _duration(m: re.Match[str]) -> float:
    """Seconds in a "2d 3h 15m" clause"""
    return (int(m.group("days") or 0) * 86400
            + int(m.group("hours") or 0) * 3600
            + int(m.group("minutes") or 0) * 60
            + int(m.group("seconds") or 0))


def _summarise(text: str) -> str:
    return " ".join(text.split())[:200]


def _reset_moment(m: re.Match[str], now: float) -> float | None:
    """The epoch moment a "resets …" clause names, or None if it does not name one"""
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").replace(".", "").lower()
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    elif hour > 23:
        return None
    ref = datetime.fromtimestamp(now, _zone(m.group("tz")))
    month = _MONTHS.get((m.group("month") or "")[:3].lower())
    day = int(m.group("day") or 0)
    if month and day:
        year = int(m.group("year") or 0)
        try:
            when = ref.replace(year=year or ref.year, month=month, day=day, hour=hour,
                               minute=minute, second=0, microsecond=0)
        except ValueError:
            return None  # e.g. "Feb 30"
        # A date with no year is next year's when it has already gone past
        if not year and when < ref - timedelta(days=180):
            try:
                when = when.replace(year=when.year + 1)
            except ValueError:
                return None  # Feb 29 into a non-leap year
    else:
        when = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if when <= ref:
            when += timedelta(days=1)  # "resets 3am" said at 4pm means tomorrow
    return when.timestamp()


def _zone(name: str | None) -> tzinfo | None:
    """The named timezone, or the machine's own if it is missing or unknown"""
    if name:
        try:
            return ZoneInfo(name.strip())
        except (KeyError, ValueError, OSError):
            pass
    return datetime.now().astimezone().tzinfo


# -- the transport failing under us


#: The transient wordings the CLI assembles when the failure carried no HTTP status
_TRANSIENT_TEXT_RE = re.compile(
    r"API Error:.{0,200}?(?:"
    r"Connection to the API was lost"
    r"|The API is at capacity"
    r"|server-side issue, usually temporary"
    r")",
    re.IGNORECASE | re.DOTALL,
)

#: `terminal_reason` of a background turn whose session process died before it settled (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)
SESSION_LOST = "session_lost"

#: The status the CLI names in the message body when the envelope's own field is missing
_TRANSIENT_STATUS_RE = re.compile(r"API Error:\s*(5\d\d)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TransientFailure:
    """A turn the transport lost, not one the work broke"""

    message: str
    status: int | None


def transient_failure(text: str | None, *, terminal_reason: str | None = None,
                      api_error_status: int | None = None) -> TransientFailure | None:
    """Read a failed turn as a transient transport failure, or None if it is not one"""
    if not text:
        return None
    if usage_limit(text) is not None:
        return None  # the window, not the transport — the branch above owns it
    if terminal_reason == SESSION_LOST:
        # The process died under the turn: the transport, whatever the work had reached
        return TransientFailure(message=_summarise(text), status=None)
    if api_error_status is not None:
        # The CLI said what happened
        if api_error_status < 500:
            return None
        return TransientFailure(message=_summarise(text), status=api_error_status)
    if terminal_reason and terminal_reason != "api_error":
        # It stopped for a reason it named
        return None
    named = _TRANSIENT_STATUS_RE.search(text)
    if named:
        return TransientFailure(message=_summarise(text), status=int(named.group(1)))
    if _TRANSIENT_TEXT_RE.search(text):
        return TransientFailure(message=_summarise(text), status=None)
    return None


# -- the budget ran out

#: `terminal_reason` and `subtype` on an envelope the CLI stopped for `--max-budget-usd`
BUDGET_TERMINAL_REASON = "budget_exhausted"
BUDGET_SUBTYPE = "error_max_budget_usd"


def stopped_for_budget(result: TurnResult) -> bool:
    """Did `claude` stop this turn because it hit its budget?"""
    return (result.terminal_reason == BUDGET_TERMINAL_REASON
            or result.subtype == BUDGET_SUBTYPE)


# -- the account cannot answer
_AUTH_RE = re.compile(
    r"^\s*(?:"
    r"Failed to authenticate"
    r"|OAuth (?:token|session) expired"
    r"|Login expired"
    r"|Please run /login"
    r"|Invalid API key"
    r"|API Error: 401"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class AuthFailure:
    """A turn refused because Claude Code could not authenticate"""

    message: str


def auth_failure(text: str | None) -> AuthFailure | None:
    """Read a failed turn as an authentication failure, or None if it is not one"""
    if not text or not _AUTH_RE.search(text):
        return None
    return AuthFailure(message=_summarise(text))


# The CLI's `model_not_found` texts, verbatim from the 2.1.308 bundle
_MODEL_REJECTED_RE = re.compile(
    r"There's an issue with the selected model \(([^)]*)\)"
    r"|The model (\S+) is not available on your")


def model_rejected(text: str | None) -> str | None:
    """The model id a failed turn says claude refused, or None"""
    m = _MODEL_REJECTED_RE.search(text or "")
    return (m.group(1) or m.group(2)) if m else None


#: Where Claude Code keeps the OAuth session it signs in with and refreshes
CREDENTIALS_ENV = "JARVIS_CLAUDE_CREDENTIALS"


def credentials_path() -> Path:
    override = os.environ.get(CREDENTIALS_ENV)
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser() / ".credentials.json"


def signin_changed_at() -> float | None:
    """When Claude Code's stored sign-in last changed, or None if nothing can be said"""
    path = credentials_path()
    try:
        changed = path.stat().st_mtime
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
    expires = oauth.get("refreshTokenExpiresAt") if isinstance(oauth, dict) else None
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and \
            expires / 1000 <= time.time():
        return None
    return changed


def process_alive(pid: int | None) -> bool:
    """Is this turn's process still running?"""
    if not pid:
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
        return reaped != pid  # 0 → still running; pid → it just exited (now reaped)
    except ChildProcessError:
        pass  # not (or no longer) our child — fall through to the signal check
    except (OSError, ValueError):
        pass
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True  # alive, just not ours to signal
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True  # no /proc (or it vanished mid-read) — trust the signal
    want = Path(claude_bin()).name
    return any(Path(tok).name == want
               for tok in raw.decode(errors="replace").split("\0") if tok)


def kill_process_group(pid: int | None) -> bool:
    """Terminate a turn and everything it spawned"""
    if not pid:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False


@dataclass
class HeadlessResult:
    """One `claude -p` one-shot call: what it said, and what it cost"""

    text: str
    usage: dict[str, Any] | None = None
    session_id: str = ""
    #: The model that actually served the call
    model: str = ""
    #: How big this call's own input was, in characters (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    prompt_chars: int = 0
    system_prompt_chars: int = 0
    #: How long the subprocess took, in milliseconds. 0 means NOT MEASURED (docs/superpowers/specs/2026-10-01-neo-observability.md)
    latency_ms: int = 0


def _attribute_subprocess(result: HeadlessResult, record: Any = None) -> None:
    """Record a headless call made from inside a work order's process tree"""
    wo_id = os.environ.get("JARVIS_WO_ID", "").strip()
    if not wo_id:
        return
    from . import agent_usage

    # What RAN the call, not where it ran
    label = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    (record or agent_usage.record)(
        agent_usage.WORKER_SUBPROCESS, usage=result.usage,
        project=os.environ.get("JARVIS_PROJECT", ""), wo_id=wo_id,
        label=label or "claude -p", model=model_of(result), ok=bool(result.usage))


def model_of(result: HeadlessResult) -> str:
    """The model to price a headless call at"""
    return result.model or "unknown"


#: How far out the caller walk looks before refusing
_CALLER_FRAME_CAP = 20

#: Modules that FORWARD a caller's declaration instead of making one
_FORWARDERS = frozenset({"jarvis.claude_cli", "jarvis.structured", "jarvis.seats"})


@dataclass(frozen=True)
class Authorisation:
    """A checked declaration, minted where the frames prove it and carried where they do not"""

    kind: str

    def __post_init__(self) -> None:
        _check_records_itself(self.kind)


def authorise(kind: str) -> Authorisation:
    """Mint an `Authorisation` for `kind` on THIS thread, or raise `AttributionRefused`"""
    return Authorisation(kind)


def _check_kind(kind: str) -> None:
    """Spec §2 conditions 1 and 2: the kind itself, with nothing said about the caller"""
    from . import agent_usage

    if kind not in agent_usage.KIND_LABELS:
        raise AttributionRefused(
            f"records_itself={kind!r} is not an OS accounting kind "
            "(agent_usage.KIND_LABELS): a caller that records a call itself must name "
            "the kind it writes.")
    if kind in agent_usage.SUBPROCESS_KINDS:
        raise AttributionRefused(
            f"records_itself={kind!r} is the kind this transport writes itself; "
            "a caller cannot claim it.")


def _check_records_itself(kind: str) -> None:
    """Raise `AttributionRefused` unless `kind` is a declaration this caller can make"""
    _check_kind(kind)
    nearest = ""
    frame: Any = sys._getframe(1)
    seen = 0
    while frame is not None and seen < _CALLER_FRAME_CAP:
        module = frame.f_globals.get("__name__", "")
        # A forwarder's frame is not evidence of an OS declaration — see `_FORWARDERS`
        if module not in _FORWARDERS:
            nearest = nearest or module
            if module == "jarvis" or module.startswith("jarvis."):
                return
            seen += 1
        frame = frame.f_back
    raise AttributionRefused(
        f"records_itself={kind!r} is the OS's own opt-out from subprocess attribution "
        f"and {nearest} may not make it: a call from outside the jarvis package is "
        "billed to JARVIS_WO_ID and cannot be switched off. Delete the argument.")


def run_headless_result(prompt: str, system_prompt: str | None = None,
                        model: str | None = None, cwd: Path | None = None,
                        timeout: int = 300, tools: str | None = None, *,
                        records_itself: str | Authorisation = "", record: Any = None,
                        permission_mode: str | None = None,
                        env_extra: dict[str, str] | None = None,
                        settings: Path | str | None = None,
                        keep_default_context: bool = False) -> HeadlessResult:
    """One-shot headless call (`claude -p`), with its accounting kept (docs/superpowers/specs/2026-09-13-a-round-the-panel-can-afford.md)"""
    # FIRST, before any argument is built or any subprocess runs: a refused call spends nothing (spec §2)
    if isinstance(records_itself, Authorisation):
        # Already checked where the frames proved it (spec §3)
        _check_kind(records_itself.kind)
    elif records_itself:
        _check_records_itself(records_itself)
    # How big our own input was, kept whether or not the reply parses (spec §3 (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    prompt_chars = len(prompt)
    system_prompt_chars = len(system_prompt or "")
    # THE BACKSTOP, spec §4 of the same spec
    if prompt_chars + system_prompt_chars > MAX_OS_PROMPT_CHARS:
        kind = (records_itself.kind if isinstance(records_itself, Authorisation)
                else (records_itself or "unattributed"))
        # NUMBERS AND IDENTIFIERS ONLY — never a fragment of the prompt: this message reaches the inbox
        raise PromptTooLargeError(
            f"refused a {kind} call: prompt {prompt_chars} chars + system prompt "
            f"{system_prompt_chars} chars = {prompt_chars + system_prompt_chars}, "
            f"over the {MAX_OS_PROMPT_CHARS}-char ceiling (os.max_os_prompt_chars)",
            prompt_chars=prompt_chars, system_prompt_chars=system_prompt_chars,
            ceiling=MAX_OS_PROMPT_CHARS)
    # THE PROMPT'S SECOND DOOR, spec §2: docs/superpowers/specs/2026-09-26-a-prompt-too-big-for-argv.md
    over = len(prompt.encode()) > PROMPT_ARGV_LIMIT
    args: list[str] = ["-p", *([] if over else [prompt]), "--output-format", "json",
                       "--no-session-persistence"]
    if model:
        args += ["--model", model]
    if settings:
        args += ["--settings", str(settings)]
    if tools is not None:  # "" is meaningful: it disables every tool
        args += ["--tools", tools]
    if permission_mode:
        args += ["--permission-mode", permission_mode]
    with contextlib.ExitStack() as stack:
        if tools == "":
            # `--tools ""` strips the BUILT-IN tools and leaves every MCP server's schemas in the request
            args += ["--strict-mcp-config", *stack.enter_context(_empty_mcp_config()),
                     "--setting-sources", "", "--disable-slash-commands"]
        args += stack.enter_context(
            _system_prompt_arg(system_prompt, keep_default_context=keep_default_context))
        # AROUND THE SUBPROCESS AND NOTHING ELSE (docs/superpowers/specs/2026-10-01-neo-observability.md)
        started = time.monotonic()
        out = _run(args, cwd=cwd, timeout=timeout, env_extra=env_extra,
                   stdin_text=prompt if over else None)
        latency_ms = int((time.monotonic() - started) * 1000)
    data: Any = None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        pass
    if not isinstance(data, dict):
        # Not JSON at all, or not an object: the text is still the answer (that is what `run_headless` has...
        result = HeadlessResult(text=out, model=model or "",
                                prompt_chars=prompt_chars,
                                system_prompt_chars=system_prompt_chars,
                                latency_ms=latency_ms)
    else:
        served = [name for name in (data.get("modelUsage") or {})]
        result = HeadlessResult(
            text=data.get("result", ""),
            usage=derive_turn_usage(data),
            prompt_chars=prompt_chars,
            system_prompt_chars=system_prompt_chars,
            latency_ms=latency_ms,
            session_id=data.get("session_id") or "",
            # One key is the ordinary case; more than one means the call was served by several models and no...
            model=(served[0] if len(served) == 1 else "") or model or "",
        )
    if result.usage is not None:
        # Injected HERE rather than inside `derive_turn_usage`: a worker turn's envelope comes off a result...
        result.usage["prompt_chars"] = prompt_chars
        result.usage["system_prompt_chars"] = system_prompt_chars
        # Same reason one field along: the sites that hand over `usage=result.usage` never see the dataclass... (docs/superpowers/specs/2026-10-01-neo-observability.md)
        result.usage["latency_ms"] = latency_ms
    if not records_itself:
        _attribute_subprocess(result, record)
    return result


def run_headless(prompt: str, system_prompt: str | None = None,
                 model: str | None = None, cwd: Path | None = None,
                 timeout: int = 300, tools: str | None = None, *,
                 records_itself: str | Authorisation = "", record: Any = None,
                 permission_mode: str | None = None,
                 env_extra: dict[str, str] | None = None,
                 settings: Path | str | None = None,
                 keep_default_context: bool = False) -> str:
    """`run_headless_result`, keeping only the text (#103)"""
    return run_headless_result(prompt, system_prompt=system_prompt, model=model,
                               cwd=cwd, timeout=timeout, tools=tools,
                               records_itself=records_itself, record=record,
                               permission_mode=permission_mode,
                               env_extra=env_extra, settings=settings,
                               keep_default_context=keep_default_context).text


def run_session_turn(prompt: str, session_id: str, *, resume: bool, cwd: Path,
                     model: str | None = None, append_system_prompt: str | None = None,
                     permission_mode: str | None = None,
                     add_dirs: list[Path] | None = None,
                     timeout: int = 900) -> HeadlessResult:
    """One waited-for turn of a LONG-LIVED OS session (Morpheus): `--session-id` first, `--resume` after"""
    args = turn_args(prompt, session_id, resume, model=model,
                     permission_mode=permission_mode,
                     append_system_prompt=append_system_prompt, add_dirs=add_dirs or None)
    started = time.monotonic()
    out = _run(args, cwd=cwd, timeout=timeout)
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        data: Any = json.loads(out)
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        return HeadlessResult(text=out, model=model or "", prompt_chars=len(prompt),
                              latency_ms=latency_ms, session_id=session_id)
    served = list(data.get("modelUsage") or {})
    usage = derive_turn_usage(data)
    if usage is not None:
        usage["prompt_chars"] = len(prompt)
        usage["latency_ms"] = latency_ms
    return HeadlessResult(
        text=str(data.get("result") or ""), usage=usage,
        session_id=str(data.get("session_id") or session_id),
        model=(served[0] if len(served) == 1 else "") or model or "",
        prompt_chars=len(prompt), latency_ms=latency_ms)


def unpack_headless(value: HeadlessResult | str) -> tuple[str, dict[str, Any] | None]:
    """(text, usage) from whichever of the two a transport seam returned"""
    if isinstance(value, HeadlessResult):
        return value.text, value.usage
    return value, None


def session_transcript_path(cwd: Path, session_id: str) -> Path:
    """Location of the session transcript (~/.claude/projects/<munged-cwd>/<id>.jsonl)"""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    munged = "".join(c if c.isalnum() else "-" for c in str(cwd))
    return config_dir / "projects" / munged / f"{session_id}.jsonl"
