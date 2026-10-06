"""Does a dispatched worker's FIRST Serena call work, in a fresh worktree?

The failure: the Claude Code plugin starts the Serena MCP server with fixed args carrying
neither `--project` nor `--project-from-cwd`, and no Serena env var selects a project, so
the first `find_symbol` of a dispatched turn comes back `No active project`. Spec
docs/specs/2026-10-02-serena-the-cheap-path.md §5.

TWO MODES, one real `claude -p` turn each, so each run bills tokens. Not a test.

    uv run python scripts/probe_serena_activation.py                  # BEFORE
    uv run python scripts/probe_serena_activation.py --wo <wo-id>     # AFTER

BEFORE (no `--wo`): the settings carry the scratch id, which maps to no work-order
record, so `SessionStart` injects nothing and the turn shows the raw failure — expect
`No active project`.

AFTER (`--wo <real-id>`): the settings carry a real work-order id, the hook maps it and
injects `hooks.serena_activation_context`, and the same turn shows symbols.

THREE PATHS THAT MUST NOT BE CONFLATED, each the subject of a wrong earlier revision:

  CODE_ROOT    this checkout — where the scratch worktree is cut from, and where the
               `jarvis` build under test lives. `jarvis` on the operator's PATH is the
               PRODUCTION venv, and `dispatch._worker_path()` bakes that bindir into
               `settings["env"]["PATH"]`, which WINS inside the session: left alone, the
               `jarvis _hook` that runs is production's build, which has no
               `serena_activation_context`, and the probe measures the old code.
  STATE_ROOT   where the project DB is (`.jarvis/jarvis.db`). It is the MAIN checkout,
               never a worktree, and it is what `JARVIS_PROJECT_PATH` must name or the
               hook cannot map the session to a work order.
  SCRATCH      a worktree id of its OWN, never the work-order id: `CODE_ROOT/.claude/
               worktrees/<wo-id>` is the live session's own worktree.

The 25-second wait is load-bearing: the Serena MCP server reports `pending` for the first
seconds of a session, and a call made before it connects fails for a reason that has
nothing to do with project activation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(CODE_ROOT / "src"))

from jarvis.catalog import ProjectSpec  # noqa: E402
from jarvis.dispatch import _write_worker_settings  # noqa: E402


def state_root(code_root: Path) -> Path:
    """The checkout holding `.jarvis/jarvis.db` — this one, or its parent repository.

    A worktree's `.jarvis/` holds only `worker-settings`; the records live in the main
    checkout, and a probe pointed at the worktree maps no session to any work order.
    """
    parts = code_root.parts
    if len(parts) > 3 and parts[-3:-1] == (".claude", "worktrees"):
        return code_root.parents[2]
    return code_root


STATE_ROOT = state_root(CODE_ROOT)

#: Seconds to wait inside the session before the first MCP call. The server is `pending`
#: for the first few; this is that with room to spare, and well under the 240s a single
#: foreground Bash call may block (`hooks.MAX_FOREGROUND_SECONDS`).
MCP_WARMUP_SECONDS = 25

#: THE PROMPT MAY NOT MENTION ACTIVATION IN EITHER DIRECTION, and both directions were
#: tried and were wrong. A revision that said "make ONE call … do not call any other
#: tool" SUPPRESSED the behaviour under test: the injected block arrived (confirmed in
#: the transcript) and the model obeyed the prohibition over it — 0 `activate_project`.
#: A revision that said "obey any instruction already in this context about activating a
#: Serena project" FAKED it: a dispatched worker gets no such sentence, so the run proved
#: the injected string was correct and not that a worker acts on it, which is the claim.
#: Hence a neutral ask, which is what a real work order looks like.
PROMPT = """\
Find the symbol `handle_hook` in src/jarvis/hooks.py. Use Serena's find_symbol.

Wait about {warmup} seconds first — the Serena MCP server is not connected yet.
"""


def claude() -> str:
    return os.environ.get("JARVIS_CLAUDE_BIN", "claude")


def make_worktree(scratch_id: str) -> Path:
    """A scratch worktree where dispatch predicts one: `<root>/.claude/worktrees/<id>`.

    The path is not a free choice — `_write_worker_settings` computes its permission
    rules and its CLAUDE.md excludes from exactly this layout. REFUSES an existing path:
    a live session's worktree sits in the same directory and running the turn inside it
    would measure that session's tree, not a fresh one.
    """
    tree = CODE_ROOT / ".claude" / "worktrees" / scratch_id
    if tree.exists():
        raise SystemExit(f"refusing: {tree} already exists — pass a unique --scratch id")
    subprocess.run(["git", "worktree", "add", "--detach", str(tree), "HEAD"],
                   cwd=CODE_ROOT, check=True, capture_output=True, text=True)
    return tree


def drop_worktree(tree: Path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(tree)],
                   cwd=CODE_ROOT, capture_output=True, text=True)
    shutil.rmtree(tree, ignore_errors=True)


def settings_for(scratch_id: str, wo_id: str | None) -> Path:
    """The worker's settings file, written by dispatch's own writer and then corrected.

    Reused rather than reimplemented: a probe that hand-built this file would be
    measuring a settings shape no dispatch ever produces. Two values are rewritten
    afterwards, both for reasons in the module docstring — `env.PATH` so the hook runs
    THIS build's `jarvis`, and `env.JARVIS_PROJECT_PATH` / `env.JARVIS_WO_ID` so the
    hook can map the session to a record (or, with no `--wo`, provably cannot).
    """
    path = _write_worker_settings(ProjectSpec(name="jarvis-os", path=CODE_ROOT),
                                  {"id": scratch_id, "title": "serena probe"})
    settings = json.loads(path.read_text())
    env = settings.setdefault("env", {})
    bindir = str(CODE_ROOT / ".venv" / "bin")
    env["PATH"] = os.pathsep.join([bindir, *(
        p for p in str(env.get("PATH") or "").split(os.pathsep) if p and p != bindir)])
    env["JARVIS_PROJECT_PATH"] = str(STATE_ROOT)
    if wo_id:
        env["JARVIS_WO_ID"] = wo_id
    path.write_text(json.dumps(settings, indent=2))
    return path


def hook_build(settings: Path) -> str:
    """Which `jarvis` the settings' PATH resolves, and whether it has the fix.

    The one line that says whether this run measured the new code or production's.
    """
    env_path = json.loads(settings.read_text())["env"]["PATH"]
    exe = shutil.which("jarvis", path=env_path)
    if not exe:
        raise SystemExit(f"refusing: no `jarvis` on the settings PATH {env_path!r}")
    if not exe.startswith(str(CODE_ROOT)):
        raise SystemExit(f"refusing: settings PATH resolves {exe}, not this checkout")
    interpreter = Path(exe).read_text().splitlines()[0].lstrip("#!").strip()
    probe = subprocess.run(
        [interpreter, "-c", "import jarvis.hooks as h; "
         "print(h.__file__, hasattr(h, 'serena_activation_context'))"],
        capture_output=True, text=True)
    return f"{exe}\n          {interpreter}\n          {probe.stdout.strip()}"


def transcript(session_id: str) -> Path | None:
    for path in (Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"):
        return path
    return None


def tool_calls(path: Path) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """`[(tool_use_id, tool name)]` in call order, and every tool result by id.

    De-duplicated by `tool_use_id`: a transcript carries each record more than once, and
    counting lines reports a single call as two.
    """
    calls: list[tuple[str, str]] = []
    seen: set[str] = set()
    results: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") or {}
        for block in (message.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id") not in seen:
                seen.add(block["id"])
                calls.append((block["id"], str(block.get("name") or "")))
            elif block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "".join(c.get("text", "") for c in content
                                      if isinstance(c, dict))
                results.setdefault(str(block.get("tool_use_id")), str(content or ""))
    return calls, results


def first_find_symbol(session_id: str) -> tuple[str | None, list[str]]:
    """The FIRST `find_symbol` result of the turn, verbatim, and the tool call order.

    The reading the spec asks for, and the only one that is honest under a neutral
    prompt: the final assistant message is the model's prose and is not pinned by
    anything now that the prompt no longer dictates it.
    """
    path = transcript(session_id)
    if path is None:
        return None, []
    calls, results = tool_calls(path)
    order = [name for _, name in calls]
    for call_id, name in calls:
        if name.endswith("__find_symbol"):
            return results.get(call_id, ""), order
    return None, order


def run_turn(tree: Path, settings: Path, timeout: int) -> dict[str, object]:
    args = [claude(), "-p", "--output-format", "json", "--permission-mode", "auto",
            "--settings", str(settings),
            "--", PROMPT.format(warmup=MCP_WARMUP_SECONDS)]
    started = time.time()
    done = subprocess.run(args, cwd=tree, capture_output=True, text=True,
                          timeout=timeout)
    out: dict[str, object] = {"rc": done.returncode,
                              "seconds": round(time.time() - started, 1),
                              "stderr": done.stderr.strip()[-600:]}
    try:
        data = json.loads(done.stdout)
    except ValueError:
        out["result"] = done.stdout.strip()
        return out
    out["result"] = str(data.get("result") or "") if isinstance(data, dict) else ""
    if isinstance(data, dict):
        out["session_id"] = data.get("session_id")
        out["cost_usd"] = data.get("total_cost_usd")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BEFORE run with no --wo (the hook is silent; expect "
                    "`No active project`); AFTER run with --wo <real work-order id> "
                    "(the hook injects the activation call; expect symbols).")
    parser.add_argument("--wo", default=None,
                        help="a REAL work-order id in the project DB. Omit for the "
                             "BEFORE run — never the scratch worktree id")
    parser.add_argument("--scratch", default=f"probe-serena-{int(time.time())}",
                        help="scratch worktree id, must not already exist")
    parser.add_argument("--keep", action="store_true", help="leave the scratch worktree")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    settings = settings_for(args.scratch, args.wo)
    tree = make_worktree(args.scratch)
    print(f"mode      {'AFTER (--wo ' + args.wo + ')' if args.wo else 'BEFORE (no --wo)'}")
    print(f"code      {CODE_ROOT}")
    print(f"state     {STATE_ROOT}")
    print(f"worktree  {tree}")
    print(f"settings  {settings}")
    print(f"hook      {hook_build(settings)}")
    print(f"serena    JARVIS_SERENA="
          f"{json.loads(settings.read_text())['env'].get('JARVIS_SERENA')}")
    print(f"project   .serena/project.yml present: "
          f"{(tree / '.serena' / 'project.yml').exists()}")
    try:
        out = run_turn(tree, settings, args.timeout)
    finally:
        if not args.keep:
            drop_worktree(tree)

    print(f"\nrc {out['rc']}  {out['seconds']}s  session {out.get('session_id')}  "
          f"${out.get('cost_usd')}")
    if out.get("session_id"):
        reply, order = first_find_symbol(str(out["session_id"]))
        print("--- first find_symbol result, verbatim ---")
        print(reply if reply is not None
              else "NO find_symbol CALL IN THIS TURN — there is no reading to report")
        print("--- tool calls, in order ---")
        print(", then ".join(order) if order else "(none)")
    else:
        print("--- claude stdout, verbatim (no session id, so no transcript) ---")
        print(out["result"])
    if out["stderr"]:
        print("--- stderr ---")
        print(out["stderr"])


if __name__ == "__main__":
    main()
