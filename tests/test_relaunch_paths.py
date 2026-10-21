"""Every relaunch path asks the shared compaction decision — discovered, not listed (docs/superpowers/specs/2026-09-29-one-compaction-decision-on-every-relaunch.md §3.7)"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "jarvis"

#: dispatch.py is here although the brief named three files: it holds the only call site of...
MODULES = ("worker_session", "daemon", "ops", "dispatch")

#: Every way a turn is put in front of a worker
LAUNCHERS = {"start", "send", "retry", "_launch", "compact", "compact_then_resume"}

DECISION = "compact_before_relaunch"

EXEMPT: dict[str, str] = {
    "worker_session.start": "the transport itself; the decision is above it",
    "worker_session.send": "the transport itself; the decision is above it",
    "worker_session.retry": "the transport itself; the decision is above it",
    "worker_session.compact": "the transport itself; the decision is above it",
    "worker_session._compact_or_withdraw": "the withdrawal wrapper around the action",
    "worker_session.compact_before_relaunch": "the decision itself",
}


def _qualname(module: str, stack: list[ast.AST]) -> str | None:
    """`module.Class.func` / `module.func` for the innermost function enclosing a call"""
    names: list[str] = []
    for node in stack:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
    if not names:
        return None
    return ".".join([module, *names])


def _called_names(node: ast.AST) -> set[str]:
    """Every name called anywhere inside this node — `x(...)`, `self.x(...)`, `m.x(...)`"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Attribute):
            out.add(sub.func.attr)
        elif isinstance(sub.func, ast.Name):
            out.add(sub.func.id)
    return out


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Every function in a module by its bare name"""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, node)
    return out


def _sites() -> list[tuple[str, int, str, str]]:
    """(module, line, enclosing qualified name, launcher) for every launch in the tree"""
    found: list[tuple[str, int, str, str]] = []
    for module in MODULES:
        tree = ast.parse((SRC / f"{module}.py").read_text())
        stack: list[ast.AST] = []

        def visit(node: ast.AST, module: str = module, tree: ast.AST = tree) -> None:
            stack.append(node)
            if isinstance(node, ast.Call):
                launcher = None
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr in LAUNCHERS
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "worker_session"):
                    launcher = node.func.attr
                elif (module == "worker_session" and isinstance(node.func, ast.Name)
                        and node.func.id in LAUNCHERS):
                    launcher = node.func.id
                if launcher is not None:
                    qual = _qualname(module, stack)
                    if qual is not None:
                        found.append((module, node.lineno, qual, launcher))
            for child in ast.iter_child_nodes(node):
                visit(child)
            stack.pop()

        visit(tree)
    return found


def _reaches_decision(module: str, qual: str) -> bool:
    tree = ast.parse((SRC / f"{module}.py").read_text())
    functions = _functions(tree)
    enclosing = functions.get(qual.split(".")[-1])
    if enclosing is None:
        return False
    called = _called_names(enclosing)
    if DECISION in called:
        return True
    # ONE LEXICAL HOP, and it exists for exactly one pair: `Daemon._deliver` asks...
    return any(DECISION in _called_names(functions[name])
               for name in called if name in functions)


def test_every_relaunch_path_reaches_the_shared_compaction_decision():
    sites = _sites()
    assert sites, "the AST walk found no launch sites at all — a rename made it vacuous"
    bad = [f"{module}.py:{line} {qual} calls worker_session.{launcher}"
           for module, line, qual, launcher in sites
           if qual not in EXEMPT and not _reaches_decision(module, qual)]
    assert not bad, (
        "these relaunch paths launch a turn without asking "
        f"`worker_session.{DECISION}`:\n  " + "\n  ".join(bad)
        + f"\n\nFix one of two ways: call {DECISION} before launching (and do not "
          "launch when it returns a turn), or add the qualified name to EXEMPT in this "
          "file with a one-line reason.")


def test_the_exempt_list_is_only_the_transport():
    """A registration that names a module outside `worker_session` is the decision leaking back out to the..."""
    assert all(name.startswith("worker_session.") for name in EXEMPT)
    assert all(reason.strip() for reason in EXEMPT.values())


def test_no_exemption_is_stale():
    """A key naming a function that no longer exists widens the exemption invisibly: nothing else..."""
    live = {qual for _, _, qual, _ in _sites()}
    dead = sorted(name for name in EXEMPT if name not in live)
    assert not dead, (
        "these EXEMPT entries name no launch site any more:\n  " + "\n  ".join(dead)
        + "\n\nDelete them, or point them at the name the code moved to.")


def test_the_enclosing_names_resolved_here_do_not_collide():
    """`_functions` keys on the BARE name"""
    wanted = {(module, qual.split(".")[-1]) for module, _, qual, _ in _sites()}
    clashes = sorted(
        f"{module}.{name}"
        for module, name in wanted
        if sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == name
               for node in ast.walk(ast.parse((SRC / f"{module}.py").read_text()))) > 1)
    assert not clashes, (
        "same-named bodies in one module, so the bare-name lookup is now ambiguous:\n  "
        + "\n  ".join(clashes)
        + "\n\nKey `_functions` by `Class.name`, falling back to the bare name, before "
          "trusting what `_reaches_decision` says about these.")
