"""The onboarding interview's draft file, and the catalog document it becomes (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §0.3, §3)"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import CatalogError, parse_catalog

SCHEMA = 1


class DraftError(ValueError):
    """A draft file this reader will not accept"""


@dataclass
class DraftProject:
    """First-class because a project's name never appears in the resolved map (§0.3)"""
    name: str
    path: str
    description: str = ""
    model: str = ""


@dataclass
class Draft:
    project: DraftProject
    reason: str = ""
    values: dict[str, Any] = field(default_factory=dict)
    answers: list[dict[str, Any]] = field(default_factory=list)
    schema: int = SCHEMA


def as_dict(draft: Draft) -> dict[str, Any]:
    return {
        "schema": draft.schema,
        "project": {"name": draft.project.name, "path": draft.project.path,
                    "description": draft.project.description,
                    "model": draft.project.model},
        "reason": draft.reason,
        "values": draft.values,
        "answers": draft.answers,
    }


def load(path: str | Path) -> Draft:
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except OSError as e:
        raise DraftError(f"cannot read the draft at {file}: {e}") from e
    except ValueError as e:
        raise DraftError(f"the draft at {file} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise DraftError(f"the draft at {file} must be a JSON object")

    schema = data.get("schema")
    if schema != SCHEMA:
        raise DraftError(
            f"the draft at {file} has schema {schema!r}, and this build reads schema "
            f"{SCHEMA} — the interview and the write path must agree on the draft schema")

    project = data.get("project")
    if not isinstance(project, dict):
        raise DraftError(f"the draft at {file} has no `project` object")
    name, ppath = project.get("name"), project.get("path")
    if not isinstance(name, str) or not name:
        raise DraftError("project.name is required")
    if not isinstance(ppath, str) or not ppath:
        raise DraftError("project.path is required")

    values = data.get("values", {})
    if not isinstance(values, dict) or not all(isinstance(k, str) for k in values):
        raise DraftError("`values` must be an object keyed by resolved config paths")
    answers = data.get("answers", [])
    if not isinstance(answers, list):
        raise DraftError("`answers` must be a list")

    return Draft(
        project=DraftProject(name=name, path=ppath,
                             description=str(project.get("description") or ""),
                             model=str(project.get("model") or "")),
        reason=str(data.get("reason") or ""),
        values=values,
        answers=answers,
        schema=SCHEMA,
    )


def save(draft: Draft, path: str | Path) -> Path:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(as_dict(draft), indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(file.parent), prefix=f".{file.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, file)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return file


def candidate_document(draft: Draft, document: dict[str, Any]) -> dict[str, Any]:
    """What the catalog document would become. Mutates nothing."""
    from . import ops

    candidate = copy.deepcopy(document)
    projects = candidate.setdefault("projects", [])
    if not isinstance(projects, list):
        raise DraftError('"projects" must be a list in the catalog file')
    entry = next((p for p in projects if isinstance(p, dict)
                  and p.get("name") == draft.project.name), None)
    if entry is None:
        entry = {"name": draft.project.name}
        projects.append(entry)
    entry["path"] = draft.project.path
    # An accepted default leaves the document silent, so an empty answer writes no key (§3)
    for key, value in (("description", draft.project.description),
                       ("model", draft.project.model)):
        if value:
            entry[key] = value

    for path, value in draft.values.items():
        container, leaf = ops._document_slot(candidate, path, create=True)
        assert container is not None  # `create=True` builds the whole chain
        container[leaf] = value
    return candidate


def validate(draft: Draft, document: dict[str, Any]) -> list[str]:
    """Every reason the candidate document would not parse, in the parser's own words"""
    candidate = candidate_document(draft, document)
    try:
        parse_catalog(json.loads(json.dumps(candidate)))
    except CatalogError as e:
        return [str(e)]
    return []
