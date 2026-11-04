"""A project's value beats the fleet's: docs/superpowers/specs/2026-11-04-project-config-takes-precedence.md"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import jarvis
from jarvis import ops
from jarvis.catalog import ProjectSpec, parse_catalog
from jarvis.daemon import Daemon

#: Fleet reads of a block a project can override, and why each has no project to ask (spec's table)
FLEET_READS = {
    # `project=None` branches and not-catalogued fallbacks of the scoped accessors
    ("ops.py", "concision"), ("ops.py", "validation"), ("ops.py", "rules"),
    ("ops.py", "evolution"), ("ops.py", "wiring"), ("ops.py", "inspect"),
    ("ops.py", "ui_health"), ("ops.py", "navigation"), ("ops.py", "messaging"),
    ("ops.py", "pull_requests"), ("ops.py", "supervisor"), ("fleetcost.py", "cost"),
    ("steward.py", "evolution"),  # `evolution_for`'s fallback, and the digest's fleet headline
    ("cli.py", "evolution"),  # `steward ledger`'s fleet headline; caps per project follow it
    # Refused on a project (`*_FLEET_ONLY*`, `_parse_ui_health`)
    ("daemon.py", "cost"), ("daemon.py", "ui_health"), ("daemon.py", "fleet_health"),
    ("invariants.py", "fleet_health"),
}


def _fleet_reads() -> set[tuple[str, str]]:
    blocks = {f.name for f in dataclasses.fields(ProjectSpec)}
    found = set()
    for path in Path(jarvis.__file__).parent.rglob("*.py"):
        if path.name == "catalog.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Attribute) and node.attr in blocks
                    and isinstance(node.value, ast.Attribute) and node.value.attr == "os"):
                found.add((path.name, node.attr))
    return found


def test_no_new_code_reads_a_per_project_block_off_the_fleet():
    """`catalog.os.<block>` silently ignores a project's value: read the project's, or refuse it there"""
    assert _fleet_reads() <= FLEET_READS, "read the project's block, or refuse the key on a project"


def test_the_stuck_cooldown_is_the_projects_own(monkeypatch):
    cat = parse_catalog({"os": {"fleet_health": {"cooldown_minutes": 1}},
                         "projects": [{"name": "a", "path": "/tmp/a",
                                       "fleet_health": {"cooldown_minutes": 100}}]})
    monkeypatch.setattr(ops, "last_stuck_investigation", lambda *a: {
        "updated_at": 0.0, ops.STUCK_FINGERPRINT_KEY: "before"})

    # 10 minutes on: past the fleet's cooldown, inside the project's
    assert Daemon._stuck_cooled(SimpleNamespace(catalog=cat), "a", "wo-1", "now", 600.0)
