"""Session dispatch: which command launches a work order's `claude` session (docs/superpowers/specs/2026-10-22-session-dispatch.md)"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import claude_cli

REAL_CLAUDE_ENV = "JARVIS_REAL_CLAUDE"

PHASE_OPEN = "open"
PHASE_RESUME = "resume"

_LAUNCHER_KEYS = {"command", "env"}


@dataclass(frozen=True)
class Launcher:
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)


def _command(raw: Any, where: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        parts = [raw]
    elif isinstance(raw, list):
        parts = raw
    else:
        raise ValueError(f"{where}: command must be a path or a list of strings, got {raw!r}")
    if not parts or not all(isinstance(p, str) and p for p in parts):
        raise ValueError(f"{where}: command must be a non-empty path or list of strings, "
                         f"got {raw!r}")
    return (str(Path(parts[0]).expanduser()), *parts[1:])


def parse_launcher(raw: Any, where: str) -> Launcher | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return Launcher(command=_command(raw, where))
    unknown = set(raw) - _LAUNCHER_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}; "
                         f"allowed: {sorted(_LAUNCHER_KEYS)}")
    if "command" not in raw:
        raise ValueError(f"{where}: command is required")
    env = raw.get("env", {})
    if (not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
        raise ValueError(f"{where}: env must map names to string values, got {env!r}")
    return Launcher(command=_command(raw["command"], where), env=dict(env))


@dataclass(frozen=True)
class Launch:
    launcher: Launcher | None
    project: str
    project_path: Path
    wo_id: str
    wo_kind: str
    phase: str
    transport: str

    def argv(self, claude_args: list[str]) -> list[str]:
        if self.launcher is None:
            return [claude_cli.claude_bin(), *claude_args]
        return [*self.launcher.command, *claude_args]

    def env(self) -> dict[str, str]:
        if self.launcher is None:
            return {}
        return {
            **self.launcher.env,
            REAL_CLAUDE_ENV: claude_cli.claude_bin(),
            "JARVIS_LAUNCH_PROJECT": self.project,
            "JARVIS_LAUNCH_PROJECT_PATH": str(self.project_path),
            "JARVIS_LAUNCH_WO_ID": self.wo_id,
            "JARVIS_LAUNCH_WO_KIND": self.wo_kind,
            "JARVIS_LAUNCH_PHASE": self.phase,
            "JARVIS_LAUNCH_TRANSPORT": self.transport,
        }


def for_work_order(project: Any, wo: dict[str, Any], *, resume: bool,
                   transport: str) -> Launch:
    return Launch(launcher=getattr(project.worker, "launcher", None),
                  project=project.name, project_path=Path(project.path),
                  wo_id=str(wo["id"]), wo_kind=str(wo.get("kind") or ""),
                  phase=PHASE_RESUME if resume else PHASE_OPEN, transport=transport)
