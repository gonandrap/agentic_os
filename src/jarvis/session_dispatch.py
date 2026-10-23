"""Session dispatch: which command launches a work order's `claude` session (docs/superpowers/specs/2026-10-22-session-dispatch.md)"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import claude_cli

REAL_CLAUDE_ENV = "JARVIS_REAL_CLAUDE"

PHASE_OPEN = "open"
PHASE_RESUME = "resume"

_LAUNCHER_KEYS = {"command", "open", "env", "context_window"}

DEFAULT_CONTEXT_WINDOW = 1_000_000

PLACEHOLDERS = frozenset({
    "prompt", "wo_name", "wo_id", "title", "project", "project_path", "model", "effort",
    "permission_mode", "compact_pct", "autocompact_window", "append_system_prompt",
    "settings_file", "inject_dir",
})

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Launcher:
    command: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    #: argv template that OPENS the session in place of `claude --bg …`; later turns resume bare
    open: tuple[str, ...] = ()
    context_window: int = DEFAULT_CONTEXT_WINDOW


def wo_name(wo_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", wo_id)


def compact_pct(window: int | None, context_window: int = DEFAULT_CONTEXT_WINDOW) -> str:
    if not window:
        return "100"
    return str(max(1, min(100, round(window * 100 / context_window))))


def _template(raw: Any, where: str) -> tuple[str, ...]:
    if (not isinstance(raw, list) or not raw
            or not all(isinstance(p, str) and p for p in raw)):
        raise ValueError(f"{where}: open must be a non-empty list of strings, got {raw!r}")
    for part in raw:
        for name in _PLACEHOLDER_RE.findall(part):
            if name not in PLACEHOLDERS:
                raise ValueError(f"{where}: unknown placeholder {{{name}}}; allowed: "
                                 f"{', '.join(sorted(PLACEHOLDERS))}")
    return (str(Path(raw[0]).expanduser()), *raw[1:])


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
    if "command" not in raw and "open" not in raw:
        raise ValueError(f"{where}: needs a command, an open template, or both")
    env = raw.get("env", {})
    if (not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
        raise ValueError(f"{where}: env must map names to string values, got {env!r}")
    window = raw.get("context_window", DEFAULT_CONTEXT_WINDOW)
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError(f"{where}: context_window must be a positive number of tokens, "
                         f"got {window!r}")
    return Launcher(
        command=_command(raw["command"], where) if "command" in raw else (),
        open=_template(raw["open"], where) if "open" in raw else (),
        env=dict(env), context_window=window)


@dataclass(frozen=True)
class Launch:
    launcher: Launcher | None
    project: str
    project_path: Path
    wo_id: str
    wo_kind: str
    phase: str
    transport: str

    wo_title: str = ""

    @property
    def opens_by_template(self) -> bool:
        return (self.launcher is not None and bool(self.launcher.open)
                and self.phase == PHASE_OPEN)

    def argv(self, claude_args: list[str]) -> list[str]:
        if self.launcher is None or not self.launcher.command:
            return [claude_cli.claude_bin(), *claude_args]
        return [*self.launcher.command, *claude_args]

    def open_argv(self, prompt: str, briefing: dict[str, Any]) -> list[str]:
        assert self.launcher is not None and self.launcher.open
        window = briefing.get("autocompact_window")
        values = {
            "prompt": prompt,
            "wo_name": wo_name(self.wo_id),
            "wo_id": self.wo_id,
            "title": self.wo_title,
            "project": self.project,
            "project_path": str(self.project_path),
            "model": str(briefing.get("model") or ""),
            "effort": str(briefing.get("effort") or ""),
            "permission_mode": str(briefing.get("permission_mode") or ""),
            "compact_pct": compact_pct(window, self.launcher.context_window),
            "autocompact_window": str(window or ""),
            "append_system_prompt": str(briefing.get("append_system_prompt") or ""),
            "settings_file": str(briefing.get("settings_file") or ""),
            "inject_dir": str(briefing.get("inject_dir") or ""),
        }
        return [_PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], part)
                for part in self.launcher.open]

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


def write_inject_dir(dest: Path, briefing: dict[str, Any]) -> Path:
    """The order's briefing as a claude-shining `--inject` dir"""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    settings = briefing.get("settings_file")
    if settings:
        shutil.copyfile(settings, dest / "settings-fragment.json")
    prompt = briefing.get("append_system_prompt")
    if prompt:
        (dest / "context.md").write_text(prompt, encoding="utf-8")
    for root in briefing.get("add_dirs") or []:
        for leaf in ("skills", "agents"):
            src = Path(root) / ".claude" / leaf
            if src.is_dir():
                shutil.copytree(src, dest / leaf, dirs_exist_ok=True)
    return dest.resolve()


def for_work_order(project: Any, wo: dict[str, Any], *, resume: bool,
                   transport: str) -> Launch:
    return Launch(launcher=getattr(project.worker, "launcher", None),
                  project=project.name, project_path=Path(project.path),
                  wo_id=str(wo["id"]), wo_kind=str(wo.get("kind") or ""),
                  phase=PHASE_RESUME if resume else PHASE_OPEN, transport=transport,
                  wo_title=str(wo.get("title") or ""))
