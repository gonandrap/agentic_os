"""Which of the USER'S OWN MCP servers, skills and plugins reach a dispatched worker (docs/superpowers/specs/2026-09-16-per-project-wiring.md)"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import db
from .catalog import WiringConfig

#: Nothing in a settings file can unwire one of these individually
CLAUDE_AI_PREFIX = "claude.ai "
PLUGIN_SERVER_PREFIX = "plugin:"

LEVER_CONNECTORS = "connectors"
LEVER_BUNDLED = "bundled_skills"
LEVER_PLUGIN = "plugin:"
LEVER_SKILL = "skill:"

#: How Claude Code derives a tool prefix from a server name: every character that is not a letter
_TOOL_PREFIX_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def tool_prefix(server_name: str) -> str:
    """The `mcp__…__` prefix every tool of `server_name` is called under"""
    return f"mcp__{_TOOL_PREFIX_UNSAFE.sub('_', server_name)}__"


def claude_home() -> Path:
    """The user's Claude configuration directory, honouring `CLAUDE_CONFIG_DIR`"""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


# -- the half that reaches a worker

def settings_patch(w: WiringConfig) -> dict[str, Any]:
    """The settings keys that carry `w`'s deselections into one spawn"""
    patch: dict[str, Any] = {}
    if not w.claude_ai_connectors:
        # Gates the AUTO-FETCHED connectors only, which is exactly the set the page lists
        patch["disableClaudeAiConnectors"] = True
    if not w.bundled_skills:
        patch["disableBundledSkills"] = True
    if w.disabled_plugins:
        patch["enabledPlugins"] = {pid: False for pid in w.disabled_plugins}
    if w.disabled_skills:
        # "off" hides the skill from the model AND from `/name`
        patch["skillOverrides"] = {name: "off" for name in w.disabled_skills}
    return patch


def serena_wired(w: WiringConfig) -> bool:
    """Is Serena still reaching this project's workers?"""
    return not any(pid.split("@")[0] == "serena" for pid in w.disabled_plugins)


# -- the half that reads the user's own configuration

@dataclass(frozen=True)
class Item:
    """One thing the user has configured, and whether this project wires it"""

    kind: str          # "mcp" | "skill" | "plugin"
    name: str          # what the page shows and, for a list lever, what it stores
    source: str        # where it comes from: "claude.ai", "plugin <id>", "user", …
    detail: str = ""   # one line of description
    lever: str = ""    # "" = nothing can unwire it from a settings file
    wired: bool = True
    note: str = ""     # why it cannot be unwired, or what unwiring also takes


@dataclass
class Inventory:
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ts: float = 0.0

    @property
    def unwired(self) -> list[Item]:
        return [i for i in self.items if not i.wired]


def _claude(args: list[str], timeout: int) -> str:
    from .claude_cli import claude_bin

    proc = subprocess.run([claude_bin(), *args], capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:200])
    return proc.stdout


def read_plugins(timeout: int = 30) -> list[dict[str, Any]]:
    """Every installed plugin, as `claude plugin list --json` reports it"""
    data = json.loads(_claude(["plugin", "list", "--json"], timeout) or "[]")
    return [p for p in data if isinstance(p, dict) and p.get("id")]


def read_mcp_servers(timeout: int = 90) -> list[tuple[str, str]]:
    """`(name, detail)` for every MCP server the CLI can see, health checks and all"""
    out: list[tuple[str, str]] = []
    for line in _claude(["mcp", "list"], timeout).splitlines():
        line = line.strip()
        if not line or ": " not in line or line.endswith(":"):
            continue
        name, _, rest = line.partition(": ")
        detail, sep, _status = rest.rpartition(" - ")
        out.append((name.strip(), (detail if sep else rest).strip()))
    return out


def _skill_title(path: Path) -> str:
    """The `description:` line of a SKILL.md, or "" — front matter read as text"""
    try:
        text = path.read_text(errors="replace")[:4000]
    except OSError:
        return ""
    match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip().strip('"\'') if match else ""


def read_user_skills(home: Path | None = None) -> list[tuple[str, str]]:
    """`(name, description)` for every skill in the user's own skills directory"""
    root = (home or claude_home()) / "skills"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        skill = d / "SKILL.md"
        if skill.is_file():
            out.append((d.name, _skill_title(skill)))
    return out


def read_synced_skills(home: Path | None = None) -> list[tuple[str, str]]:
    """`(anthropic-skills:<name>, description)` for every claude.ai synced skill"""
    out = []
    for manifest in sorted(((home or claude_home()) / "skills" / "synced").glob("*/manifest.json")):
        try:
            skills = json.loads(manifest.read_text()).get("skills") or []
        except (OSError, ValueError, AttributeError):
            continue
        for s in skills:
            if isinstance(s, dict) and s.get("name"):
                out.append((f"anthropic-skills:{s['name']}", str(s.get("description") or "")))
    return sorted(set(out))


def _plugin_detail(entry: dict[str, Any]) -> str:
    """What a plugin contributes, read from the copy on disk"""
    path = Path(str(entry.get("installPath") or ""))
    bits = []
    manifest = path / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            bits.append(str(json.loads(manifest.read_text()).get("description") or ""))
        except (OSError, ValueError):
            pass
    skills = path / "skills"
    if skills.is_dir():
        n = sum(1 for d in skills.iterdir() if (d / "SKILL.md").is_file())
        if n:
            bits.append(f"{n} skill{'s' if n != 1 else ''}")
    return " · ".join(b for b in bits if b)


#: `claude mcp list` health-checks every server over the NETWORK
CACHE_SECONDS = 300.0
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"inventory": None, "thread": None}


def cached(*, refresh: bool = False) -> Inventory | None:
    """The last inventory, re-read in the background when it is stale or forced"""
    with _LOCK:
        inv = _STATE["inventory"]
        if inv is not None and not refresh and db.now() - inv.ts < CACHE_SECONDS:
            return inv
        thread = _STATE["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=discover, kwargs={"refresh": True},
                                      name="jarvis-wiring", daemon=True)
            _STATE["thread"] = thread
            thread.start()
        # A stale answer beats a blank page while the re-read runs
        return None if refresh else inv


def peek() -> Inventory | None:
    """The cached inventory WITHOUT triggering a read (docs/superpowers/specs/2026-09-24-order-observability.md)"""
    with _LOCK:
        return _STATE["inventory"]


def discover(*, refresh: bool = False, timeout: int = 90) -> Inventory:
    """Everything the user has configured, with no project applied"""
    with _LOCK:
        inv = _STATE["inventory"]
    if not refresh and inv is not None and db.now() - inv.ts < CACHE_SECONDS:
        return inv

    inv = Inventory(ts=db.now())
    plugin_ids: set[str] = set()
    try:
        for entry in read_plugins(timeout=min(timeout, 30)):
            pid = str(entry["id"])
            plugin_ids.add(pid)
            if not entry.get("enabled"):
                continue  # the user has it off already; wiring it on is not ours to do
            inv.items.append(Item(
                kind="plugin", name=pid, source=f"{entry.get('scope') or 'user'} scope",
                detail=_plugin_detail(entry), lever=f"{LEVER_PLUGIN}{pid}",
                note="its MCP servers, skills and agents go with it"))
    except Exception as e:  # noqa: BLE001 — a page that half-renders beats a 500
        inv.errors.append(f"could not list plugins: {e}")

    try:
        for name, detail in read_mcp_servers(timeout=timeout):
            if name.startswith(CLAUDE_AI_PREFIX):
                item = Item(kind="mcp", name=name, source="claude.ai connector",
                            detail=detail, lever=LEVER_CONNECTORS,
                            note="the claude.ai connectors wire as one block")
            elif name.startswith(PLUGIN_SERVER_PREFIX):
                pid = _plugin_of(name, plugin_ids)
                item = Item(kind="mcp", name=name, source=f"plugin {pid or '?'}",
                            detail=detail,
                            lever=f"{LEVER_PLUGIN}{pid}" if pid else "",
                            note=("unwiring it unwires the whole plugin"
                                  if pid else "its plugin is not installed at user "
                                              "scope, so there is no lever here"))
            else:
                item = Item(kind="mcp", name=name, source="your Claude config",
                            detail=detail,
                            # No markdown here: `note` is rendered as text, on the page and by `jarvis config wiring` alike
                            note="only /mcp disable unwires this one, and that writes "
                                 "to your own Claude configuration")
            inv.items.append(item)
    except Exception as e:  # noqa: BLE001
        inv.errors.append(f"could not list MCP servers: {e}")

    for name, detail in read_user_skills():
        inv.items.append(Item(kind="skill", name=name, source="your skills directory",
                              detail=detail, lever=f"{LEVER_SKILL}{name}"))
    for name, detail in read_synced_skills():
        inv.items.append(Item(kind="skill", name=name, source="claude.ai synced",
                              detail=detail, lever=f"{LEVER_SKILL}{name}"))
    inv.items.append(Item(
        kind="skill", name="Claude Code's bundled skills",
        source="Claude Code", lever=LEVER_BUNDLED,
        detail="the skills and workflows the CLI ships (code review, dataviz, …)",
        note="they wire as one block"))

    inv.ts = db.now()  # stamped when the read FINISHED: that is what goes stale
    with _LOCK:
        _STATE["inventory"] = inv
    return inv


def _plugin_of(server_name: str, plugin_ids: set[str]) -> str:
    """`plugin:serena:serena` → the installed id whose head is `serena`, or "\""""
    head = server_name.split(":")[1] if server_name.count(":") >= 2 else ""
    return next((pid for pid in sorted(plugin_ids) if pid.split("@")[0] == head), "")


def applied(w: WiringConfig, inv: Inventory) -> Inventory:
    """`inv` with this project's selection applied to each row's `wired`"""
    out = Inventory(errors=list(inv.errors), ts=inv.ts)
    for item in inv.items:
        out.items.append(replace(item, wired=lever_wired(w, item.lever)))
    return out


def lever_wired(w: WiringConfig, lever: str) -> bool:
    """Is what `lever` controls wired?"""
    if lever == LEVER_CONNECTORS:
        return w.claude_ai_connectors
    if lever == LEVER_BUNDLED:
        return w.bundled_skills
    if lever.startswith(LEVER_PLUGIN):
        return lever[len(LEVER_PLUGIN):] not in w.disabled_plugins
    if lever.startswith(LEVER_SKILL):
        return lever[len(LEVER_SKILL):] not in w.disabled_skills
    return True


def lever_setting(lever: str, wired: bool,
                  w: WiringConfig) -> tuple[str, Any]:
    """The catalog path a lever writes, and the value that puts it in state `wired`"""
    if lever == LEVER_CONNECTORS:
        return "wiring.claude_ai_connectors", bool(wired)
    if lever == LEVER_BUNDLED:
        return "wiring.bundled_skills", bool(wired)
    for prefix, key, current in (
        (LEVER_PLUGIN, "wiring.disabled_plugins", w.disabled_plugins),
        (LEVER_SKILL, "wiring.disabled_skills", w.disabled_skills),
    ):
        if lever.startswith(prefix):
            name = lever[len(prefix):]
            names = set(current) - {name} if wired else set(current) | {name}
            return key, sorted(names)
    raise ValueError(f"unknown wiring lever {lever!r}")
