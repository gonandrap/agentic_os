#!/usr/bin/env python3
"""Can a peer message reach a HEADLESS (`claude -p`) worker MID-TURN?

Hand-run only. §3 of
docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
says why this cannot be a pytest: the root conftest.py points JARVIS_CLAUDE_BIN at a
stub that exits 1, so a test would measure the stub and report its behaviour as the
answer.

Receiver argv mirrors `claude_cli.turn_args()` / `spawn_turn()` exactly — `-p
--output-format json --session-id <uuid> -n "[WO …] …"`, detached, stdin=DEVNULL. A
spike run in a friendlier shape answers a question nobody asked.

Mid-turn is PROVEN BY CONSTRUCTION, not asserted: a `-p` session gets one turn and
exits. If the message is in that turn's result, it arrived between tool calls.

    uv run python scripts/spike_peer_message.py --trials 5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Disablers named in the spec: any one of these switches the peer feature off, so a
#: fleet with one set gets the queue fallback and must not silently get nothing.
DISABLERS = ("DISABLE_TELEMETRY", "DO_NOT_TRACK",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_GROWTHBOOK")

RECEIVER_PROMPT = """\
Harness session. Do exactly this and nothing else.

1. Call the Bash tool {calls} times, one call each, each running: sleep {secs}
2. If at any point another agent sends you a message, stop the loop at the next
   opportunity.
3. Your FINAL message must be ONE line of JSON and nothing else - no prose, no code
   fence:
   {{"received": true or false, "text": "<the message verbatim, or empty>",
     "sender": "<who it says sent it, or empty>", "calls_done": <integer>}}

Use no tool other than Bash.
"""

SENDER_PROMPT = """\
Do exactly this and nothing else.

1. Call ListAgents.
2. Call SendMessage with to="{name}" and message="{token}"
3. Your FINAL message must be ONE line of JSON and nothing else:
   {{"sent": true or false, "saw_target": true or false, "agents": ["<names>"]}}
"""


def claude() -> str:
    return os.environ.get("JARVIS_CLAUDE_BIN", "claude")


def cli_version() -> str:
    out = subprocess.run([claude(), "--version"], capture_output=True, text=True)
    return out.stdout.strip()


def spawn_receiver(sid: str, name: str, outfile: Path, calls: int, secs: int,
                   autocompact: str | None,
                   settings: str | None = None) -> subprocess.Popen[bytes]:
    args = [claude(), "-p", "--output-format", "json", "--session-id", sid,
            "-n", name, "--permission-mode", "auto"]
    if autocompact:
        args += ["--autocompact", autocompact]
    if settings:
        args += ["--settings", settings]
    args += ["--", RECEIVER_PROMPT.format(calls=calls, secs=secs)]
    outfile.parent.mkdir(parents=True, exist_ok=True)
    with outfile.open("wb") as out:
        return subprocess.Popen(args, cwd=REPO, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)


def run_sender(name: str, token: str, timeout: int) -> dict[str, object]:
    args = [claude(), "-p", "--output-format", "json", "--permission-mode", "auto",
            "--allowedTools", "ListAgents", "SendMessage",
            "--", SENDER_PROMPT.format(name=name, token=token)]
    try:
        done = subprocess.run(args, cwd=REPO, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": "sender timed out"}
    return {"rc": done.returncode, "result": result_field(done.stdout),
            "stderr": done.stderr.strip()[:400]}


def result_field(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except ValueError:
        return stdout.strip()[:400]
    if isinstance(data, dict):
        return str(data.get("result") or "").strip()
    return ""


def parse_json_line(text: str) -> dict[str, object] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def transcript(sid: str) -> Path | None:
    for p in (Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
        return p
    return None


def trial(n: int, calls: int, secs: int, fire_after: int, autocompact: str | None,
          workdir: Path, settings: str | None = None) -> dict[str, object]:
    sid = str(uuid.uuid4())
    token = f"SPIKE-{uuid.uuid4().hex[:12]}"
    name = f"[WO wo-spike-{n}] peer message spike"
    outfile = workdir / f"trial-{n}.json"
    proc = spawn_receiver(sid, name, outfile, calls, secs, autocompact, settings)
    time.sleep(fire_after)
    t0 = time.time()
    sender = run_sender(name, token, timeout=calls * secs + 120)
    sent_at = time.time() - t0
    try:
        proc.wait(timeout=calls * secs + 180)
    except subprocess.TimeoutExpired:
        proc.kill()
    raw = outfile.read_text(errors="replace") if outfile.exists() else ""
    res = result_field(raw)
    got = parse_json_line(res) or {}
    tpath = transcript(sid)
    ttext = tpath.read_text(errors="replace") if tpath else ""
    return {
        "trial": n,
        "session_id": sid,
        "token": token,
        "autocompact": autocompact,
        "sender": sender,
        "sender_seconds": round(sent_at, 1),
        "receiver_said": got or res[:300],
        "delivered_mid_turn": bool(got.get("received")) and token in str(got.get("text", "")),
        "token_in_transcript": token in ttext,
        "transcript": str(tpath) if tpath else None,
        "sender_identified": str(got.get("sender") or ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--calls", type=int, default=12)
    ap.add_argument("--secs", type=int, default=5)
    ap.add_argument("--fire-after", type=int, default=20)
    ap.add_argument("--autocompact", default=None)
    # The receiver-side off switch: `crossSessionInbound` accept | hold | refuse.
    ap.add_argument("--settings", default=None)
    ap.add_argument("--out", default="/tmp/spike-peer-message")
    a = ap.parse_args()

    workdir = Path(a.out)
    workdir.mkdir(parents=True, exist_ok=True)
    env_report = {k: os.environ.get(k) for k in DISABLERS}
    settings = Path.home() / ".claude" / "settings.json"
    sj = json.loads(settings.read_text()) if settings.exists() else {}
    header = {
        "cli_version": cli_version(),
        "disablers_set": {k: v for k, v in env_report.items() if v},
        "crossSessionInbound": sj.get("crossSessionInbound", "(unset)"),
        "isolatePeerMachines": sj.get("isolatePeerMachines", "(unset)"),
        "autocompact": a.autocompact,
    }
    print(json.dumps(header, indent=2), flush=True)

    rows = []
    for n in range(1, a.trials + 1):
        row = trial(n, a.calls, a.secs, a.fire_after, a.autocompact, workdir,
                    a.settings)
        rows.append(row)
        print(json.dumps(row), flush=True)

    ok = sum(1 for r in rows if r["delivered_mid_turn"] and r["token_in_transcript"])
    print(json.dumps({"trials": len(rows), "passed": ok,
                      "verdict": "works" if ok == len(rows) else
                                 ("does not" if ok == 0 else "not reliable")},
                     indent=2))
    (workdir / "summary.json").write_text(
        json.dumps({"header": header, "rows": rows, "passed": ok}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
