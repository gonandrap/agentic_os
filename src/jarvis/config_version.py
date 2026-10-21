"""The config version ledger's arithmetic: canonical form (docs/superpowers/specs/2026-08-27-the-config-console.md §2)"""

from __future__ import annotations

import hashlib
import json
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import catalog
from .gates import GateConfig

__all__ = [
    "canonicalise", "diff", "resolve", "validation_config_from_resolved", "version_id",
]

# Dataclass field name -> the dotted path the catalog DOCUMENT spells it with, where the two differ
_RENAMES: dict[type, dict[str, str]] = {
    catalog.OsConfig: {
        "default_model": "defaults.model",
        "default_effort": "defaults.effort",
        "default_permission_mode": "defaults.permission_mode",
        "default_max_concurrent": "defaults.max_concurrent",
        "max_in_flight": "defaults.max_in_flight",
        "default_autocompact_window": "defaults.autocompact_window",
        "default_budget_usd": "defaults.budget_usd",
        "default_feature_budget_usd": "defaults.feature_budget_usd",
        "notification_sinks": "notifications.sinks",
        "telegram_token_env": "notifications.telegram.token_env",
        "telegram_chat_id_env": "notifications.telegram.chat_id_env",
        "ui_port": "ui.port",
        "ui_base_url": "ui.base_url",
    },
    GateConfig: {"extra_patterns": "patterns"},
}

# `raw` is the user's document, already stored whole as `document_json`
_SKIP: dict[type, frozenset[str]] = {
    catalog.ProjectSpec: frozenset({"raw", "name"}),
}


def canonicalise(document: Any) -> str:
    """The one spelling of a configuration document: sorted keys, 2-space indent"""
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)


def version_id(document: Any, build: str | None = None) -> str:
    """`cfg-` + the first 16 hex of sha256 over the canonical form"""
    canonical = canonicalise(document)
    if build:
        canonical = f"{canonical}\n@build {build}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"cfg-{digest[:16]}"


def _jsonable(value: Any) -> Any:
    """A resolved value has to survive `json.dumps` and come back equal enough to rebuild the dataclass it..."""
    if isinstance(value, Path):
        return str(value)
    # A dataclass reached as a VALUE rather than as a namespace to flatten
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (frozenset, set)):
        return sorted(str(v) for v in value)
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        renames = _RENAMES.get(type(value), {})
        skip = _SKIP.get(type(value), frozenset())
        for f in fields(value):
            if f.name in skip:
                continue
            _flatten(getattr(value, f.name),
                     f"{prefix}.{renames.get(f.name, f.name)}", out)
        return
    out[prefix] = _jsonable(value)


def resolve(cat: catalog.Catalog) -> dict[str, Any]:
    """Flatten a parsed catalog to `dotted path -> value`, every default materialised"""
    out: dict[str, Any] = {}
    _flatten(cat.os, "os", out)
    for project in cat.projects:
        _flatten(project, f"projects.{project.name}", out)
    return out


def diff(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Every path where two resolved maps disagree, ordered by path"""
    changes: list[dict[str, Any]] = []
    for path in sorted(set(a) | set(b)):
        if path not in b:
            changes.append({"path": path, "kind": "removed",
                            "old": a[path], "new": None})
        elif path not in a:
            changes.append({"path": path, "kind": "added",
                            "old": None, "new": b[path]})
        elif a[path] != b[path]:
            changes.append({"path": path, "kind": "changed",
                            "old": a[path], "new": b[path]})
    return changes


def _coerce(value: Any, default: Any) -> Any:
    """JSON gives back lists and dicts; the dataclass wants what it declared"""
    if isinstance(default, tuple):
        return tuple(value)
    if isinstance(default, frozenset):
        return frozenset(value)
    if isinstance(default, dict):
        return {str(k): str(v) for k, v in value.items()}
    return value


def _field_default(f: Any) -> Any:
    if f.default is not MISSING:
        return f.default
    return f.default_factory() if f.default_factory is not MISSING else None


def validation_config_from_resolved(
        resolved: dict[str, Any],
        project: str | None = None) -> catalog.ValidationConfig:
    """Rebuild a `ValidationConfig` from a stored `resolved_json` map"""
    prefix = "os.validation."
    if project:
        scoped = f"projects.{project}.validation."
        if any(k.startswith(scoped) for k in resolved):
            prefix = scoped

    kwargs: dict[str, Any] = {}
    for f in fields(catalog.ValidationConfig):
        key = prefix + f.name
        if key in resolved:  # absent = the shipped default stands
            kwargs[f.name] = _coerce(resolved[key], _field_default(f))
    return catalog.ValidationConfig(**kwargs)
