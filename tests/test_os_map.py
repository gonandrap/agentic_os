"""docs/os is a conformant OKF bundle and each order gets one fitting playbook"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jarvis import os_map

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "docs" / "os"


def _concepts():
    return [p for p in ROOT.rglob("*.md") if p.name not in ("index.md", "log.md")]


def test_every_concept_has_a_type():
    """OKF v0.2 conformance: every non-reserved file carries a non-empty `type`"""
    assert _concepts()
    for path in _concepts():
        meta, _ = os_map.parse(path.read_text())
        assert meta.get("type"), f"{path} has no OKF type"


def test_every_bundle_link_and_resource_resolves():
    for path in [*_concepts(), ROOT / "index.md"]:
        text = path.read_text()
        for target in re.findall(r"\]\((/[^)\s]+)\)", text):
            assert (ROOT / target.lstrip("/")).is_file(), f"{path}: {target}"
        resource = os_map.parse(text)[0].get("resource")
        if resource and not str(resource).startswith("http"):
            assert (path.parent / resource).resolve().is_file(), f"{path}: {resource}"


def test_the_index_lists_every_concept():
    index = (ROOT / "index.md").read_text()
    for path in _concepts():
        assert f"(/{path.relative_to(ROOT).as_posix()})" in index, path


@pytest.mark.parametrize("wo,expected", [
    ({"kind": "investigator", "title": "Fix the bug"}, "investigation"),
    ({"kind": "analyst", "title": "x"}, "investigation"),
    ({"kind": "planner", "title": "x"}, "feature"),
    ({"kind": "worker", "title": "x", "parent_id": "fo-1"}, "feature"),
    ({"kind": "worker", "title": "Refactor dispatch into modules"}, "refactor"),
    ({"kind": "worker", "title": "Budget cap is not stuck",
      "description": "Fix https://github.com/o/r/issues/1114.\n\nmore"}, "bugfix"),
    ({"kind": "worker", "title": "Cut a release of Jarvis OS"}, "release"),
    ({"kind": "worker", "title": "A release order overtaken mid-CI fails"}, "bugfix"),
    ({"kind": "worker", "title": "Add a dashboard column"}, "general"),
])
def test_an_order_gets_the_playbook_for_its_kind(wo, expected):
    assert os_map.classify(wo, os_map.playbooks(ROOT)).key == expected


def test_the_brief_block_points_at_repo_paths_and_names_the_index():
    block = "\n".join(os_map.brief_block({"kind": "worker", "title": "Refactor x"}, REPO))
    assert block.startswith("# Your map: Refactor")
    assert "`docs/os/architecture/codebase-map.md`" in block
    assert "](/" not in block and "docs/os/index.md" in block


def test_a_project_without_a_bundle_gets_no_block(tmp_path):
    assert os_map.brief_block({"kind": "worker", "title": "x"}, tmp_path) == []
