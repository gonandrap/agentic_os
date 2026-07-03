"""Thin wrapper around the `claude` CLI.

All interaction with Claude Code goes through here so tests can substitute a fake
`claude` executable (JARVIS_CLAUDE_BIN) and so a different backend can be swapped in.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def spawn_background(
    prompt: str,
    cwd: Path,
    name: str,
    session_id: str,
    model: str | None = None,
    effort: str | None = None,
    permission_mode: str | None = None,
    append_system_prompt: str | None = None,
    worktree: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Spawn a native Claude Code background session for a work order.

    The session id is chosen by us (UUID) so the work order row can reference it
    before the process even starts. Extra env vars (JARVIS_WO_ID etc.) are passed
    via --settings {"env": ...} so injected hooks can identify the work order.
    """
    args: list[str] = ["--bg", "--session-id", session_id, "--name", name]
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
    if env:
        args += ["--settings", json.dumps({"env": env})]
    args.append(prompt)
    _run(args, cwd=cwd, timeout=120)


def send_to_session(session_id: str, message: str, cwd: Path,
                    timeout: int = 900) -> str:
    """Deliver a user message to an existing session (headless resume).

    Runs a full turn: the session receives the message, processes it, and the
    result text is returned. The transcript is shared with the original session.
    """
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
