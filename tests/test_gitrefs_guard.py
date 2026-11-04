"""The guard that stops the NEXT git reader hand-rolling its own refresh (io-c1bbfff4)"""

import ast
from pathlib import Path

import jarvis

SRC = Path(jarvis.__file__).parent
OWNER = "gitrefs.py"

#: A helper that ends up spawning `git <args>`, so `"fetch"` among its literals is a fetch
GIT_RUNNERS = {"git", "_git", "_run", "run", "attempt", "_git_bytes", "read"}


def _literals(call: ast.Call) -> set[str]:
    out = set()
    for arg in call.args:
        nodes = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
        out |= {n.value for n in nodes
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return out


def _callee(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _fetch_spawns(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and "fetch" in (lits := _literals(node))
            and ("git" in lits or _callee(node) in GIT_RUNNERS)]


def test_only_gitrefs_spawns_git_fetch():
    offenders = {f"{path.relative_to(SRC)}:{line}"
                 for path in sorted(SRC.rglob("*.py")) if path.name != OWNER
                 for line in _fetch_spawns(path)}
    assert not offenders, (
        f"git fetch outside {OWNER}: {sorted(offenders)} — refreshing a clone belongs to "
        "jarvis.gitrefs, which caches it per repo and reports failure to its caller")


def test_the_guard_sees_a_fetch_a_new_reader_would_write(tmp_path):
    sneaky = tmp_path / "newreader.py"
    sneaky.write_text('import subprocess\n'
                      'def f(repo):\n'
                      '    subprocess.run(["git", "-C", str(repo), "fetch", "origin"])\n'
                      '    _git(repo, "fetch", "--tags", "origin")\n')
    assert _fetch_spawns(sneaky) == [3, 4]


def test_the_guard_passes_over_the_word_fetch_that_spawns_nothing(tmp_path):
    innocent = tmp_path / "innocent.py"
    innocent.write_text('PROOF_FETCH = "fetch"\n'
                        'REASONS = {"fetch": "no local answer"}\n'
                        'def f(mp, mod):\n'
                        '    mp.setattr(mod, "fetch", lambda *a: True)\n')
    assert _fetch_spawns(innocent) == []
