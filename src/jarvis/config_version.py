"""The config version ledger's arithmetic: canonical form, content-addressed id,
resolution to dotted paths, and the way back to a `ValidationConfig`.

A leaf module by design — it imports `catalog` and nothing else of the OS. See
docs/superpowers/specs/2026-08-27-the-config-console.md §2 for what a version IS and
why the stored unit is a whole document rather than a per-key edit.

Two representations, two jobs, and confusing them is the mistake this module exists to
prevent:

- the **document** is the catalog file's own JSON, canonicalised. It is what the file is
  rewritten from and what the id hashes, so it must stay the RAW document — never
  `asdict(Catalog)`, which would eat the forward-compatible keys `parse_catalog`
  deliberately ignores.
- the **resolved map** is `parse_catalog(document)` flattened to `path -> value` with
  every default materialised at write time. It is evidence of what ran, and materialising
  is what makes it survive a release that moves a default (§2, §6).

`resolve()` and `validation_config_from_resolved()` perform no inheritance of their own:
project-vs-OS validation inheritance lives in `catalog._parse_validation` (§1.2).
"""

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

# Dataclass field name -> the dotted path the catalog DOCUMENT spells it with, where the
# two differ. `OsConfig` flattens the document's `defaults`, `notifications` and `ui`
# objects into one namespace; the resolved map spells them the document's way, because
# that is the path the user types at `jarvis config set`.
_RENAMES: dict[type, dict[str, str]] = {
    catalog.OsConfig: {
        "default_model": "defaults.model",
        "default_effort": "defaults.effort",
        "default_permission_mode": "defaults.permission_mode",
        "default_max_concurrent": "defaults.max_concurrent",
        "default_autocompact_window": "defaults.autocompact_window",
        "notification_sinks": "notifications.sinks",
        "telegram_token_env": "notifications.telegram.token_env",
        "telegram_chat_id_env": "notifications.telegram.chat_id_env",
        "ui_port": "ui.port",
        "ui_base_url": "ui.base_url",
    },
    GateConfig: {"extra_patterns": "patterns"},
}

# `raw` is the user's document, already stored whole as `document_json`; `name` is the
# path segment the project's settings hang under, not a setting under it.
_SKIP: dict[type, frozenset[str]] = {
    catalog.ProjectSpec: frozenset({"raw", "name"}),
}


def canonicalise(document: Any) -> str:
    """The one spelling of a configuration document: sorted keys, 2-space indent.

    No trailing newline — a file written as `canonicalise(doc) + "\\n"` reads back
    through `json.loads` to the same document and so to the same canonical form.
    """
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)


def version_id(document: Any, build: str | None = None) -> str:
    """`cfg-` + the first 16 hex of sha256 over the canonical form.

    Content-addressed, the same move as `evidence.fingerprint`, and its two consequences
    are features rather than side effects (§2): an edit that changes nothing writes no
    row, and re-applying an old configuration lands back on its old id.

    `build` SALTS the hash, and is for `actor="release"` rows ONLY (§6.1, Neo question
    181). A release rebase records the same document resolved under a new build: same
    document, so the same id, so no row — the salt is what gives that fact a row of its
    own. Every other writer must leave it None, or an edit would land on a different id
    on every upgrade and the two consequences above would both stop holding.
    `CentralStore.add_config_version` is the only caller that passes it, off `actor`.
    """
    canonical = canonicalise(document)
    if build:
        canonical = f"{canonical}\n@build {build}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"cfg-{digest[:16]}"


def _jsonable(value: Any) -> Any:
    """A resolved value has to survive `json.dumps` and come back equal enough to
    rebuild the dataclass it came from — see `_coerce` for the way back."""
    if isinstance(value, Path):
        return str(value)
    # A dataclass reached as a VALUE rather than as a namespace to flatten —
    # `catalog.SupervisorConfig.probes` is a tuple of them. Same predicate as `_flatten`
    # above. `_coerce`, the way back, needs no matching branch: its only caller is
    # `validation_config_from_resolved`, and `ValidationConfig` holds no dataclass.
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
    """Flatten a parsed catalog to `dotted path -> value`, every default materialised.

    Reflective over the dataclasses on purpose: a field added to `ValidationConfig`, or a
    `validation` block added to `ProjectSpec` by the per-project work order, appears here
    with no edit to this module — which is what lets the two run in parallel (§2).

    `Catalog.source_path` is not a setting and does not appear.
    """
    out: dict[str, Any] = {}
    _flatten(cat.os, "os", out)
    for project in cat.projects:
        _flatten(project, f"projects.{project.name}", out)
    return out


def diff(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, Any]]:
    """Every path where two resolved maps disagree, ordered by path.

    `kind` is the authority on which side exists: `old`/`new` are always present and are
    `None` on the missing side, which a real null value looks exactly like.
    """
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
    """JSON gives back lists and dicts; the dataclass wants what it declared."""
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
    """Rebuild a `ValidationConfig` from a stored `resolved_json` map.

    The only legal way to judge a round under the version it was stamped with: §6 forbids
    ever re-parsing a historical document with `parse_catalog`, so without this the read
    has no implementation.

    A LOOKUP, NOT A MERGE. `project` names a prefix; the fallback to the `os` block is
    WHOLE-block and never per-key, so nothing here changes when the per-project work
    order lands and `resolve()` starts yielding `projects.<name>.validation.*`. Today it
    yields none, and every project falls back. `project=None` means the OS block.
    """
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
