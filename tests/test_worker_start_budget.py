"""Jarvis's share of a worker's starting context stays within 10K tokens (chars/4) (docs/os/architecture/worker-context.md)"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import context, os_map, worker_brief, worker_session
from jarvis.catalog import OsConfig, ProjectSpec, WorkerDefaults
from jarvis.central_store import PINNED_TAG, CentralStore
from jarvis.dispatch import build_worker_prompt
from jarvis.gates import GateConfig

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "src" / "jarvis" / "assets"
STANDING = "S" * 600  # the size of jarvis_os's worker.append_system_prompt
DESCRIPTION = "D" * 3200  # a bug filed by `jarvis bug report`, issue text inlined


def _spec() -> ProjectSpec:
    return ProjectSpec(name="jarvis_os", path=REPO, worker=WorkerDefaults(),
                       gates=GateConfig.parse(True))


def _base(central: CentralStore) -> None:
    """~130 topics and four long pinned rulings, like the live base"""
    for i in range(4):
        central.add_knowledge(f"USER RULING {i}: " + "rule text " * 120,
                              project="jarvis_os", topic="rulings", tags=PINNED_TAG)
    for i in range(260):
        central.add_knowledge(f"ENTRY {i} ABOUT THE STUCK SWEEP AND BUDGET CAPS "
                              + "detail " * 50, project="jarvis_os", topic=f"topic {i % 130}")


def _listing(path: Path) -> int:
    meta, _ = os_map.parse(path.read_text())
    return len(str(meta.get("name") or path.stem)) + len(str(meta.get("description") or "")) + 4


def start_breakdown(central: CentralStore, wo: dict) -> dict[str, int]:
    """Estimated tokens per Jarvis-controlled ingredient of a worker's first request"""
    cfg = OsConfig()
    spec = _spec()
    brief = central.knowledge_brief(
        spec.name, pinned_limit=cfg.knowledge_inject_limit,
        digest_limit=cfg.knowledge_digest_limit, digest_chars=cfg.knowledge_digest_chars,
        title=wo["title"], hint_limit=cfg.knowledge_hint_limit,
        hint_chars=cfg.knowledge_hint_chars, pinned_chars=cfg.knowledge_pinned_chars)
    prompt = build_worker_prompt(wo, spec, brief)
    append = worker_session._append_system_prompt(
        worker_brief.git_briefing("claude-opus-5"), STANDING)
    skills = [*sorted((ASSETS / "skills").glob("*/SKILL.md")),
              *sorted((REPO / ".claude" / "skills").glob("*/SKILL.md"))]
    agents = sorted((ASSETS / "worker-agents").glob("*.md"))
    chars = {
        "worker_prompt": len(prompt) - len(DESCRIPTION),
        "work_order_description": len(DESCRIPTION),
        "append_system_prompt": len(append),
        "claude_md": len((REPO / "CLAUDE.md").read_text()),
        "session_start_context": len(context.session_start_text(True, REPO)),
        "skill_and_agent_listing": sum(_listing(p) for p in skills + agents),
    }
    return {k: v // context.TOKEN_BYTES for k, v in chars.items()}


@pytest.mark.parametrize("title,kind", [
    ("A work order at its budget cap is not stuck: the sweep has no exclusion", "worker"),
    ("Refactor the dispatch module into smaller pieces", "worker"),
    ("Cut a release of Jarvis OS", "worker"),
    ("Diagnose why wo-4091ba42 is not moving", "investigator"),
])
def test_jarvis_share_of_a_worker_start_fits_the_budget(jarvis_home, title, kind):
    central = CentralStore()
    _base(central)
    wo = {"id": "wo-71b2ecad", "title": title, "kind": kind,
          "description": DESCRIPTION}
    parts = start_breakdown(central, wo)
    total = sum(parts.values())
    assert total <= context.START_BUDGET_TOKENS, (
        f"{total} estimated tokens > {context.START_BUDGET_TOKENS}: {parts}")


def test_claude_md_stays_under_five_thousand_tokens():
    size = len((REPO / "CLAUDE.md").read_text())
    assert size // context.TOKEN_BYTES <= 5_000, f"CLAUDE.md is {size} chars"


def test_start_budget_sums_only_what_reaches_the_window():
    rows = [{"name": "worker_prompt", "tokens": 6000},
            {"name": "memory_files", "tokens": 3000},
            {"name": "add_dirs", "tokens": 90_000},
            {"name": "knowledge_index", "tokens": None},
            {"name": "session_start_context", "tokens": 1500}]
    assert context.start_budget(rows) == {"tokens": 10_500, "budget": 10_000,
                                          "over": True}
