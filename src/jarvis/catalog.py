"""Catalog: the JSON file describing the fleet of projects Jarvis manages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Mirrors `claude --permission-mode` choices exactly (CLI rejects anything else).
VALID_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
}

# Default worker mode. `auto` runs routine tools (grep, edits, scripts, tests, git)
# without a prompt per action — the only way a `--bg` worker can run unattended, since
# a background session can't answer a prompt. Sensitive paths stay protected by the
# project's PreToolUse deny guards (catalog settings_overrides), which fire in every
# mode; `auto` does not weaken those. See ASSUMPTIONS.md §9.
DEFAULT_PERMISSION_MODE = "auto"

# Modes in which a `--bg` worker never stalls waiting for a human: `auto` (classifier
# vets each action), `bypassPermissions` (no checks), and `dontAsk` (unlisted tools are
# denied, not prompted). Every OTHER mode — acceptEdits, manual/default, plan — prompts
# on tool calls a real task needs (git, tests, scripts), and a background session can't
# answer, so it hangs. `worker_stalls_on_prompts()` flags those for the user.
AUTONOMOUS_PERMISSION_MODES = {"auto", "bypassPermissions", "dontAsk"}


def worker_stalls_on_prompts(mode: str) -> bool:
    """True when a background worker in this permission mode will block on a prompt."""
    return mode not in AUTONOMOUS_PERMISSION_MODES


# Default simultaneous work orders per project; the rest queue (catalog-tunable per
# project, or fleet-wide via os.defaults.max_concurrent).
DEFAULT_MAX_CONCURRENT = 5


class CatalogError(ValueError):
    """Raised when the catalog file is invalid."""


@dataclass
class WorkerDefaults:
    model: str | None = None
    effort: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    append_system_prompt: str | None = None


@dataclass
class ProjectSpec:
    name: str
    path: Path
    description: str = ""
    model: str | None = None
    worker: WorkerDefaults = field(default_factory=WorkerDefaults)
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class NeoConfig:
    """Neo, the OS answerer agent (responds to worker questions as the user)."""
    enabled: bool = True
    model: str = "opus"
    learnings_limit: int = 50
    timeout: int = 300


@dataclass
class OsConfig:
    default_model: str = "sonnet"
    default_effort: str | None = None
    default_permission_mode: str = DEFAULT_PERMISSION_MODE
    default_max_concurrent: int = DEFAULT_MAX_CONCURRENT
    notification_sinks: list[str] = field(default_factory=lambda: ["log"])
    telegram_token_env: str = "JARVIS_TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "JARVIS_TELEGRAM_CHAT_ID"
    ui_port: int = 8787
    knowledge_inject_limit: int = 8
    neo: NeoConfig = field(default_factory=NeoConfig)


@dataclass
class Catalog:
    os: OsConfig
    projects: list[ProjectSpec]
    source_path: Path | None = None

    def project(self, name: str) -> ProjectSpec:
        for p in self.projects:
            if p.name == name:
                return p
        raise CatalogError(f"unknown project {name!r} (known: {[p.name for p in self.projects]})")


def _err(msg: str) -> CatalogError:
    return CatalogError(f"catalog error: {msg}")


def load_catalog(path: str | Path) -> Catalog:
    path = Path(path).expanduser()
    if not path.exists():
        raise _err(f"file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise _err(f"invalid JSON in {path}: {e}") from e
    return parse_catalog(data, source_path=path)


def parse_catalog(data: Any, source_path: Path | None = None) -> Catalog:
    if not isinstance(data, dict):
        raise _err("top level must be an object")

    os_raw = data.get("os", {})
    defaults = os_raw.get("defaults", {})
    notif = os_raw.get("notifications", {})
    telegram = notif.get("telegram", {})
    ui = os_raw.get("ui", {})

    neo_raw = os_raw.get("neo", {})
    if not isinstance(neo_raw, dict):
        raise _err('"os.neo" must be an object')
    neo_cfg = NeoConfig(
        enabled=bool(neo_raw.get("enabled", True)),
        model=neo_raw.get("model", "opus"),
        learnings_limit=int(neo_raw.get("learnings_limit", 50)),
        timeout=int(neo_raw.get("timeout", 300)),
    )

    os_cfg = OsConfig(
        default_model=defaults.get("model", "sonnet"),
        default_effort=defaults.get("effort"),
        default_permission_mode=defaults.get("permission_mode", DEFAULT_PERMISSION_MODE),
        default_max_concurrent=int(defaults.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        notification_sinks=notif.get("sinks", ["log"]),
        telegram_token_env=telegram.get("token_env", "JARVIS_TELEGRAM_TOKEN"),
        telegram_chat_id_env=telegram.get("chat_id_env", "JARVIS_TELEGRAM_CHAT_ID"),
        ui_port=ui.get("port", 8787),
        knowledge_inject_limit=os_raw.get("knowledge_inject_limit", 8),
        neo=neo_cfg,
    )
    if os_cfg.default_permission_mode not in VALID_PERMISSION_MODES:
        raise _err(f"os.defaults.permission_mode {os_cfg.default_permission_mode!r} not in {sorted(VALID_PERMISSION_MODES)}")
    if os_cfg.default_max_concurrent < 1:
        raise _err("os.defaults.max_concurrent must be >= 1")

    projects_raw = data.get("projects")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise _err('"projects" must be a non-empty list')

    projects: list[ProjectSpec] = []
    seen: set[str] = set()
    for i, p in enumerate(projects_raw):
        if not isinstance(p, dict):
            raise _err(f"projects[{i}] must be an object")
        name = p.get("name")
        if not name or not isinstance(name, str):
            raise _err(f"projects[{i}].name is required")
        if name in seen:
            raise _err(f"duplicate project name {name!r}")
        seen.add(name)
        raw_path = p.get("path")
        if not raw_path:
            raise _err(f"projects[{i}] ({name}): path is required")
        ppath = Path(raw_path).expanduser().resolve()

        w = p.get("worker", {})
        pmode = w.get("permission_mode", os_cfg.default_permission_mode)
        if pmode not in VALID_PERMISSION_MODES:
            raise _err(f"project {name}: worker.permission_mode {pmode!r} invalid")
        max_conc = int(p.get("max_concurrent", os_cfg.default_max_concurrent))
        if max_conc < 1:
            raise _err(f"project {name}: max_concurrent must be >= 1")
        worker = WorkerDefaults(
            model=w.get("model") or p.get("model") or os_cfg.default_model,
            effort=w.get("effort", os_cfg.default_effort),
            permission_mode=pmode,
            append_system_prompt=w.get("append_system_prompt"),
        )
        projects.append(
            ProjectSpec(
                name=name,
                path=ppath,
                description=p.get("description", ""),
                model=p.get("model") or os_cfg.default_model,
                worker=worker,
                settings_overrides=p.get("settings_overrides", {}),
                max_concurrent=max_conc,
                raw=p,
            )
        )

    return Catalog(os=os_cfg, projects=projects, source_path=source_path)


def validate_paths(catalog: Catalog) -> list[str]:
    """Return human-readable problems with project paths (missing dir, not a git repo)."""
    problems = []
    for p in catalog.projects:
        if not p.path.is_dir():
            problems.append(f"{p.name}: path does not exist: {p.path}")
        elif not (p.path / ".git").exists():
            problems.append(f"{p.name}: not a git repository ({p.path}) — run `git init` first")
    return problems
