#!/usr/bin/env python
"""Mutation-test the prefix-drift battery: plant a drift, assert the right file goes red.

    uv run python scripts/mutate_prefix_drift.py

A green battery proves nothing until each of its scenarios has been shown to fail for
the reason it claims to watch (kn-45bae078 item 3). This plants each drift in PRODUCTION
code one at a time, runs both owning test files, restores the tree, and checks the
observed owner against the one declared below. Exits non-zero on any disagreement, so
the table in the pull request is reproducible rather than asserted.

WHY THIS IS A COMMITTED SCRIPT AND NOT A SHELL LOOP (review round 2). The first version
was ad-hoc and decided "red" by grepping stdout for lines starting with `FAILED`. A
mutation that breaks module IMPORT produces `ERROR ... during collection` instead, which
that grep cannot see — so the most broken outcome available was scored as a pass, and a
false row reached the pull request and the knowledge base. Red is `returncode != 0`
here, which is the one reading that cannot miss a way for a run to go wrong.

TEST CALL SITES ARE NEVER EDITED. A mutation must be a change a real commit could make,
and a rename that updates the definition but not its own module's use of it is a
NameError, not a drift — it takes every test down and measures nothing (that was case A
above). `RENAME` therefore edits the definition AND `OS_INVARIANTS` together.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = "evals/test_prefix_drift.py"
UNIT = "tests/test_cache_health.py"

#: (name, file, [(old, new), ...], {files that must go red})
MUTATIONS: list[tuple[str, str, list[tuple[str, str]], set[str]]] = [
    ("the composition order in _append_system_prompt is reversed",
     "src/jarvis/worker_session.py",
     [('            git_briefing(model),\n'
       '            wo.get("append_system_prompt") or project.worker.append_system_prompt),',
       '            wo.get("append_system_prompt") or project.worker.append_system_prompt,\n'
       '            git_briefing(model)),')],
     {EVAL}),
    ("per-order text is placed INSIDE the shared head",
     "src/jarvis/worker_session.py",
     [('            git_briefing(model),\n',
       '            git_briefing(model) + f"\\n\\nWork order {wo[\'id\']}.",\n')],
     {EVAL}),
    ("a static block is appended AFTER the variable region",
     "src/jarvis/worker_session.py",
     [('            wo.get("append_system_prompt") or project.worker.append_system_prompt),',
       '            wo.get("append_system_prompt") or project.worker.append_system_prompt,\n'
       '            "# Reminder\\n\\nRun the tests."),')],
     {EVAL}),
    ("the working directory reaches git_briefing",
     "src/jarvis/worker_brief.py",
     [('    return "\\n".join([\n        "# Git",',
       '    import os\n    return "\\n".join([\n        f"# Git ({os.getcwd()})",')],
     {EVAL}),
    ("the CLI's git-status snapshot is switched back on",
     "src/jarvis/dispatch.py",
     [('    settings["includeGitInstructions"] = False',
       '    settings["includeGitInstructions"] = True')],
     {EVAL}),
    ("the 5-minute cache flag is dropped from the worker settings",
     "src/jarvis/dispatch.py",
     [("        **claude_cli.PROMPT_CACHE_5M_ENV,\n", "")],
     {EVAL}),
    ("a generated skill file varies between rebuilds",
     "src/jarvis/bootstrap.py",
     [("    shutil.copytree(src, dest)\n    return root",
       "    shutil.copytree(src, dest)\n    import time\n"
       "    (dest / 'stamp.md').write_text(str(time.time_ns()))\n    return root")],
     {EVAL}),
    ("the invariant stops claiming to be the authoritative measurement",
     "src/jarvis/invariants.py",
     [("    THIS IS THE AUTHORITATIVE MEASUREMENT OF PREFIX STABILITY IN THIS TREE",
       "    This is one measurement of prefix stability in this tree")],
     {UNIT}),
    ("the battery's name is removed from the invariant's DOCSTRING",
     "src/jarvis/invariants.py",
     [(" (`evals/test_prefix_drift.py`)", "")],
     {UNIT}),
    ("the battery's name is removed from the invariant's VIOLATION DETAIL",
     "src/jarvis/invariants.py",
     [('f"`jarvis inspect <wo-id>` labels every re-write of one order by cause, and "\n'
       '            f"`pytest evals/test_prefix_drift.py` says whether the part of the prompt "\n'
       '            f"JARVIS renders still has its shared head where the code says — green there "\n'
       '            f"with this red points at the CLI or an MCP server rather than at this tree. "',
       'f"`jarvis inspect <wo-id>` labels every re-write of one order by cause. "')],
     {EVAL}),
    ("the invariant is renamed, definition and registration together",
     "src/jarvis/invariants.py",
     [("def check_prefix_stable()", "def check_prefix_moved()"),
      ("\n    check_prefix_stable,\n", "\n    check_prefix_moved,\n")],
     {EVAL, UNIT}),
]


def _red(target: str) -> bool:
    """Did this file go red? Any non-zero exit — a failure, a collection ERROR, a crash."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", target, "-q", "--no-header", "-p", "no:randomly"],
        cwd=ROOT, capture_output=True, text=True).returncode != 0


def _drop_pycache() -> None:
    """.pyc invalidation keys on source mtime+size, so a same-size edit restored within
    the same second loads STALE bytecode and the next mutation looks spuriously
    undetected (kn-45bae078)."""
    for pc in (ROOT / "src").rglob("__pycache__"):
        shutil.rmtree(pc, ignore_errors=True)


def main() -> int:
    bad = 0
    for name, rel, pairs, expected in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        text = original
        for old, new in pairs:
            if old not in text:
                print(f"SKEW  {name}\n      pattern gone from {rel} — the mutation no "
                      f"longer describes the code")
                bad += 1
                text = None
                break
            text = text.replace(old, new, 1)
        if text is None:
            continue
        path.write_text(text)
        _drop_pycache()
        try:
            observed = {t for t in (EVAL, UNIT) if _red(t)}
        finally:
            path.write_text(original)
            _drop_pycache()

        ok = observed == expected
        bad += not ok
        print(f"{'ok   ' if ok else 'WRONG'} {name}\n"
              f"      red: {', '.join(sorted(observed)) or 'NOTHING — the mutation is undetected'}"
              + ("" if ok else f"\n      expected: {', '.join(sorted(expected))}"))
    print(f"\n{len(MUTATIONS) - bad}/{len(MUTATIONS)} mutations land where declared")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
