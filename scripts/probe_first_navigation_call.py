"""What makes a dispatched worker's FIRST code-navigation call a symbol call?

THE MEASUREMENT, and it inverts the obvious reading: DEFERRAL is the cheap path, not
presence. With the Serena read tools DEFERRED behind `ToolSearch` (the vendor default,
`worker.tool_search` `on`/`cli`) the first code-navigation call was a symbol call 7/7.
With the same tools PRESENT in the tool list with full schemas (`ENABLE_TOOL_SEARCH=false`,
`worker.tool_search=off`) it was a symbol call 1/10 — and the failing first call was the
same string every single time:

    grep -rn "total_for" --include=*.py .

Mechanism: the brief's `ToolSearch select:` line is an ACTION the worker executes, not an
exhortation it weighs, and having paid a call to load `find_symbol` it then uses it.
Presence deletes that step, and the grep prior wins. wo-ab5d81db; addendum under section 4
of docs/specs/2026-10-02-serena-the-cheap-path.md.

Runs the eval's own harness (imported from `evals/llm/test_navigation_judgment.py`, never
retyped) with the BRIEF and the ENV varied independently — the thing the eval itself
cannot do, because each of its arms briefs with the setting it spawns with.

NOT A TEST. Every run spawns a real `claude` turn and bills tokens; `JARVIS_EVALS_LLM=1`
is set by this script because the eval module is gated on it.

    uv run python scripts/probe_first_navigation_call.py v1-faithful --runs 10
    uv run python scripts/probe_first_navigation_call.py --list

One JSON line per run on stdout: the first navigation call, PASS/FAIL, and the whole tool
log in order.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))
os.environ["JARVIS_EVALS_LLM"] = "1"

spec = importlib.util.spec_from_file_location(
    "nav_eval", ROOT / "evals" / "llm" / "test_navigation_judgment.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)

from jarvis import worker_brief  # noqa: E402

#: A mandated FIRST call, substituted for `worker_brief.navigation_core` by the `v2-ritual`
#: variant: the strongest wording tried, to test whether prose alone can beat the grep
#: prior when the tools are present. It cannot.
RITUAL = [
    "# Finding code: your symbol tools are IN your tool list",
    "They are listed with full schemas — `find_symbol`, `find_referencing_symbols`, "
    "`get_symbols_overview` and `activate_project` are callable directly.",
    "YOUR FIRST CODE-NAVIGATION CALL IS `get_symbols_overview` OR `find_symbol`, never "
    "a shell command. Make it before any `grep`/`cat`/`sed -n` at a source path: those "
    "answer text questions, never symbol ones.",
    "- `find_referencing_symbols` for callers — grep has no equivalent; "
    "`get_symbols_overview` before opening a file whole; `find_symbol` instead of "
    "`grep -rn \"def foo\"`.",
    "- `activate_project` on the repo root if a call says no project is active.",
]

#: variant -> (brief tool_search, env tool_search, py_nav_hook, patch navigation_core)
VARIANTS = {
    # what the eval fixture built in its tool-search arm BEFORE wo-ab5d81db: deferred
    # wording, tools present. The mismatch was a harness defect, so it is a control and
    # not a candidate.
    "v0-mismatched": ("cli", "off", "on", False),
    # the spawn the fleet would actually run with worker.tool_search=off
    "v1-faithful": ("off", "off", "on", False),
    # faithful wording plus a mandated FIRST call
    "v2-ritual": ("off", "off", "on", True),
    # the arm that PASSES today: deferral on, with the hook live
    "v3-deferred-hook": ("cli", "cli", "on", False),
    # deferral on, hook off: the fleet's shipped default, the passing no-steer arm
    "v4-deferred": ("cli", "cli", "off", False),
    # tools present, hook OFF: is the hook doing anything to first-call order?
    "v5-faithful-nohook": ("off", "off", "off", False),
    # deferral PINNED ON by Jarvis (ENABLE_TOOL_SEARCH=true) rather than left to the
    # vendor default, with the hook live: the candidate shipped config.
    "v6-deferral-pinned": ("on", "on", "on", False),
}

QUESTION = ("Where is `total_for` defined in this repository, and which functions call "
            "it? Do not change any files; just answer.")


def make_repo() -> Path:
    """The eval's scratch repo, built from the eval's own fixtures.

    Outside this checkout deliberately, for the reason the eval's `repo` fixture gives:
    run from inside Jarvis, the subject would read this repo's CLAUDE.md — which itself
    says to prefer Serena — and the probe would be measuring that file.
    """
    root = Path(tempfile.mkdtemp(prefix="navprobe-"))
    (root / "pricing.py").write_text(T.MODULE)
    (root / "checkout.py").write_text(T.CALLER)
    (root / "notes.md").write_text(T.NOTES)
    (root / ".jarvis").mkdir()
    (root / ".serena").mkdir()
    (root / ".serena" / "project.yml").write_text(
        'project_name: "nav-probe"\nlanguage_servers:\n- python\n'
        'ignore_all_files_in_gitignore: false\n')
    subprocess.run(["git", "init", "-q"], cwd=root, check=False)
    return root


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spawn real `claude` turns and report whether the FIRST "
                    "code-navigation call was a symbol call. Spends tokens.")
    parser.add_argument("variant", nargs="?", choices=sorted(VARIANTS),
                        help="which (brief, env, hook) combination to run")
    parser.add_argument("--runs", type=int, default=1, help="turns to spawn (each bills)")
    parser.add_argument("--list", action="store_true",
                        help="print the variant table and exit, spending nothing")
    args = parser.parse_args()

    if args.list or not args.variant:
        for name, (brief_ts, env_ts, hook, patch) in sorted(VARIANTS.items()):
            print(f"{name:22} brief={brief_ts:4} env={env_ts:4} hook={hook:3} "
                  f"ritual={patch}")
        return

    brief_ts, env_ts, hook, patch = VARIANTS[args.variant]
    if patch:
        real = worker_brief.navigation_core

        def patched(serena=True, tool_search="cli"):
            return RITUAL if (serena and tool_search == "off") else real(serena, tool_search)
        worker_brief.navigation_core = patched

    repo = make_repo()
    # The eval's own helper, so the probe cannot drift from what the eval briefs with.
    brief = T.worker_briefing(repo, brief_ts)
    if patch:
        assert "YOUR FIRST CODE-NAVIGATION CALL" in brief

    for i in range(args.runs):
        outcome: dict[str, str] = {}
        calls = T.run_and_record(
            repo, QUESTION, system_prompt=brief, bash_first="off", tool_search=env_ts,
            py_nav_hook=hook, outcome=outcome)
        first = T.first_navigation_call(calls)
        print(json.dumps({
            "variant": args.variant, "run": i,
            "first_nav": first,
            "verdict": "PASS" if first and T.is_symbol_call(first["tool"]) else "FAIL",
            "answered": bool(outcome.get("text")),
            "error": outcome.get("error", "")[:200],
            "log": [c["tool"] + (f"({c['command'][:60]})" if c["tool"] == "Bash" else "")
                    for c in calls],
        }), flush=True)


if __name__ == "__main__":
    main()
