"""A feature order's spec: its sections, its agent profile, and where both are put.

The spec is the feature's one artifact — see
docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md. This module owns
everything the OS does WITH that document once a planner has written it: cutting out the
section a child implements, cutting out the `Agent profile` appendix, and materialising
each where a worker's Claude session can reach it.

Split from `sections.py` on purpose. That module is generic markdown surgery with no idea
what a feature order is; this one knows about plans, work orders and `.jarvis/`, and knows
nothing about how a heading is found.

**Everything here degrades to None or a no-op.** A feature with no spec, a spec with no
agent profile, a `.jarvis/` that will not take a write — none of those may stop a work
order dispatching (§3 of the spec).
"""

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

#: The appendix every spec must carry. Matched as a heading substring, case-insensitively,
#: by `sections.extract_section` — so "## Appendix: Agent profile" and "## Agent Profile"
#: both resolve, and the planner is not made to reproduce a heading byte for byte.
AGENT_PROFILE = "Agent profile"

#: Shortest agent profile that is a profile rather than a placeholder. A heading with one
#: line under it would produce an agent definition whose whole persona is its own title.
MIN_PROFILE_CHARS = 120

#: Filesystem-safe slug for the generated agent's `name:`. Feature-order ids are already
#: `fo-<hex>`, so this only ever has work to do if id minting changes.
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def feature_dir(project_path: Path, fo_id: str) -> Path:
    """Where everything materialised for one feature lives.

    Under the project's gitignored `.jarvis/` tree rather than in any worktree: a child
    branches from the default branch, and the spec is on the PLANNER's unmerged branch.
    """
    return project_path / ".jarvis" / "features" / fo_id


def agent_root(project_path: Path, fo_id: str) -> Path:
    """The directory handed to `--add-dir` so `--add-dir/.claude/agents/` is found."""
    return feature_dir(project_path, fo_id) / "agent"


def agent_name(fo_id: str) -> str:
    return _SLUG_RE.sub("-", fo_id.strip().lower()).strip("-") or "jarvis-feature"


def agent_profile(doc_text: str) -> str:
    """The spec's `Agent profile` appendix, or "" when it has none."""
    return (sections.extract_section(doc_text or "", AGENT_PROFILE) or "").strip()


def profile_problems(doc_text: str) -> list[str]:
    """Why this spec cannot produce an agent type. Empty when it can.

    Separate from `agent_profile` because the two callers want different things: the
    plan validator wants the reason to hand back to the planner, and dispatch wants the
    text or nothing.
    """
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
    """The agent definition file's bytes.

    NO `tools:` key, and that is the decision rather than the omission: kn-44fb3e42
    established that `tools:` is enforced by the CLI, and a child worker needs every tool
    it would otherwise have. See §3 of the design document.
    """
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
    """Write the feature's agent definition; return its name, or None.

    Idempotent — the same bytes are rewritten on every dispatch, so a definition someone
    deleted heals itself and a feature released before this existed acquires one on its
    next dispatch (§3, "Lifecycle").
    """
    profile = agent_profile(doc_text)
    if not profile:
        return None
    name = agent_name(fo_id)
    target = agent_root(project_path, fo_id) / ".claude" / "agents" / f"{name}.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_agent(fo_id, title, profile))
    except OSError as e:
        # A persona is never worth a work order: §3, "Degradation is silent and total".
        log.warning("could not write the agent type for %s: %s", fo_id, e)
        return None
    return name


def remove_agent(project_path: Path, fo_id: str) -> bool:
    """Drop the feature's agent type. True when there was one to drop.

    Called when the feature order settles. The spec snapshot stays in the plan, so
    `jarvis fo agent <fo-id>` can rebuild it.
    """
    root = agent_root(project_path, fo_id)
    if not root.exists():
        return False
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()


def plan_of(store: ProjectStore, fo_id: str) -> dict[str, Any]:
    """The feature's stored plan, or `{}` for anything missing or unplanned."""
    try:
        fo = store.get_feature_order(fo_id)
    except KeyError:
        return {}
    return db.from_json(fo.get("plan"), {}) or {}


def spec_of(store: ProjectStore, wo: dict[str, Any]) -> dict[str, str] | None:
    """This work order's spec, section included, or None.

    The single lookup behind three readers — the worker's prompt, the materialised
    section file and the validation panel's packet — so none of them can disagree about
    which section a child was given. Returns `repo_path`, `content` (the whole spec),
    `section` (its heading name or number, as the plan wrote it) and `section_text` (the
    extracted section, "" when it does not resolve).

    A planner or a manager has no section of its own: both own the whole feature.
    """
    if not wo.get("parent_id") or wo.get("kind") in ("planner", "manager"):
        return None
    plan = plan_of(store, wo["parent_id"])
    content = str(plan.get("design_doc_content") or "")
    repo_path = str(plan.get("design_doc") or "")
    if not (content and repo_path):
        return None
    which = str(wo.get("spec_section") or "").strip()
    text = sections.extract_section(content, which) if which else None
    return {"repo_path": repo_path, "content": content,
            "section": which, "section_text": (text or "").strip()}


def materialize(project_path: Path, fo_id: str, wo_id: str,
                spec: dict[str, str]) -> dict[str, str]:
    """Write the spec and this child's section under `.jarvis/`; return the paths.

    Two files, and the ORDER they are named to the worker is the token saving: the
    section is what the child is meant to read, and the whole spec is the wider context
    it reaches for only when the section is not enough (§4).

    Keys present only when the corresponding write succeeded, so a caller that renders
    `spec.get("section_path")` degrades to naming the document alone.
    """
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
