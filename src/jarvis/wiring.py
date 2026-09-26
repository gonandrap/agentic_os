"""Which of the USER'S OWN MCP servers, skills and plugins reach a dispatched worker.

Two halves, and the split is the whole design (spec
docs/superpowers/specs/2026-09-16-per-project-wiring.md):

* **Discovery** reads the user's Claude configuration to POPULATE the page. It never
  writes there — not a settings file, not `~/.claude.json`, not a plugin's state. A
  deselection is Jarvis state, kept in the catalog like every other project setting.
* **`settings_patch`** turns that state into keys for the file
  `dispatch._write_worker_settings` already writes and the spawn already passes to
  `--settings`. That file is per work order and per spawn, so a deselection takes effect
  at dispatch time and nowhere else.

EVERY LEVER HERE WAS VERIFIED LIVE against Claude Code 2.1.272 rather than read off a
doc; `jarvis learn show kn-…` (topic `worker environment`) records the probes and what
each one printed. The one that matters most for reading this file: a lever exists per
SOURCE, not per item, so what the page may offer is decided here and not by the UI.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .catalog import WiringConfig

#: Nothing in a settings file can unwire one of these individually. `/mcp disable` does
#: it by writing `disabledMcpServers` into the project entry of `~/.claude.json` — the
#: user's own configuration, which this feature may not touch — so the page offers the
#: block switch that a settings file DOES carry and says so on the row.
CLAUDE_AI_PREFIX = "claude.ai "
PLUGIN_SERVER_PREFIX = "plugin:"

LEVER_CONNECTORS = "connectors"
LEVER_BUNDLED = "bundled_skills"
LEVER_PLUGIN = "plugin:"
LEVER_SKILL = "skill:"

#: How Claude Code derives a tool prefix from a server name: every character that is not
#: a letter, digit or underscore becomes `_`. Read off this session's own tool list —
#: `claude.ai Google Drive` → `mcp__claude_ai_Google_Drive__`, `plugin:serena:serena` →
#: `mcp__plugin_serena_serena__` — which is also the mechanism behind
#: `dispatch.SERENA_TOOL_PREFIXES` having two entries for one server.
_TOOL_PREFIX_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def tool_prefix(server_name: str) -> str:
    """The `mcp__…__` prefix every tool of `server_name` is called under."""
    return f"mcp__{_TOOL_PREFIX_UNSAFE.sub('_', server_name)}__"


def claude_home() -> Path:
    """The user's Claude configuration directory, honouring `CLAUDE_CONFIG_DIR`."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


# -- the half that reaches a worker ---------------------------------------------------

def settings_patch(w: WiringConfig) -> dict[str, Any]:
    """The settings keys that carry `w`'s deselections into one spawn.

    Empty when nothing is deselected, and that emptiness is a requirement rather than an
    optimisation: at the shipped default the worker settings file must be byte-identical
    to the one written before this block existed.
    """
    patch: dict[str, Any] = {}
    if not w.claude_ai_connectors:
        # Gates the AUTO-FETCHED connectors only, which is exactly the set the page
        # lists; a connector passed explicitly on `--mcp-config` would survive, and
        # Jarvis passes none.
        patch["disableClaudeAiConnectors"] = True
    if not w.bundled_skills:
        patch["disableBundledSkills"] = True
    if w.disabled_plugins:
        patch["enabledPlugins"] = {pid: False for pid in w.disabled_plugins}
    if w.disabled_skills:
        # "off" hides the skill from the model AND from `/name`. The weaker values the
        # CLI accepts here ("name-only", "user-invocable-only") describe a human at a
        # prompt; a dispatched worker is not one, so a half-hidden skill would only be a
        # skill whose description was missing when the model reached for it.
        patch["skillOverrides"] = {name: "off" for name in w.disabled_skills}
    return patch


def serena_wired(w: WiringConfig) -> bool:
    """Is Serena still reaching this project's workers?

    Asked separately from every other server because Serena is NAMED — in the permission
    rules `dispatch` writes, in the navigation briefing every worker is handed, and in
    the planning seats' tool lists. Deselecting a server the OS goes on recommending is
    the incoherence this function exists to prevent (dispatch.py's `SERENA_READ_TOOLS`
    comment is the same trap seen from the other side).

    Read off the plugin list rather than off the MCP server list because that is where
    the lever is: `plugin:serena:serena` is unwired by unwiring the plugin that provides
    it. A Serena added by hand (`claude mcp add serena`) has no lever at all, so it stays
    wired and this stays True — which is the truth about that install, not a default.
    """
    return not any(pid.split("@")[0] == "serena" for pid in w.disabled_plugins)


# -- the half that reads the user's own configuration ---------------------------------

@dataclass(frozen=True)
class Item:
    """One thing the user has configured, and whether this project wires it."""

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
    """Every installed plugin, as `claude plugin list --json` reports it.

    The CLI rather than `~/.claude/plugins/installed_plugins.json`, which does not carry
    the enabled flag — that lives in settings, under its own precedence rules, and
    re-deriving it here would be a second answer to a question the CLI already answers.
    """
    data = json.loads(_claude(["plugin", "list", "--json"], timeout) or "[]")
    return [p for p in data if isinstance(p, dict) and p.get("id")]


def read_mcp_servers(timeout: int = 90) -> list[tuple[str, str]]:
    """`(name, detail)` for every MCP server the CLI can see, health checks and all.

    There is no `--json` here and no file that holds the whole answer: the claude.ai
    connectors are fetched from the account and the plugin servers are contributed at
    load time, so the CLI's own listing is the only place all sources meet. Parsed
    leniently — an unreadable line is skipped rather than raised, because a page that
    lists eight servers is better than a page that lists none.
    """
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
    """The `description:` line of a SKILL.md, or "" — front matter read as text.

    No YAML parser: this is one field of a file the OS does not own, and a skill whose
    front matter Jarvis cannot parse must still be listable and deselectable.
    """
    try:
        text = path.read_text(errors="replace")[:4000]
    except OSError:
        return ""
    match = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip().strip('"\'') if match else ""


def read_user_skills(home: Path | None = None) -> list[tuple[str, str]]:
    """`(name, description)` for every skill in the user's own skills directory."""
    root = (home or claude_home()) / "skills"
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        skill = d / "SKILL.md"
        if skill.is_file():
            out.append((d.name, _skill_title(skill)))
    return out


def _plugin_detail(entry: dict[str, Any]) -> str:
    """What a plugin contributes, read from the copy on disk."""
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


#: `claude mcp list` health-checks every server over the NETWORK — measured at 11.8s on
#: the machine this was built on. That is a fine cost for a terminal command and a bad
#: one in a page request, so the two callers get two entry points: `discover` blocks and
#: the CLI uses it, `cached` never blocks and the dashboard uses it. The daemon calls
#: neither: a dispatch reads the catalog and nothing else.
CACHE_SECONDS = 300.0
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"inventory": None, "thread": None}


def cached(*, refresh: bool = False) -> Inventory | None:
    """The last inventory, re-read in the background when it is stale or forced.

    `None` means "nothing read yet, a read is running" — the one state the page has to
    render as itself rather than as an empty list, because an empty list here would say
    the user has no MCP servers.
    """
    with _LOCK:
        inv = _STATE["inventory"]
        if inv is not None and not refresh and time.time() - inv.ts < CACHE_SECONDS:
            return inv
        thread = _STATE["thread"]
        if thread is None or not thread.is_alive():
            thread = threading.Thread(target=discover, kwargs={"refresh": True},
                                      name="jarvis-wiring", daemon=True)
            _STATE["thread"] = thread
            thread.start()
        # A stale answer beats a blank page while the re-read runs; `ts` is on the
        # record so the page can say how old it is.
        return None if refresh else inv


def peek() -> Inventory | None:
    """The cached inventory WITHOUT triggering a read. None means nothing read yet.

    `cached()` starts a discovery thread when the cache is cold, and discovery shells out
    to `claude mcp list`. `context.py` measures inside the dispatch path, where a
    measurement must not spawn a subprocess — so it reads the cache or says it is cold
    (§5 of docs/specs/2026-09-24-order-observability.md).
    """
    with _LOCK:
        return _STATE["inventory"]


def discover(*, refresh: bool = False, timeout: int = 90) -> Inventory:
    """Everything the user has configured, with no project applied. BLOCKS.

    Never raises: a source that cannot be read becomes a line in `errors`, because the
    page's job is to show what IS wired and a plugin listing that failed must not hide
    the MCP servers that were read fine.
    """
    with _LOCK:
        inv = _STATE["inventory"]
    if not refresh and inv is not None and time.time() - inv.ts < CACHE_SECONDS:
        return inv

    inv = Inventory(ts=time.time())
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
                            # No markdown here: `note` is rendered as text, on the
                            # page and by `jarvis config wiring` alike.
                            note="only /mcp disable unwires this one, and that writes "
                                 "to your own Claude configuration")
            inv.items.append(item)
    except Exception as e:  # noqa: BLE001
        inv.errors.append(f"could not list MCP servers: {e}")

    for name, detail in read_user_skills():
        inv.items.append(Item(kind="skill", name=name, source="your skills directory",
                              detail=detail, lever=f"{LEVER_SKILL}{name}"))
    inv.items.append(Item(
        kind="skill", name="Claude Code's bundled skills",
        source="Claude Code", lever=LEVER_BUNDLED,
        detail="the skills and workflows the CLI ships (code review, dataviz, …)",
        note="they wire as one block"))

    inv.ts = time.time()  # stamped when the read FINISHED: that is what goes stale
    with _LOCK:
        _STATE["inventory"] = inv
    return inv


def _plugin_of(server_name: str, plugin_ids: set[str]) -> str:
    """`plugin:serena:serena` → the installed id whose head is `serena`, or ""."""
    head = server_name.split(":")[1] if server_name.count(":") >= 2 else ""
    return next((pid for pid in sorted(plugin_ids) if pid.split("@")[0] == head), "")


def applied(w: WiringConfig, inv: Inventory) -> Inventory:
    """`inv` with this project's selection applied to each row's `wired`."""
    out = Inventory(errors=list(inv.errors), ts=inv.ts)
    for item in inv.items:
        out.items.append(replace(item, wired=lever_wired(w, item.lever)))
    return out


def lever_wired(w: WiringConfig, lever: str) -> bool:
    """Is what `lever` controls wired? An item with no lever is always wired."""
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
    """The catalog path a lever writes, and the value that puts it in state `wired`.

    ONE PLACE decides this, so the page, the CLI and any later surface cannot drift
    (kn-4ea33fe6: two surfaces that apply the same rule must share the resolver). The
    list levers are read-modify-write against `w`, sorted, so the same deselection made
    twice produces the same document and therefore the same config version.
    """
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
