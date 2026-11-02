"""No module defines the same top-level name twice: the second silently wins"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "jarvis"

_DEF = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _overload(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    """`@overload` stubs are meant to repeat a name"""
    names = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
             for d in node.decorator_list}
    return "overload" in names


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_defines_a_top_level_name_twice(path: Path) -> None:
    body = ast.parse(path.read_text()).body
    names = Counter(n.name for n in body if isinstance(n, _DEF) and not _overload(n))
    assert [n for n, count in names.items() if count > 1] == []
