"""A feature order's spec: its sections, its agent profile, and where both are put (docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md)"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db, sections

if TYPE_CHECKING:  # pragma: no cover
    from .project_store import ProjectStore

log = logging.getLogger(__name__)

#: The appendix every spec must carry
AGENT_PROFILE = "Agent profile"

#: Shortest agent profile that is a profile rather than a placeholder
MIN_PROFILE_CHARS = 120

#: Filesystem-safe slug for the generated agent's `name:`
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def feature_dir(project_path: Path, fo_id: str) -> Path:
    """Where everything materialised for one feature lives"""
    return project_path / ".jarvis" / "features" / fo_id


def agent_root(project_path: Path, fo_id: str) -> Path:
    """The directory handed to `--add-dir` so `--add-dir/.claude/agents/` is found"""
    return feature_dir(project_path, fo_id) / "agent"


def agent_name(fo_id: str) -> str:
    return _SLUG_RE.sub("-", fo_id.strip().lower()).strip("-") or "jarvis-feature"


def agent_profile(doc_text: str) -> str:
    """The spec's `Agent profile` appendix, or "" when it has none"""
    return (sections.extract_section(doc_text or "", AGENT_PROFILE) or "").strip()


def profile_problems(doc_text: str) -> list[str]:
    """Why this spec cannot produce an agent type"""
    profile = agent_profile(doc_text)
    if not profile:
        return [
            f"the design document has no `{AGENT_PROFILE}` section. Every feature order "
            f"builds its own agent type from that appendix, and every child work order "
            f"runs as it — add a final section describing the role, the expertise and "
            f"the standing rules for the workers that will implement this feature"
        ]
    if len(profile) < MIN_PROFILE_CHARS:
        return [
            f"the `{AGENT_PROFILE}` section is {len(profile)} characters, under the "
            f"{MIN_PROFILE_CHARS} needed to brief an agent. It becomes the system prompt "
            f"of every child worker — say what the role is, what it knows, and how it is "
            f"expected to work"
        ]
    return []


def render_agent(fo_id: str, title: str, profile: str) -> str:
    """The agent definition file's bytes"""
    name = agent_name(fo_id)
    desc = (title or fo_id).replace("\n", " ").strip()[:180]
    body = profile.strip()
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: The implementing agent for Jarvis feature order {fo_id} — {desc}\n"
        f"---\n\n"
        f"{body}\n"
    )


def install_agent(project_path: Path, fo_id: str, title: str,
                  doc_text: str) -> str | None:
    """Write the feature's agent definition; return its name, or None"""
    profile = agent_profile(doc_text)
    if not profile:
        return None
    name = agent_name(fo_id)
    target = agent_root(project_path, fo_id) / ".claude" / "agents" / f"{name}.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_agent(fo_id, title, profile))
    except OSError as e:
        # A persona is never worth a work order: §3, "Degradation is silent and total"
        log.warning("could not write the agent type for %s: %s", fo_id, e)
        return None
    return name


def remove_agent(project_path: Path, fo_id: str) -> bool:
    """Drop the feature's agent type"""
    root = agent_root(project_path, fo_id)
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


def plan_of(store: ProjectStore, fo_id: str) -> dict[str, Any]:
    """The feature's stored plan, or `{}` for anything missing or unplanned"""
    try:
        fo = store.get_feature_order(fo_id)
    except KeyError:
        return {}
    return db.from_json(fo.get("plan"), {}) or {}


def _plan_section_for_title(plan: dict[str, Any], title: str) -> str:
    """The section the plan's item of this TITLE names, or "" when it does not resolve"""
    if not title:
        return ""
    found = [str(c.get("spec_section") or "").strip()
             for c in (plan.get("children") or [])
             if isinstance(c, dict) and str(c.get("title") or "") == title]
    return found[0] if len(found) == 1 else ""


def spec_of(store: ProjectStore, wo: dict[str, Any]) -> dict[str, str] | None:
    """This work order's spec, section included, or None (docs/superpowers/specs/2026-10-13-an-assumption-review-payload-carries-its-spec-section.md)"""
    if not wo.get("parent_id") or wo.get("kind") in ("planner", "manager"):
        return None
    plan = plan_of(store, wo["parent_id"])
    content = str(plan.get("design_doc_content") or "")
    repo_path = str(plan.get("design_doc") or "")
    if not (content and repo_path):
        return None
    which = str(wo.get("spec_section") or "").strip()
    if not which:
        which = _plan_section_for_title(plan, str(wo.get("title") or ""))
        if which:
            store.adopt_spec_section(str(wo["id"]), which)
    text = sections.extract_section(content, which) if which else None
    return {"repo_path": repo_path, "content": content,
            "section": which, "section_text": (text or "").strip()}


def spec_link(store: ProjectStore, wo: dict[str, Any]) -> dict[str, str] | None:
    """Where this order's spec can be READ (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""
    from .ui import markdown  # stdlib-only module; no FastAPI, no Jinja

    fo_id = str(wo.get("parent_id") or "")
    if not fo_id:
        return None
    plan = plan_of(store, fo_id)
    content = str(plan.get("design_doc_content") or "")
    repo_path = str(plan.get("design_doc") or "")
    if not (content and repo_path):
        return None
    which = str(wo.get("spec_section") or "").strip()
    anchor = markdown.anchor_for(content, which) if which else None
    return {"fo_id": fo_id, "repo_path": repo_path,
            "source": str(plan.get("design_doc_source") or ""),
            "anchor": anchor or ""}


def materialize(project_path: Path, fo_id: str, wo_id: str,
                spec: dict[str, str]) -> dict[str, str]:
    """Write the spec and this child's section under `.jarvis/`; return the paths"""
    out: dict[str, str] = {"repo_path": spec["repo_path"]}
    root = feature_dir(project_path, fo_id)
    try:
        doc = root / Path(spec["repo_path"]).name
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(spec["content"])
        out["path"] = str(doc)
        if spec.get("section_text"):
            section = root / "sections" / f"{wo_id}.md"
            section.parent.mkdir(parents=True, exist_ok=True)
            section.write_text(spec["section_text"] + "\n")
            out["section"] = spec["section"]
            out["section_path"] = str(section)
    except OSError as e:
        log.warning("could not materialise the spec for %s: %s", wo_id, e)
    return out
