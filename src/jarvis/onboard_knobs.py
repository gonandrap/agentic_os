"""Every resolved config path classified into a behaviour area (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §1)"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

__all__ = [
    "AREA_KEYS", "AREA_PREFIXES", "AREA_SCOPE", "Area", "MUST_HAVE", "UNASKABLE",
    "area_of", "asked", "coverage",
]


@dataclass(frozen=True)
class Area:
    """Declared here, filled by `onboard_prose` (§4): this module holds no user-facing text"""

    key: str
    title: str
    scope: str
    summary: str
    on_costs: str
    off_costs: str
    refs: tuple[tuple[str, str], ...]
    presets: dict[str, dict[str, Any]]


# The areas the user named by name; §2 and §5 both need to know when they are done
MUST_HAVE = frozenset({
    "evolution", "validation", "neo", "rules", "supervisor", "morpheus", "steward",
    "gates", "budgets", "model", "investigation", "compaction",
})

AREA_KEYS: tuple[str, ...] = (
    # must-haves first, in the order the user listed them
    "evolution",
    "validation",
    "neo",
    "rules",
    "supervisor",
    "investigation",
    "morpheus",
    "steward",
    "gates",
    "budgets",
    "compaction",
    "model",
    # the rest
    "fleet_health",
    "ui_health",
    "schedule",
    "navigation",
    "concision",
    "cost",
    "inspect",
    "observability",
    "messaging",
    "delivery",
    "knowledge",
    "wiring",
    "worktree",
    "notifications",
    "environment",
)

# Area key to the tails it owns; longest entry wins, so a narrow tail carves out of a wide one
AREA_PREFIXES: dict[str, tuple[str, ...]] = {
    "evolution": ("evolution.mode", "evolution.min_samples", "evolution.major_min_refs",
                  "evolution.medium_min_refs", "evolution.min_refs_by_kind",
                  "evolution.min_precision"),
    "validation": ("validation",),
    "neo": ("neo",),
    "rules": ("rules",),
    "supervisor": ("supervisor",),
    "investigation": ("worker.investigation_model", "worker.investigation_effort",
                      "worker.investigation_budget_usd", "neo.review_findings"),
    "morpheus": ("morpheus",),
    "steward": ("autonomy.self_dispatch", "evolution.weekly_usd",
                "evolution.critical_weekly_usd", "evolution.order_budget_usd",
                "evolution.yield_above_usd", "evolution.max_depth"),
    "gates": ("gates",),
    "budgets": ("max_concurrent", "defaults.max_in_flight", "worker.budget_usd",
                "worker.feature_budget_usd"),
    "compaction": ("worker.autocompact_window", "worker.cache_ttl", "compact_min_context",
                   "cold_prefix_floor", "cold_prefix_floor_max", "cache_health_window_days",
                   "cache_health_min_orders", "cache_health_min_boundaries",
                   "cache_health_prefix_share"),
    "model": ("model", "worker.model", "worker.effort", "worker.permission_mode",
              "worker.transport"),
    "fleet_health": ("fleet_health",),
    "ui_health": ("ui_health",),
    "schedule": ("schedule",),
    "navigation": ("navigation", "worker.py_nav_hook", "worker.doc_nav_hook",
                   "worker.bash_first", "worker.tool_search",
                   "worker.doc_read_limit_lines"),
    "concision": ("concision",),
    "cost": ("cost",),
    "inspect": ("inspect",),
    "observability": ("observability",),
    "messaging": ("messaging",),
    "delivery": ("bugs", "pull_requests", "release"),
    "knowledge": ("knowledge_digest_chars", "knowledge_digest_limit", "knowledge_hint_chars",
                  "knowledge_hint_limit", "knowledge_inject_limit", "knowledge_pinned_chars"),
    "wiring": ("wiring",),
    "worktree": ("worktree",),
    "notifications": ("notifications", "ui"),
    "environment": ("worker.denied_tools", "worker.mcp_tool_timeout_ms",
                    "worker.require_crew", "worker.append_system_prompt", "worker.launcher",
                    "disk_free_warn_gb", "max_os_prompt_chars", "path", "description",
                    "settings_overrides"),
}

AREA_SCOPE: dict[str, str] = {
    "evolution": "both",
    "validation": "both",
    "neo": "os",
    "rules": "both",
    "supervisor": "both",
    "investigation": "both",
    "morpheus": "os",
    # Only `autonomy.self_dispatch` decides anything here and it is OS-wide, so §2 warns on the whole area
    "steward": "os",
    "gates": "project",
    "budgets": "both",
    "compaction": "both",
    "model": "both",
    "fleet_health": "both",
    "ui_health": "both",
    "schedule": "both",
    "navigation": "both",
    "concision": "both",
    "cost": "both",
    "inspect": "both",
    "observability": "both",
    "messaging": "both",
    "delivery": "both",
    "knowledge": "os",
    "wiring": "both",
    "worktree": "both",
    "notifications": "os",
    "environment": "both",
}

# Identity and plumbing: classified, never asked
UNASKABLE = frozenset({
    "path", "description", "settings_overrides", "worker.launcher",
    "ui.port", "ui.base_url",
    "notifications.telegram.token_env", "notifications.telegram.chat_id_env",
})

# One knob, two spellings: `config_version._RENAMES` and `default_*` vs the project's `worker.`
_OS_TAIL_ALIASES: dict[str, str] = {
    "defaults.model": "model",
    "defaults.effort": "worker.effort",
    "defaults.permission_mode": "worker.permission_mode",
    "defaults.max_concurrent": "max_concurrent",
    "defaults.autocompact_window": "worker.autocompact_window",
    "defaults.budget_usd": "worker.budget_usd",
    "defaults.feature_budget_usd": "worker.feature_budget_usd",
    "default_bash_first": "worker.bash_first",
    "default_cache_ttl": "worker.cache_ttl",
    "default_doc_nav_hook": "worker.doc_nav_hook",
    "default_doc_read_limit_lines": "worker.doc_read_limit_lines",
    "default_py_nav_hook": "worker.py_nav_hook",
    "default_tool_search": "worker.tool_search",
    "default_transport": "worker.transport",
}

# Leaf names a user holds an opinion about; the rest are thresholds, char budgets and rosters
_OPINION_LEAVES = frozenset({
    "enabled", "health_enabled", "mode", "model", "level", "effort", "permission_mode",
    "transport", "cache_ttl", "bash_first", "tool_search", "py_nav_hook", "doc_nav_hook",
    "require_crew", "self_dispatch", "auto_merge", "auto_review", "follow_ups",
    "feature_units", "fast_path", "review_findings", "max_concurrent", "max_in_flight",
    "budget_usd", "feature_budget_usd", "investigation_budget_usd", "investigation_model",
    "investigation_effort", "weekly_usd", "critical_weekly_usd", "order_budget_usd",
    "yield_above_usd", "max_depth", "stakes_classifier", "on_failed", "on_timeout",
    "chair_model", "digest_model", "label", "bundled_skills", "claude_ai_connectors",
    "false_positive_disarm", "chrome",
})

_LONGEST_FIRST: tuple[tuple[str, str], ...] = tuple(sorted(
    ((prefix, key) for key, prefixes in AREA_PREFIXES.items() for prefix in prefixes),
    key=lambda item: len(item[0]), reverse=True,
))


def _tail(path: str) -> str | None:
    """`os.x.y` and `projects.<name>.x.y` are the same decision at two scopes"""
    if path.startswith("os."):
        tail = path[3:]
        return _OS_TAIL_ALIASES.get(tail, tail)
    if path.startswith("projects."):
        parts = path.split(".", 2)
        return parts[2] if len(parts) == 3 else None
    return None


def area_of(path: str) -> tuple[str, str] | None:
    """(area key, canonical path tail) — the tail is the OS/project-agnostic spelling"""
    tail = _tail(path)
    if tail is None:
        return None
    for prefix, key in _LONGEST_FIRST:
        if tail == prefix or tail.startswith(prefix + "."):
            return key, tail
    return None


@lru_cache(maxsize=1)
def _shape() -> dict[str, Any]:
    """Canonical tail -> the value the resolver emits, so `asked` can see dicts and lists"""
    from . import catalog, config_version

    document = {"os": {}, "projects": [{"name": "_", "path": "/",
                                        "description": ""}]}
    resolved = config_version.resolve(catalog.parse_catalog(document))
    out: dict[str, Any] = {}
    for path, value in resolved.items():
        tail = _tail(path)
        if tail is not None:
            out[tail] = value
    return out


def asked(path: str) -> bool:
    """A scalar or short enum the user can hold an opinion about — a rule, not a list"""
    hit = area_of(path)
    if hit is None:
        return False
    tail = hit[1]
    if tail in UNASKABLE:
        return False
    if isinstance(_shape().get(tail), (dict, list)):
        return False
    return tail.rsplit(".", 1)[-1] in _OPINION_LEAVES


def coverage(resolved: dict[str, Any]) -> list[str]:
    """Every path the registry cannot classify — a new knob in `catalog.py` shows up here"""
    return sorted(path for path in resolved if area_of(path) is None)
