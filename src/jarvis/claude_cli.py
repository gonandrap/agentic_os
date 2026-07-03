"""Thin wrapper around the `claude` CLI.

All interaction with Claude Code goes through here so tests can substitute a fake
`claude` executable (JARVIS_CLAUDE_BIN) and so a different backend can be swapped in.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class ClaudeCliError(RuntimeError):
    pass


def claude_bin() -> str:
    return os.environ.get("JARVIS_CLAUDE_BIN", "claude")


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


@dataclass
class BgSession:
    """A background session as reported by `claude agents --json`."""
    id: str
    session_id: str
    cwd: str
    name: str
    state: str  # running | blocked | done | ...
    kind: str = "background"
    started_at: float | None = None


def list_background_sessions(cwd: Path | None = None, include_done: bool = True) -> list[BgSession]:
    args = ["agents", "--json"]
    if include_done:
        args.append("--all")
    if cwd is not None:
        args += ["--cwd", str(cwd)]
    out = _run(args, timeout=60)
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
    args.append(prompt)
    out = _run(args, cwd=cwd, timeout=120)
    m = _JOB_ID_RE.search(out or "")
    return m.group(1) if m else None


def jobs_dir() -> Path:
    override = os.environ.get("JARVIS_CLAUDE_JOBS_DIR")
    if override:
        return Path(override)
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    return config / "jobs"


def wait_job_result(job_id: str, timeout: float = 900, poll: float = 5.0) -> str | None:
    """Best-effort: wait for a background job to finish and return its result text.

    Reads the supervisor's per-job state file (internal format — failures are
    swallowed, returning None)."""
    state_path = jobs_dir() / job_id / "state.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = json.loads(state_path.read_text())
            if state.get("state") == "done":
                output = state.get("output") or {}
                return output.get("result") if isinstance(output, dict) else None
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        time.sleep(poll)
    return None


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


def send_to_session(session_id: str, message: str, cwd: Path,
                    bg_id: str | None = None, timeout: int = 900) -> str:
    """Deliver a user message to an existing session (headless resume).

    Runs a full turn: the session receives the message, processes it, and the
    result text is returned. The transcript is shared with the original session.
    If the session is still attached to an (idle) background agent, it is released
    first — resume refuses to run against bg-owned sessions.
    """
    if bg_id:
        stop_session(bg_id)
    args = ["--resume", session_id, "-p", message, "--output-format", "json"]
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
