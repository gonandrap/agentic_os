"""Thin wrapper around the `claude` CLI.

All interaction with Claude Code goes through here so tests can substitute a fake
`claude` executable (JARVIS_CLAUDE_BIN) and so a different backend can be swapped in.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


#: `claude` location override, mirroring bugreport's GH_BIN_ENV. Tests point it at a
#: fake; the test-isolation gate points it at a stub that refuses to run.
CLAUDE_BIN_ENV = "JARVIS_CLAUDE_BIN"


class ClaudeCliError(RuntimeError):
    pass


def claude_bin() -> str:
    return os.environ.get(CLAUDE_BIN_ENV, "claude")


def available() -> bool:
    return shutil.which(claude_bin()) is not None


def version() -> str:
    out = _run(["--version"], timeout=30)
    return out.strip()


def _run(args: list[str], cwd: Path | None = None, timeout: int = 120,
         env_extra: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        proc = subprocess.run(
            [claude_bin(), *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise ClaudeCliError(f"`{claude_bin()}` not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise ClaudeCliError(f"`claude {' '.join(args[:3])}...` timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise ClaudeCliError(
            f"claude {' '.join(args[:4])}... failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:500] or proc.stdout.strip()[:500]}"
        )
    return proc.stdout


# Claude Code's session-state vocabulary, as emitted by `claude agents --json` and by
# the supervisor's own `~/.claude/jobs/<id>/state.json` (verified against CLI 2.1.220).
# NOTE: there is no "running" here — that word belongs to Jarvis's *work order* status
# vocabulary (see project_store.WO_STATUSES). Conflating the two silently misreads every
# healthy worker as needing the user, so always go through the helpers below.
ACTIVE_STATES = frozenset({"working", "starting", "queued"})
BLOCKED_STATES = frozenset({"blocked"})
FINISHED_STATES = frozenset({"done", "failed", "cancelled"})


@dataclass
class BgSession:
    """A background session as reported by `claude agents --json`."""
    id: str
    session_id: str
    cwd: str
    name: str
    state: str  # working | blocked | done | failed | cancelled | ...
    kind: str = "background"
    started_at: float | None = None

    @property
    def is_active(self) -> bool:
        """The agent is making progress on its own — nothing is wanted from the user."""
        return self.state in ACTIVE_STATES

    @property
    def is_blocked(self) -> bool:
        """The agent stopped mid-turn on a permission prompt or a question."""
        return self.state in BLOCKED_STATES

    @property
    def is_finished(self) -> bool:
        """The turn ended, whether it succeeded, failed or was cancelled."""
        return self.state in FINISHED_STATES


def list_background_sessions(cwd: Path | None = None, include_done: bool = True,
                             timeout: int = 60) -> list[BgSession]:
    """The Claude sessions the user has open. `timeout` is short for interactive callers
    (a web request must not hang on a slow CLI) and generous for the daemon."""
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
) -> list[str]:
    """The flags that constitute a worker's briefing, in one place.

    A resumed session RE-DERIVES its system prompt, model, effort, permission mode
    and reachable directories from the argv it is launched with — it does not
    inherit them from the transcript. So every path that starts a worker turn
    (initial dispatch, bg resume-fork, headless resume) must pass the same set, or
    the worker silently loses the project's standing instructions and the OS skills
    from that turn onwards. Shared here so a new launch path cannot forget one.

    Callers must not append a bare positional after these: `--add-dir` is variadic
    and would swallow it (fence with `--` first, as spawn_background does).
    """
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
) -> str | None:
    """Spawn a native Claude Code background session; returns the job id if the
    CLI reported one.

    The supervisor daemon assigns the session id (a --session-id flag is ignored for
    --bg dispatches — verified empirically), so the work order is bound to its session
    afterwards: the SessionStart hook reports the real id, and the reconciler falls
    back to matching the unique `[WO <id>]` name.

    With resume_session_id, the new background agent continues that conversation
    (fork semantics: full context carried over, fresh session id — verified live).
    This is how user feedback is delivered while keeping the worker visible in the
    agents view.

    add_dirs are extra directories the session may reach; Claude also loads skills from
    each `<dir>/.claude/skills/`, which is how the OS ships its own skills to a worker
    whose worktree contains only tracked files.

    settings_file carries the FULL settings for the worker (OS-injected project
    settings merged with per-work-order env like JARVIS_WO_ID). It must be passed
    explicitly: the worker runs in a fresh git worktree, and the project's
    .claude/settings.json — being deliberately untracked — does not exist there.
    """
    args: list[str] = ["--bg", "--name", name]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if worktree:
        args += ["--worktree", worktree]
    args += _briefing_args(model, effort, permission_mode, append_system_prompt,
                           settings_file, add_dirs)
    # `--` fences the prompt off from option parsing. Without it a variadic option
    # (`--add-dir <directories...>` is one) keeps consuming positionals and swallows
    # the prompt as a directory: the session boots with nothing to do and parks at
    # the welcome screen forever, which reads as "created but never started".
    # It also lets a prompt begin with a dash. Never append anything after this.
    args.append("--")
    args.append(prompt)
    out = _run(args, cwd=cwd, timeout=120)
    m = _JOB_ID_RE.search(out or "")
    return m.group(1) if m else None


def stop_session(bg_id: str) -> bool:
    """Release a background session from the supervisor (`claude stop <id>`).

    Required before a headless resume: a session still owned by a live bg agent
    refuses `--resume` (verified live). Safe on already-stopped sessions.
    """
    try:
        _run(["stop", bg_id], timeout=30)
        return True
    except ClaudeCliError:
        return False


# -- worker turns (the transport every worker conversation runs on) --------------------


@dataclass
class TurnResult:
    """The parsed outcome of one headless turn (`claude -p --output-format json`)."""

    ok: bool
    result: str = ""
    session_id: str = ""
    error: str = ""
    cost_usd: float | None = None
    num_turns: int | None = None
    subtype: str = ""


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
) -> list[str]:
    """argv for one worker turn. Split out from `spawn_turn` so tests can assert on it.

    `--session-id` on the opening turn, `--resume` on every one after. Both name the
    SAME id: unlike `--bg --resume` (which forks the conversation under a supervisor-
    assigned id), a headless resume reuses the id it is given — `--fork-session` exists
    to opt into the other behaviour. That is what lets Jarvis mint the id up front and
    treat it as immutable for the work order's lifetime.
    """
    args = ["-p", "--output-format", "json",
            "--resume" if resume else "--session-id", session_id]
    if name:
        args += ["-n", name]
    if worktree:
        args += ["--worktree", worktree]
    args += _briefing_args(model, effort, permission_mode, append_system_prompt,
                           settings_file, add_dirs)
    # Same fence, same reason as `spawn_background`: `--add-dir` and `--tools` are both
    # variadic and will eat the prompt as an option value if it arrives bare. Nothing
    # may be appended after this.
    args += ["--", prompt]
    return args


def spawn_turn(prompt: str, cwd: Path, session_id: str, outfile: Path,
               errfile: Path, resume: bool = False, **kwargs: Any) -> int:
    """Start one worker turn as a detached process; returns its pid.

    Detached (`start_new_session=True`) on purpose: a turn can run for hours and
    `shipit` restarts jarvisd on every release, so a turn parented to the daemon would
    lose its reply on each deploy. Its own process group is also what makes `cancel()`
    able to take the whole tree down.

    stdin is /dev/null because `claude -p` otherwise spends three seconds waiting for
    input that is never coming, on every turn.
    """
    outfile.parent.mkdir(parents=True, exist_ok=True)
    args = turn_args(prompt, session_id, resume, **kwargs)
    try:
        with outfile.open("w") as out, errfile.open("w") as err:
            proc = subprocess.Popen(
                [claude_bin(), *args],
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
    except (FileNotFoundError, OSError) as e:
        raise ClaudeCliError(f"could not start `{claude_bin()}`: {e}") from e
    return proc.pid


def read_turn_result(outfile: Path, errfile: Path | None = None) -> TurnResult | None:
    """Parse a finished turn's output. None means "nothing usable there (yet)".

    The caller decides what None means: still running (the process is alive) or a
    turn that died without saying anything (it is not).
    """
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
    return TurnResult(
        ok=ok,
        result=data.get("result") or "",
        session_id=data.get("session_id") or "",
        error="" if ok else (data.get("result") or stderr_tail or "turn reported is_error"),
        cost_usd=data.get("total_cost_usd"),
        num_turns=data.get("num_turns"),
        subtype=data.get("subtype") or "",
    )


def process_alive(pid: int | None) -> bool:
    """Is this turn's process still running?

    Three cases, in the order they are asked, because no single check covers them all:

    1. **Still our child** (the usual case — same daemon that launched it).
       `waitpid(WNOHANG)` is authoritative *and* reaps it. `os.kill(pid, 0)` is not
       enough here: a detached child nobody waits on becomes a **zombie**, and signalling
       a zombie succeeds, so a finished turn would read as running forever.
    2. **Ours, but mid-launch.** `waitpid` returns 0 for a child that has forked but not
       yet exec'd, which is correct. Inspecting `/proc` instead would not be: in that
       window the cmdline is still the *parent's*, so a cmdline test declares a
       just-launched turn dead and reaps it before it has written anything. That is
       exactly what CI caught.
    3. **No longer our child** — jarvisd restarted, so the turn was reparented to init
       (which reaps it, making `kill` honest again). Only here is the `/proc` cmdline
       consulted, purely as a pid-reuse guard: a turn can run for hours, and a recycled
       pid read as "still running" would hang its work order forever. Matching is on a
       whole path component, never a substring — `claude` appears in plenty of unrelated
       command lines, this suite's own `.claude/worktrees/*/.venv/bin/python` included.
    """
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
    """Terminate a turn and everything it spawned. False if it was already gone."""
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


def run_headless(prompt: str, system_prompt: str | None = None,
                 model: str | None = None, cwd: Path | None = None,
                 timeout: int = 300, tools: str | None = None) -> str:
    """One-shot headless call (`claude -p`) returning the result text.

    Used by Neo: the system prompt (persona + learnings) is byte-stable across
    calls, so consecutive invocations within the Anthropic cache TTL share a
    cached prefix — question-specific content rides in `prompt`, after it.

    `tools` selects the callee's built-in tool set: `None` (default) leaves it
    alone, `""` strips every tool, or a comma-separated list ("Read,Bash").
    Strip them when the answer must come from the prompt rather than from the
    machine — a tooled callee will happily go read the real state and answer
    about *that*. Note this is availability, not permission: `--allowedTools`
    and `--disallowedTools` do not remove a tool, and under
    `permissions.defaultMode: auto` they do not stop it being used either.
    """
    args: list[str] = ["-p", prompt, "--output-format", "json"]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if model:
        args += ["--model", model]
    if tools is not None:  # "" is meaningful: it disables every tool
        args += ["--tools", tools]
    out = _run(args, cwd=cwd, timeout=timeout)
    try:
        data = json.loads(out)
        return data.get("result", "")
    except json.JSONDecodeError:
        return out


def session_transcript_path(cwd: Path, session_id: str) -> Path:
    """Location of the session transcript (~/.claude/projects/<munged-cwd>/<id>.jsonl)."""
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    munged = "".join(c if c.isalnum() else "-" for c in str(cwd))
    return config_dir / "projects" / munged / f"{session_id}.jsonl"
