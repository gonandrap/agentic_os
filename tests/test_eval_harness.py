"""The persona evals must stay hermetic"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_jarvis_judgment.py"


@pytest.fixture(scope="module")
def eval_module():
    spec = importlib.util.spec_from_file_location("_jarvis_judgment_under_test", EVAL_PATH)
    assert spec and spec.loader, f"cannot load {EVAL_PATH}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eval_file_exists() -> None:
    assert EVAL_PATH.is_file()


def _headless_kwargs(fn) -> dict[str, ast.expr]:
    """Keyword arguments of the `run_headless(...)` call inside `fn`"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "run_headless":
            return {kw.arg: kw.value for kw in node.keywords if kw.arg}
    raise AssertionError(f"{fn.__name__}() no longer calls run_headless")


def test_subject_is_stripped_of_tools(eval_module) -> None:
    """`tools=""` — not --allowedTools, which governs permission rather than availability"""
    kwargs = _headless_kwargs(eval_module.ask)
    tools = kwargs.get("tools")
    assert isinstance(tools, ast.Constant) and tools.value == "", (
        'evals/llm ask() must pass tools="" — a tooled subject inspects the real OS '
        "and answers about live state instead of routing"
    )


def test_subject_runs_outside_the_repo(eval_module) -> None:
    """Otherwise the repo's own CLAUDE.md loads on top of the persona being graded"""
    cwd = _headless_kwargs(eval_module.ask).get("cwd")
    assert cwd is not None, "ask() must pin the subject's cwd, not inherit pytest's"
    assert hasattr(eval_module, "neutral_cwd"), "the neutral_cwd fixture went missing"


def test_instruction_does_not_pre_answer_the_status_scenario(eval_module) -> None:
    """The instruction suppresses the *reflexive* pulse check, not a real status ask"""
    instruction = eval_module.INSTRUCTION
    assert "already ran" not in instruction, "the instruction pre-answers the pulse scenario"
    assert "unless the user's own message is itself asking" in instruction, (
        "the instruction must carve out a genuine status request"
    )


def test_every_routing_scenario_is_reachable(eval_module) -> None:
    """A scenario whose expected command the instruction forbids can never pass"""
    names = {name for name, _, _ in eval_module.ROUTING}
    assert "pulse" in names
    pulse = next(r for r in eval_module.ROUTING if r[0] == "pulse")
    assert pulse[2] == ["jarvis status"]


# An eval that spawns `claude` itself re-implements the isolation `claude_cli` owns (docs/superpowers/specs/2026-09-25-a-headless-call-that-starts-from-nothing.md)
SELF_SPAWN_ALLOWED: dict[str, str] = {}

_SPAWNERS = {("subprocess", "run"), ("subprocess", "Popen")}


def _spawns_claude(tree: ast.AST) -> list[str]:
    """The spawn calls in `tree` whose argv[0] is the `claude` binary"""
    lists: dict[str, ast.List] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    lists[target.id] = node.value

    def is_claude(argv: ast.expr) -> bool:
        if isinstance(argv, ast.Name):
            argv = lists.get(argv.id, argv)
        if isinstance(argv, ast.BinOp) and isinstance(argv.op, ast.Add):
            argv = argv.left
        if not isinstance(argv, ast.List) or not argv.elts:
            return False
        first = argv.elts[0]
        if isinstance(first, ast.Constant):
            return Path(str(first.value)).name == "claude"
        return isinstance(first, ast.Call) and getattr(
            first.func, "attr", getattr(first.func, "id", "")) == "claude_bin"

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if not isinstance(func.value, ast.Name):
            continue
        name = ""
        if (func.value.id, func.attr) in _SPAWNERS:
            name = f"{func.value.id}.{func.attr}"
        elif func.value.id == "os" and func.attr.startswith("exec"):
            name = f"os.{func.attr}"
        if name and node.args and is_claude(node.args[0]):
            found.append(f"{name} at line {node.lineno}")
    return found


@pytest.mark.parametrize("path", sorted((REPO_ROOT / "evals" / "llm").glob("*.py")),
                         ids=lambda p: p.name)
def test_no_eval_spawns_claude_itself(path: Path) -> None:
    """The one transport is `claude_cli.run_headless[_result]`, which carries the minimal-context flags"""
    if path.name in SELF_SPAWN_ALLOWED:
        pytest.skip(SELF_SPAWN_ALLOWED[path.name])
    spawns = _spawns_claude(ast.parse(path.read_text()))
    assert not spawns, (
        f"{path.name} spawns claude directly ({', '.join(spawns)}); "
        "call claude_cli.run_headless instead"
    )
