"""Re-measure the facts the background worker transport stands on (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

CWD = os.environ.get("SPIKE_CWD") or os.getcwd()
BASE = ["--model", "haiku", "--permission-mode", "auto"]


def claude(*args: str, cwd: str = CWD) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["claude", *args], cwd=cwd, capture_output=True, text=True,
                          timeout=120)


def roster(short: str) -> dict:
    out = claude("agents", "--json", "--all").stdout
    return next((a for a in json.loads(out or "[]") if a.get("id") == short), {})


def wait(short: str, until, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = roster(short)
        if until(entry):
            return entry
        time.sleep(1)
    raise SystemExit(f"timed out waiting on {short}: {roster(short)}")


def stop(short: str) -> None:
    """Fact 8: the roster drops the pid before the process exits; wait for the process"""
    pid = roster(short).get("pid")
    claude("stop", short)
    wait(short, lambda e: not e.get("pid"))
    while pid:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def short_of(out: str) -> str:
    return out.split("backgrounded · ", 1)[1].split()[0]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> None:
    first = claude("--bg", "--session-id", "00000000-0000-4000-8000-000000000000",
                   "-n", "[spike] bg transport", *BASE, "--",
                   "Reply with exactly the word: PINEAPPLE. No tools.")
    check("1 --bg mints its own id", "ignoring --session-id" in first.stderr, first.stderr)
    short = short_of(first.stdout)
    entry = wait(short, lambda e: e.get("status") == "idle")
    sid = entry["sessionId"]
    check("2 roster carries pid + status", bool(entry.get("pid")), json.dumps(entry))

    live = claude("--bg", "--resume", sid, "--", "say hi")
    check("5 resuming a LIVE session forks", "started a copy" in live.stderr, live.stderr)
    claude("rm", short_of(live.stdout))

    stop(short)
    flagged = claude("--bg", "--resume", sid, *BASE, "--", "say hi")
    check("6 a flagged resume forks", "saved options" in flagged.stderr, flagged.stderr)
    claude("stop", short_of(flagged.stdout))
    claude("rm", short_of(flagged.stdout))

    bare = claude("--bg", "--resume", sid, "--",
                  "What word did you say first? Reply lowercased, no tools.")
    check("7 a bare resume of a stopped session continues it",
          short_of(bare.stdout) == short, bare.stdout + bare.stderr)
    wait(short, lambda e: e.get("status") == "idle")

    stop(short)
    task = claude("--bg", "--resume", sid, "--",
                  "Run the Bash command 'sleep 30 && echo WOKE' with run_in_background "
                  "true, then end your turn replying WAITING. When notified it finished, "
                  "reply FINISHED.")
    check("resume for the wake test", short_of(task.stdout) == short)
    time.sleep(12)
    check("10 busy while its own task is in flight", roster(short).get("status") == "busy",
          json.dumps(roster(short)))
    wait(short, lambda e: e.get("status") == "idle")
    print("PASS  9 the session woke itself and settled")

    claude("respawn", short)
    back = wait(short, lambda e: e.get("status") == "idle")
    check("11 respawn keeps the id", back.get("sessionId") == sid)

    stop(short)
    claude("rm", short)
    after = claude("--bg", "--resume", sid, "--", "Reply with exactly: BACK. No tools.")
    check("14 a removed session resumes under the same id", short_of(after.stdout) == short,
          after.stdout + after.stderr)
    wait(short, lambda e: e.get("status") == "idle")
    stop(short)
    claude("rm", short)
    print("all facts hold on", claude("--version").stdout.strip())


if __name__ == "__main__":
    main()
