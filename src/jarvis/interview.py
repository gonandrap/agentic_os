"""The onboarding interview: questions, postures, the two scopes, the re-run (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §2)"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

from . import config_version, onboard_draft, paths
from .catalog import parse_catalog
from .onboard_draft import Draft, DraftProject
from .onboard_knobs import AREA_KEYS, Area, area_of, asked

CUSTOM = "custom"
DEFAULTS = "defaults"
PRESET = "preset:"
# Provenance in `answers`, which §3 ignores, so the draft schema stays frozen (Neo Q2315)
FLEET_WARNED = {"area": "fleet", "mode": "warned"}
_SWEEP_KEYS = ("supervisor.enabled", "supervisor.health_enabled")


class InterviewError(ValueError):
    """An answer the interview will not record"""


@dataclass(frozen=True)
class Question:
    area: str
    title: str
    summary: str
    on_costs: str
    off_costs: str
    enable: str | None
    current: Any
    postures: dict[str, dict[str, Any]]
    fleet_warning: str | None


def draft_file(name: str) -> Path:
    return paths.onboarding_dir() / f"{name}.json"


def save(draft: Draft) -> Path:
    return onboard_draft.save(draft, draft_file(draft.project.name))


@lru_cache(maxsize=1)
def _default_paths() -> frozenset[str]:
    """Every resolved path this build has, with the project spelled `_`"""
    document = {"os": {}, "projects": [{"name": "_", "path": "/", "description": ""}]}
    return frozenset(config_version.resolve(parse_catalog(document)))


def _os_spelling(tail: str) -> str | None:
    return next((p for p in sorted(_default_paths())
                 if p.startswith("os.") and (area_of(p) or ("", ""))[1] == tail), None)


def _spell(area: Area, tail: str, name: str, fleet: bool) -> str:
    """`both` areas write the project spelling unless the user asked for fleet-wide (§2)"""
    project_path = f"projects.{name}.{tail}"
    has_project = f"projects._.{tail}" in _default_paths()
    if area.scope == "project" or (area.scope == "both" and not fleet and has_project):
        if not has_project:
            raise InterviewError(f"{tail} has no project spelling, yet area {area.key} is "
                                 f"project-scoped")
        return project_path
    spelled = _os_spelling(tail)
    if spelled is None:
        if has_project:
            return project_path
        raise InterviewError(f"{tail} is not a resolved config path")
    return spelled


@cache
def _enable_tail(area_key: str) -> str | None:
    """The area's shortest asked `*.enabled` tail, if it has one"""
    tails = {hit[1] for p in _default_paths() if (hit := area_of(p)) and hit[0] == area_key
             and asked(p) and hit[1].rsplit(".", 1)[-1] == "enabled"}
    return min(tails, key=lambda t: (len(t), t), default=None)


def _resolved(document: dict[str, Any]) -> dict[str, Any]:
    return config_version.resolve(parse_catalog(json.loads(json.dumps(document))))


def _others(draft: Draft, document: dict[str, Any]) -> list[str]:
    return sorted(p["name"] for p in document.get("projects", [])
                  if isinstance(p, dict) and isinstance(p.get("name"), str)
                  and p["name"] != draft.project.name)


def _owns_os(draft: Draft, document: dict[str, Any]) -> bool:
    """Is the project being onboarded the one that runs the OS (`ops._refuse_os_sweep_off`)?"""
    from . import schedule

    pairs = [(p["name"], Path(str(p.get("path") or "."))) for p in document.get("projects", [])
             if isinstance(p, dict) and isinstance(p.get("name"), str)
             and p["name"] != draft.project.name]
    pairs.append((draft.project.name, Path(draft.project.path)))
    try:
        return schedule.os_project(pairs) == draft.project.name
    except (OSError, ValueError):
        return False


def _sweeps_off(values: dict[str, Any]) -> bool:
    return any(not value and (path == key or path.endswith(f".{key}"))
               for path, value in values.items() for key in _SWEEP_KEYS)


def _expand(area: Area, posture: str, name: str, fleet: bool) -> dict[str, Any]:
    if posture not in area.presets:
        raise InterviewError(f"area {area.key} has no posture {posture!r}; it has "
                             f"{', '.join(sorted(area.presets)) or 'none'}")
    return {_spell(area, tail, name, fleet): value
            for tail, value in area.presets[posture].items()}


def _by_key(areas: Iterable[Area]) -> dict[str, Area]:
    return {a.key: a for a in areas}


def _record(draft: Draft, area_key: str, mode: str) -> None:
    draft.answers[:] = [a for a in draft.answers if a.get("area") != area_key]
    draft.answers.append({"area": area_key, "mode": mode})


def _drop_area(draft: Draft, area_key: str) -> None:
    for path in [p for p in draft.values if (area_of(p) or ("",))[0] == area_key]:
        del draft.values[path]


def _commit(draft: Draft, before: Draft, document: dict[str, Any]) -> Draft:
    """Validate, then save; a refused answer leaves the draft as it was"""
    os_keys = [p for p in draft.values if p.startswith("os.") and p not in before.values]
    others = _others(draft, document)
    if os_keys and others and FLEET_WARNED not in draft.answers:
        _restore(draft, before)
        raise InterviewError(
            f"{', '.join(sorted(os_keys))} would change the fleet setting for "
            f"{', '.join(others)} too — surface `fleet_warning` to the user first")
    if _owns_os(draft, document) and _sweeps_off(draft.values):
        _restore(draft, before)
        raise InterviewError(f"{draft.project.name} runs the OS itself — its supervisor "
                             f"sweep must stay on")
    problems = onboard_draft.validate(draft, document)
    if problems:
        _restore(draft, before)
        raise InterviewError("\n".join(problems))
    save(draft)
    return draft


def _restore(draft: Draft, before: Draft) -> None:
    draft.values, draft.answers, draft.reason = before.values, before.answers, before.reason


def match_posture(area: Area, resolved: dict[str, Any], name: str) -> str:
    """The posture whose preset the live values match exactly, else `custom`"""
    for posture in area.presets:
        for fleet in (False, True):
            try:
                spelled = _expand(area, posture, name, fleet)
            except InterviewError:
                continue
            if spelled and all(p in resolved and resolved[p] == v for p, v in spelled.items()):
                return posture
    return CUSTOM


def new_draft(path: str | Path, name: str | None = None,
              current: dict[str, Any] | None = None, *,
              areas: Iterable[Area] = (), document: dict[str, Any] | None = None) -> Draft:
    """A fresh draft, or on a re-run one pre-filled from `ops.config_show(p)["resolved"]`"""
    project_path = Path(path).expanduser().resolve()
    name = name or project_path.name
    draft = Draft(project=DraftProject(name=name, path=str(project_path)))
    if current is not None:
        from .ops import _written_paths

        document = document or {}
        entry = next((p for p in document.get("projects", [])
                      if isinstance(p, dict) and p.get("name") == name), {})
        draft.project.description = str(entry.get("description") or "")
        draft.project.model = str(entry.get("model") or "")
        prefix = f"projects.{name}."
        first_class = {f"{prefix}{k}" for k in ("path", "description", "model")}
        # Only what the document SETS, so an unchanged re-run writes nothing new (§2)
        draft.values = {p: current[p] for p in _written_paths(document, current)
                        if p.startswith(prefix) and p not in first_class}
        for area in areas:
            draft.answers.append({"area": area.key,
                                  "mode": _mode(match_posture(area, current, name))})
    save(draft)
    return draft


def _mode(posture: str) -> str:
    return posture if posture == CUSTOM else f"{PRESET}{posture}"


def _answered(draft: Draft) -> set[str]:
    return {a.get("area") for a in draft.answers}


def fleet_warning(draft: Draft, document: dict[str, Any]) -> str | None:
    """The warning an `os.` value needs first; once returned, `os.` values are allowed"""
    others = _others(draft, document)
    if not others:
        return None
    if FLEET_WARNED not in draft.answers:
        draft.answers.append(dict(FLEET_WARNED))
        save(draft)
    return _warning_text(others)


def _warning_text(others: list[str]) -> str:
    return (f"An os.* setting is the whole fleet's: it also changes {', '.join(others)}. "
            f"Leave it as it is unless you mean every project.")


def questions(draft: Draft, areas: Iterable[Area], *,
              document: dict[str, Any]) -> list[Question]:
    """The unanswered areas in `AREA_KEYS` order, quoting the area and nothing else"""
    by_key = _by_key(areas)
    done = _answered(draft)
    resolved = _resolved(onboard_draft.candidate_document(draft, document))
    others = _others(draft, document)
    owns_os = _owns_os(draft, document)
    name = draft.project.name
    out = []
    for key in AREA_KEYS:
        area = by_key.get(key)
        if area is None or key in done:
            continue
        postures = {}
        for posture in area.presets:
            values = _expand(area, posture, name, False)
            if owns_os and _sweeps_off(values):
                continue  # §2: never OFFER turning the OS project's own sweep off
            postures[posture] = values
        enable = _enable_tail(key)
        current = resolved.get(_spell(area, enable, name, False)) if enable else None
        warn = (_warning_text(others) if others and area.scope == "os" else None)
        out.append(Question(area=key, title=area.title, summary=area.summary,
                            on_costs=area.on_costs, off_costs=area.off_costs,
                            enable=enable, current=current, postures=postures,
                            fleet_warning=warn))
    return out


def answer(draft: Draft, area_key: str, mode: str, *, areas: Iterable[Area],
           document: dict[str, Any], fleet: bool = False) -> Draft:
    """`preset:<posture>` expands into `values`; `defaults` accepts the shipped default"""
    if mode == DEFAULTS:
        return accept_defaults(draft, area_key, document=document)
    if not mode.startswith(PRESET):
        raise InterviewError(f"answer {mode!r} is neither `{DEFAULTS}` nor `{PRESET}<posture>`")
    area = _by_key(areas).get(area_key)
    if area is None:
        raise InterviewError(f"unknown area {area_key!r}")
    before = copy.deepcopy(draft)
    _drop_area(draft, area_key)
    draft.values.update(_expand(area, mode[len(PRESET):], draft.project.name, fleet))
    _record(draft, area_key, mode)
    return _commit(draft, before, document)


def set_value(draft: Draft, path: str, value: Any, *, document: dict[str, Any]) -> Draft:
    """The per-knob escape hatch: one resolved path, and the area becomes `custom`"""
    hit = area_of(path)
    if hit is None:
        raise InterviewError(f"{path} is not a path any behaviour area owns")
    before = copy.deepcopy(draft)
    draft.values[path] = value
    _record(draft, hit[0], CUSTOM)
    return _commit(draft, before, document)


def accept_defaults(draft: Draft, area_key: str, *, document: dict[str, Any]) -> Draft:
    """Answered, and NO values: the document stays silent so the default can still move"""
    before = copy.deepcopy(draft)
    _drop_area(draft, area_key)
    _record(draft, area_key, DEFAULTS)
    return _commit(draft, before, document)


def accept_remaining(draft: Draft, *, document: dict[str, Any]) -> Draft:
    before = copy.deepcopy(draft)
    done = _answered(draft)
    for key in AREA_KEYS:
        if key not in done:
            _record(draft, key, DEFAULTS)
    return _commit(draft, before, document)


def set_reason(draft: Draft, text: str) -> Draft:
    """The user's own sentence: `ops._require_reason` refuses a safety move without it (§0.3)"""
    if not text.strip():
        raise InterviewError("the reason is the user's own words, and it is empty")
    draft.reason = text.strip()
    save(draft)
    return draft


def _wiring_scope(draft: Draft, document: dict[str, Any]) -> str | None:
    # `ops.project_spec` refuses a project not yet in the catalog, so a new one reads the fleet base
    adopted = {p.get("name") for p in document.get("projects", [])}
    return draft.project.name if draft.project.name in adopted else None


def wiring_choices(draft: Draft, *, document: dict[str, Any]) -> dict[str, Any]:
    """The user's real MCP servers, skills and plugins, discovered at runtime (§2)"""
    from . import ops

    return ops.wiring_show(_wiring_scope(draft, document))


def wire(draft: Draft, lever: str, wired: bool, *, document: dict[str, Any]) -> Draft:
    """Wiring is the user's real inventory, so a lever, not a posture (§2)"""
    import dataclasses

    from . import ops, wiring

    live = ops.wiring_config(_wiring_scope(draft, document))
    prefix = f"projects.{draft.project.name}.wiring."
    # Levers already in the draft overlay the live config, so two levers compose
    pending = {p[len(prefix):]: v for p, v in draft.values.items() if p.startswith(prefix)}
    tail, value = wiring.lever_setting(lever, wired, dataclasses.replace(live, **pending))
    return set_value(draft, f"projects.{draft.project.name}.{tail}", value,
                     document=document)


def summary(draft: Draft, document: dict[str, Any], *,
            lines: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """The real resolved diff, annotated; `lines` is §4's per-tail corpus"""
    from . import ops

    before = _resolved(document)
    after = _resolved(onboard_draft.candidate_document(draft, document))
    rows = []
    for change in config_version.diff(before, after):
        hit = area_of(change["path"])
        rows.append({**change, "apply": ops.apply_class(change["path"]),
                     "safety": ops.safety_key(change["path"]),
                     "line": (lines or {}).get(hit[1], "") if hit else ""})
    return rows
