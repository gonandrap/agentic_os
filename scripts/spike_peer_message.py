#!/usr/bin/env python3
"""Can a peer message reach a HEADLESS (`claude -p`) worker MID-TURN?

Hand-run only. §3 of
docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
says why this cannot be a pytest: the root conftest.py points JARVIS_CLAUDE_BIN at a
stub that exits 1, so a test would measure the stub and report its behaviour as the
answer.

Receiver argv mirrors `claude_cli.turn_args()` / `spawn_turn()` in every part that
could plausibly change the answer — `-p --output-format json -n "[WO …] …"`, detached,
stdin=DEVNULL. ONE DELIBERATE DIFFERENCE: `--session-id` on a fresh session, where a
real worker's second and later turns use `--resume`. A spike run in a friendlier shape
answers a question nobody asked, so the difference is named rather than hidden: nothing
measured here distinguishes the two, and a resumed session is NOT covered.

Mid-turn is PROVEN BY CONSTRUCTION, not asserted: a `-p` session gets one turn and
exits. If the message is in that turn's result, it arrived between tool calls.

    uv run python scripts/spike_peer_message.py --trials 5
    uv run python scripts/spike_peer_message.py --mode identity

`--mode identity` answers §3.2 unknown 4 only as far as it goes: a hook allow-listing
the sender's pid refuses a sender that is not on the list. It does NOT test FORGERY -
no sender here presents another process's pid - so nothing it prints makes the envelope
pid trustworthy. See the spike doc.
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

#: Disablers the 2026-08-17 doc review said switch the peer feature off. MEASURED
#: FALSE for DISABLE_TELEMETRY on 2.1.281; the other three are reported, not tested.
#: Printed in the header so a run says which were set while it measured.
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


# --------------------------------------------------------------------------
# Identity mode: can a receiver tell a KNOWN sender from an UNKNOWN one?
# §3.2 unknown 4. A "no" here means the peer transport does not ship, so it is
# measured, not reasoned about.
# --------------------------------------------------------------------------

HOOK = """#!/bin/bash
payload=$(cat)
printf '%s\\n' "$payload" >> {log}
case "$payload" in *cross-session-message*) ;; *) exit 0 ;; esac
pid=$(printf '%s' "$payload" | grep -o 'cc-socks/[0-9]*\\.sock' | head -1 \\
      | grep -o '[0-9][0-9]*')
if grep -qx "$pid" {allow} 2>/dev/null; then exit 0; fi
echo "peer message refused: sender pid $pid is not allow-listed" >&2
exit 2
"""


def descendants(root: int) -> set[int]:
    """Every pid under `root`, because `claude` is a shim and the socket path names
    the node process, not the pid we spawned."""
    kids: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            ppid = int(next(l.split()[1] for l in
                            (entry / "status").read_text().splitlines()
                            if l.startswith("PPid:")))
        except (OSError, StopIteration, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(entry.name))
    out, stack = set(), [root]
    while stack:
        cur = stack.pop()
        out.add(cur)
        stack += kids.get(cur, [])
    return out


def run_sender_tracked(name: str, token: str, timeout: int, allow: Path | None,
                       sender_name: str | None) -> dict[str, object]:
    """Send, and (when `allow` is given) publish the sender's own pid tree as it runs.

    That IS the production shape: a long-lived daemon knows its own pid and can tell
    the receiver about it out of band. A forger cannot get into this file.
    """
    args = [claude(), "-p", "--output-format", "json", "--permission-mode", "auto",
            "--allowedTools", "ListAgents", "SendMessage"]
    if sender_name:
        args += ["-n", sender_name]
    args += ["--", SENDER_PROMPT.format(name=name, token=token)]
    proc = subprocess.Popen(args, cwd=REPO, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    published: set[int] = set()
    deadline = time.time() + timeout
    while proc.poll() is None and time.time() < deadline:
        if allow is not None:
            new = descendants(proc.pid) - published
            if new:
                published |= new
                allow.write_text("\n".join(str(p) for p in sorted(published)) + "\n")
        time.sleep(0.3)
    try:
        out, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = "", "sender timed out"
    return {"rc": proc.returncode, "result": result_field(out),
            "stderr": (err or "").strip()[:300],
            "published_pids": sorted(published)}


def identity_trials(calls: int, secs: int, fire_after: int,
                    workdir: Path) -> list[dict[str, object]]:
    log = workdir / "hook.log"
    allow = workdir / "allow-pids.txt"
    hook = workdir / "hook_allow.sh"
    hook.write_text(HOOK.format(log=log, allow=allow))
    hook.chmod(0o755)
    settings = workdir / "hook-settings.json"
    settings.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": str(hook)}]}]}}))

    rows = []
    # 1) honest: the sender publishes its own pids, so the hook allow-lists it.
    # 2) forger: same binary, NO publication, and it names itself after the honest
    #    sender - the `from-name` a receiver sees is self-declared.
    for label, track, sender_name in (("honest", True, "jarvis-daemon"),
                                      ("forger", False, "jarvis-daemon")):
        log.write_text("")
        allow.write_text("" if track else "1\n")  # pid 1 is never the sender
        sid = str(uuid.uuid4())
        token = f"SPIKE-{label.upper()}-{uuid.uuid4().hex[:8]}"
        name = f"[WO wo-spike-id-{label}] peer identity spike"
        outfile = workdir / f"identity-{label}.json"
        proc = spawn_receiver(sid, name, outfile, calls, secs, None, str(settings))
        time.sleep(fire_after)
        sender = run_sender_tracked(name, token, calls * secs + 120,
                                    allow if track else None, sender_name)
        try:
            proc.wait(timeout=calls * secs + 180)
        except subprocess.TimeoutExpired:
            proc.kill()
        raw = outfile.read_text(errors="replace") if outfile.exists() else ""
        got = parse_json_line(result_field(raw)) or {}
        hooktext = log.read_text(errors="replace") if log.exists() else ""
        envelopes = [l for l in hooktext.splitlines() if "cross-session-message" in l]
        rows.append({
            "case": label,
            "session_id": sid,
            "token": token,
            "sender_declared_name": sender_name,
            "hook_saw_envelope": bool(envelopes),
            "hook_saw_verified_peer_pid": "verifiedPeerPid" in hooktext,
            "sender_pids_published": sender.get("published_pids"),
            "receiver_received": bool(got.get("received")),
            "receiver_calls_done": got.get("calls_done"),
            "sender_rc": sender.get("rc"),
            "sender_result": str(sender.get("result"))[:200],
        })
        print(json.dumps(rows[-1]), flush=True)
    return rows


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
    ap.add_argument("--mode", choices=("delivery", "identity"), default="delivery")
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

    if a.mode == "identity":
        rows = identity_trials(a.calls, a.secs, a.fire_after, workdir)
        honest = next(r for r in rows if r["case"] == "honest")
        forger = next(r for r in rows if r["case"] == "forger")
        verdict = ("discriminates" if honest["receiver_received"]
                   and not forger["receiver_received"] else "does not discriminate")
        print(json.dumps({"mode": "identity", "verdict": verdict}, indent=2))
        (workdir / "identity.json").write_text(
            json.dumps({"header": header, "rows": rows, "verdict": verdict},
                       indent=2))
        return 0

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
