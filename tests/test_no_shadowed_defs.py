"""A module-level def/class defined twice silently shadows the first (issue 1502)."""
import ast
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "jarvis"


def _shadowed(tree: ast.Module) -> list[str]:
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    names = Counter(n.name for n in tree.body if isinstance(n, kinds))
    return sorted(name for name, count in names.items() if count > 1)


def test_no_module_defines_a_top_level_function_or_class_twice():
    found = {str(p.relative_to(SRC)): dup for p in sorted(SRC.rglob("*.py"))
             if (dup := _shadowed(ast.parse(p.read_text(encoding="utf-8"))))}
    assert found == {}


def test_the_check_catches_a_redefinition():
    assert _shadowed(ast.parse("def f(a): ...\nclass C: ...\ndef f(b): ...\n")) == ["f"]
