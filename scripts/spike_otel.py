#!/usr/bin/env python3
"""What does Claude Code's OpenTelemetry export ACTUALLY emit?

Hand-run only. §9 of docs/specs/2026-09-24-order-observability.md asks six questions
about what arrives when `CLAUDE_CODE_ENABLE_TELEMETRY=1` and an OTLP endpoint are set
around a real headless turn, and says the listener must "record everything and interpret
nothing". This script is that listener plus one scratch turn. It prints MEASUREMENTS
ONLY - metric names, log-event names, attribute keys, arrival times, and literal
substring probes - and draws NO verdict about adopting OTEL. That verdict is the human
deliverable; a script that pre-empted it would be answering a question nobody asked.

    uv run python scripts/spike_otel.py --out /tmp/spike-otel-clean
    uv run python scripts/spike_otel.py --mode killed --out /tmp/spike-otel-killed
    uv run python scripts/spike_otel.py --mode dead --out /tmp/spike-otel-dead

TWO THINGS THIS CANNOT ANSWER, and no run of it ever will:

1. It measures ONE CLI version on ONE machine. Everything here is `claude --version` at
   the moment of the run (recorded in the header); a later version can add, rename or
   drop any name printed below.
2. It measures A SINGLE WORKER, not many concurrent ones. §9 question 6 - the cost with
   many concurrent headless workers each exporting - is NOT covered: one turn says
   nothing about port contention, collector load or interleaved flushes.

`scripts/spike_peer_message.py` is the shape this mirrors: stdlib only, `--out` workdir,
one JSON object per event on stdout, a summary dict at the end and a written summary.json.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Grep handle for §9 question 2 ("does any payload carry tool parameters"). The prompt
#: puts it inside Bash argument text, so its presence in ANY body is the evidence.
MARKER = "SPIKE-MARKER"

PROMPT = f"""\
Harness session. Do exactly this and nothing else.

1. Call the Bash tool three times, one call each, each running:
   sleep 4; echo {MARKER}-1
   sleep 4; echo {MARKER}-2
   sleep 4; echo {MARKER}-3
   (one command per call, in that order)
2. Read the file {REPO}/README.md
3. Dispatch ONE Explore subagent and ask it to name one file under src/jarvis/.
4. Your FINAL message is one line: DONE
"""

#: §9 prior (2): these switch telemetry off. Deleted from the child env so a run cannot
#: silently measure a disabled exporter; the header says which were present.
DISABLERS = ("DISABLE_TELEMETRY", "DO_NOT_TRACK",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")

#: Literal substrings searched over every decoded body. RAW EVIDENCE for §9 questions
#: 2-4 (tool params, cache-write cause / TTL split / modelUsage, context composition).
#: Found/not-found only - no term here implies a conclusion.
PROBES = (MARKER, "sleep", "tool_input", "parameters", "ephemeral_5m", "ephemeral_1h",
          "cache_creation", "cache_read", "cacheCreation", "modelUsage",
          "system_prompt", "systemPrompt", "skill", "agent", "subagent", "context",
          "README")

PRINTABLE_RUN = re.compile(rb"[ -~]{4,}")


# --------------------------------------------------------------------------
# Part 1: the listener. Accepts POST on ANY path - /v1/metrics, /v1/logs,
# /v1/traces, or whatever a future version invents - because the question is what
# arrives, not whether it matches a route we predicted.
# --------------------------------------------------------------------------

class Recorder:
    def __init__(self, workdir: Path) -> None:
        self.t0 = time.monotonic()
        self.raw = workdir / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.requests: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self.seq = 0

    def record(self, path: str, headers, body: bytes) -> dict[str, object]:
        with self.lock:
            self.seq += 1
            seq = self.seq
        slug = re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-") or "root"
        # Written BEFORE parsing, unconditionally: the audit trail must survive a
        # parse bug in anything below.
        (self.raw / f"{seq:04d}-{slug}.bin").write_bytes(body)
        raw = body
        if (headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(body)
            except OSError as exc:
                raw = body
                gz_error: str | None = repr(exc)
            else:
                gz_error = None
        else:
            gz_error = None
        row: dict[str, object] = {
            "seq": seq,
            "t": round(time.monotonic() - self.t0, 2),
            "path": path,
            "content_type": headers.get("Content-Type"),
            "content_encoding": headers.get("Content-Encoding"),
            "bytes": len(body),
            "raw_file": str(self.raw / f"{seq:04d}-{slug}.bin"),
        }
        if gz_error:
            row["gunzip_error"] = gz_error
        try:
            row["json"] = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # Protobuf (or anything else non-JSON) still yields its metric and
            # attribute names as ASCII runs, which is what §9 question 1 asks for.
            row["raw_repr"] = repr(raw)[:20000]
            row["printable_runs"] = [m.group(0).decode("ascii")
                                     for m in PRINTABLE_RUN.finditer(raw)]
        row["decoded"] = raw.decode("utf-8", errors="replace")
        with self.lock:
            self.requests.append(row)
        return row


def make_handler(rec: Recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            try:
                row = rec.record(self.path, self.headers, body)
            except Exception as exc:  # a recorder bug must not kill the export
                row = {"recorder_error": repr(exc), "path": self.path}
            printable = {k: v for k, v in row.items() if k != "decoded"}
            print(json.dumps(printable, default=str)[:4000], flush=True)
            payload = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: object) -> None:
            """No-op: the default access log goes to stderr and would drown the
            one-JSON-object-per-event stream this script IS."""

    return Handler


def start_listener(workdir: Path) -> tuple[ThreadingHTTPServer, Recorder, int]:
    rec = Recorder(workdir)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rec))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, rec, port


# --------------------------------------------------------------------------
# Part 2: the turn runner. Argv and spawn shape mirror claude_cli.spawn_turn;
# §9 names `env = {**os.environ, **cache_env()}` as the seam an adoption would use,
# so the overlay below is exactly what production would have to add.
# --------------------------------------------------------------------------

def claude() -> str:
    return os.environ.get("JARVIS_CLAUDE_BIN") or "claude"


def cli_version() -> str:
    try:
        out = subprocess.run([claude(), "--version"], capture_output=True, text=True)
    except OSError as exc:
        return f"(unavailable: {exc})"
    return out.stdout.strip() or out.stderr.strip()


def otel_env(port: int, protocol: str, interval_ms: int | None,
             log_prompts: bool) -> dict[str, str]:
    overlay = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{port}",
    }
    # Left UNSET by default on purpose: §9 question 5 asks what the CLI's OWN default
    # flush interval is, and naming one here would measure our setting instead.
    if interval_ms is not None:
        overlay["OTEL_METRIC_EXPORT_INTERVAL"] = str(interval_ms)
        overlay["OTEL_LOGS_EXPORT_INTERVAL"] = str(interval_ms)
    if log_prompts:
        overlay["OTEL_LOG_USER_PROMPTS"] = "1"
    return overlay


def spawn_turn(overlay: dict[str, str], outfile: Path,
               errfile: Path | None = None) -> subprocess.Popen[bytes]:
    args = [claude(), "-p", "--output-format", "json",
            "--session-id", str(uuid.uuid4()), "--permission-mode", "auto",
            "--", PROMPT]
    env = {**os.environ, **overlay}
    for k in DISABLERS:
        env.pop(k, None)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    with outfile.open("wb") as out:
        # errfile only for --mode dead, which must report stderr VERBATIM and so cannot
        # have it interleaved into the JSON envelope on stdout. Left None everywhere
        # else, keeping the clean/killed spawn byte-identical to before.
        if errfile is None:
            return subprocess.Popen(args, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        with errfile.open("wb") as err:
            return subprocess.Popen(args, cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)


#: Cap on the stderr text copied into summary.json for --mode dead. Stated in the
#: payload beside the text, because this is output the SCRIPT did not write: a reader
#: must be able to tell "that was all of it" from "that is where we stopped copying".
STDERR_CAP = 20000


def prove_port_closed() -> dict[str, object]:
    """Pick a port and PROVE nothing listens on it, or abort.

    Measuring against a port that turned out to be open would be a false negative:
    some other server would answer the exporter's POSTs, the turn would see a working
    endpoint, and the run would report "a dead endpoint is harmless" having never had
    one. So: bind an ephemeral port to learn a free number, close it, then connect and
    require a refusal. A successful connect means the number was reused between the
    close and the probe - abort rather than measure the wrong thing.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        holder.bind(("127.0.0.1", 0))
        port = int(holder.getsockname()[1])
    finally:
        holder.close()
    probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe_sock.settimeout(2.0)
    try:
        probe_sock.connect(("127.0.0.1", port))
    except ConnectionRefusedError as exc:
        proof = {"port": port, "method": "bind ephemeral, close, then connect",
                 "connect_result": "refused", "connect_error": repr(exc)}
    except OSError as exc:
        proof = {"port": port, "method": "bind ephemeral, close, then connect",
                 "connect_result": "failed (not refused)", "connect_error": repr(exc)}
    else:
        probe_sock.close()
        raise SystemExit(
            f"abort: connect to 127.0.0.1:{port} SUCCEEDED, so something is listening "
            f"there. --mode dead measures a turn against a CLOSED port; running it "
            f"against someone else's server would measure the opposite. Re-run.")
    finally:
        probe_sock.close()
    return proof


# --------------------------------------------------------------------------
# Part 3: harvesting. Every walk is GENERIC - shape-matched, not path-coded - so a
# renamed nesting level still yields its keys instead of silently yielding none.
# --------------------------------------------------------------------------

def walk(obj: object):
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def metric_names(rows: list[dict[str, object]]) -> list[str]:
    names: set[str] = set()
    for row in rows:
        doc = row.get("json")
        if doc is not None:
            for rm in _get(doc, "resourceMetrics"):
                for sm in _get(rm, "scopeMetrics"):
                    for m in _get(sm, "metrics"):
                        if isinstance(m, dict) and isinstance(m.get("name"), str):
                            names.add(m["name"])
        elif "metrics" in str(row.get("path", "")):
            names.update(r for r in row.get("printable_runs", [])  # type: ignore[union-attr]
                         if r.startswith("claude_code."))
    return sorted(names)


def log_event_names(rows: list[dict[str, object]]) -> list[str]:
    names: set[str] = set()
    for row in rows:
        doc = row.get("json")
        if doc is not None:
            for rl in _get(doc, "resourceLogs"):
                for sl in _get(rl, "scopeLogs"):
                    for lr in _get(sl, "logRecords"):
                        if not isinstance(lr, dict):
                            continue
                        for attr in lr.get("attributes") or []:
                            if isinstance(attr, dict) and attr.get("key") == "event.name":
                                names.add(_scalar(attr.get("value")))
                        body = lr.get("body")
                        if body is not None:
                            names.add(_scalar(body))
        elif "logs" in str(row.get("path", "")):
            names.update(r for r in row.get("printable_runs", [])  # type: ignore[union-attr]
                         if r.startswith("claude_code."))
    return sorted(n for n in names if n)


def attribute_keys(rows: list[dict[str, object]]) -> list[str]:
    """Any {"key": ..., "value": ...} pair anywhere: resource, datapoint or log record.
    OTLP/JSON uses that shape at every level, so one generic walk beats three paths."""
    keys: set[str] = set()
    for row in rows:
        doc = row.get("json")
        if doc is None:
            continue
        for node in walk(doc):
            if isinstance(node, dict) and "key" in node and "value" in node:
                if isinstance(node["key"], str):
                    keys.add(node["key"])
    return sorted(keys)


def _get(obj: object, key: str) -> list[object]:
    if isinstance(obj, dict):
        v = obj.get(key)
        if isinstance(v, list):
            return v
    return []


def _scalar(value: object) -> str:
    """OTLP AnyValue: {"stringValue": "x"} / {"intValue": "1"} / a bare scalar."""
    if isinstance(value, dict):
        for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if k in value:
                return str(value[k])
        return json.dumps(value, sort_keys=True)[:200]
    return str(value)


def arrival_times(rows: list[dict[str, object]]) -> dict[str, object]:
    per_path: dict[str, list[float]] = {}
    for row in rows:
        per_path.setdefault(str(row.get("path")), []).append(float(row.get("t", 0.0)))
    return {
        path: {"times": ts,
               "gaps": [round(b - a, 2) for a, b in zip(ts, ts[1:])]}
        for path, ts in per_path.items()
    }


def probe(rows: list[dict[str, object]]) -> dict[str, bool]:
    blob = "".join(str(row.get("decoded", "")) for row in rows)
    blob += "".join("".join(row.get("printable_runs") or [])  # type: ignore[arg-type]
                    for row in rows)
    return {term: (term in blob) for term in PROBES}


def truncate(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Bodies capped in summary.json; the full ones are in <out>/raw/."""
    out = []
    for row in rows:
        copy = dict(row)
        for key in ("decoded", "raw_repr"):
            if isinstance(copy.get(key), str):
                copy[key] = copy[key][:4000]
        if copy.get("json") is not None:
            copy["json"] = json.dumps(copy["json"], sort_keys=True)[:4000]
        if copy.get("printable_runs") is not None:
            copy["printable_runs"] = copy["printable_runs"][:400]  # type: ignore[index]
        out.append(copy)
    return out


# --------------------------------------------------------------------------
# Part 4: modes. One mode per invocation.
# --------------------------------------------------------------------------

def run_dead(a: argparse.Namespace, rec: Recorder, overlay: dict[str, str],
             workdir: Path, proof: dict[str, object]) -> dict[str, object]:
    """--mode dead: same turn, endpoint pointed at a port NOTHING listens on.

    This mode exists because the finding it backs was otherwise unreproducible: the
    closed-port measurement in the findings document (rc=0, subtype success,
    is_error false, one ordinary stdin warning on stderr) was taken with an ad-hoc
    shell script that was never committed, so nobody could re-check it from the
    artifact - wo-b2f8a660 review round 1, defect 1. Measurement only: the numbers and
    the stderr text go into summary.json and this function draws no conclusion from them.
    """
    outfile = workdir / "turn-dead.json"
    errfile = workdir / "turn-dead.stderr.txt"
    proc = spawn_turn(overlay, outfile, errfile=errfile)
    info: dict[str, object] = {"pid": proc.pid, "mode": a.mode,
                               "closed_port": proof.get("port"),
                               "closed_port_proof": proof,
                               "endpoint": overlay["OTEL_EXPORTER_OTLP_ENDPOINT"]}
    rc = proc.wait()
    info["turn_rc"] = rc
    info["turn_done_at_t"] = round(time.monotonic() - rec.t0, 2)

    stderr_text = errfile.read_text(errors="replace") if errfile.exists() else ""
    info["stderr_bytes"] = errfile.stat().st_size if errfile.exists() else 0
    info["stderr_cap_chars"] = STDERR_CAP
    info["stderr_truncated"] = len(stderr_text) > STDERR_CAP
    info["stderr_verbatim"] = stderr_text[:STDERR_CAP]
    info["stderr_file"] = str(errfile)

    envelope: object = None
    try:
        envelope = json.loads(outfile.read_text())
    except (OSError, ValueError) as exc:
        info["envelope_parse_error"] = repr(exc)
    if isinstance(envelope, dict):
        info["result_subtype"] = envelope.get("subtype")
        info["result_is_error"] = envelope.get("is_error")
        info["result_duration_ms"] = envelope.get("duration_ms")
    info["stdout_file"] = str(outfile)
    # No listener was started, so there is nothing to drain and --drain is not honoured
    # here; said out loud so its absence does not read as a dropped wait.
    info["drained"] = False
    print(json.dumps({"event": "turn-settled", **info}, default=str), flush=True)
    return info


def run_mode(a: argparse.Namespace, rec: Recorder,
             overlay: dict[str, str], workdir: Path) -> dict[str, object]:
    proc = spawn_turn(overlay, workdir / f"turn-{a.mode}.json")
    info: dict[str, object] = {"pid": proc.pid, "mode": a.mode}

    if a.mode == "killed":
        # §9 question 5's second half: does a turn that DIES flush anything? Kill the
        # whole group - `claude` is a shim, so killing the pid alone leaves node alive.
        time.sleep(a.kill_after)
        before = len(rec.requests)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError) as exc:
            info["kill_error"] = repr(exc)
        kill_t = round(time.monotonic() - rec.t0, 2)
        proc.wait()
        info.update({"killed_after_s": a.kill_after, "kill_at_t": kill_t,
                     "requests_before_kill": before})
    else:
        rc = proc.wait()
        info["turn_rc"] = rc
        info["turn_done_at_t"] = round(time.monotonic() - rec.t0, 2)

    print(json.dumps({"event": "turn-settled", **info}), flush=True)
    # Drain: a slow flush arriving after the turn is exactly the evidence §9 wants,
    # so keep serving well past the process.
    time.sleep(a.drain)

    if a.mode == "killed":
        after = [r for r in rec.requests
                 if float(r.get("t", 0.0)) > float(info["kill_at_t"])]
        info["requests_after_kill"] = len(after)
        info["any_request_after_kill"] = bool(after)
        info["paths_after_kill"] = sorted({str(r.get("path")) for r in after})
    return info


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Record what Claude Code's OTEL export emits (spec §9). "
                    "Measurements only; no verdict.")
    ap.add_argument("--mode", choices=("clean", "killed", "dead"), default="clean",
                    help="clean: listener up, turn runs to completion. killed: listener "
                         "up, group SIGKILLed mid-turn. dead: NO listener, endpoint "
                         "points at a port proven closed")
    ap.add_argument("--out", default="/tmp/spike-otel")
    ap.add_argument("--protocol", default="http/json",
                    help="OTEL_EXPORTER_OTLP_PROTOCOL (http/json, http/protobuf, grpc)")
    ap.add_argument("--interval-ms", type=int, default=None,
                    help="set OTEL_METRIC/LOGS_EXPORT_INTERVAL; unset by default so "
                         "the CLI's own default interval is what gets measured")
    ap.add_argument("--drain", type=int, default=90,
                    help="keep serving this many seconds after the turn settles")
    ap.add_argument("--kill-after", type=int, default=25,
                    help="--mode killed: SIGKILL the group this long in (mid-tool-call)")
    ap.add_argument("--log-prompts", action="store_true",
                    help="set OTEL_LOG_USER_PROMPTS=1")
    a = ap.parse_args()

    workdir = Path(a.out)
    workdir.mkdir(parents=True, exist_ok=True)
    closed_proof: dict[str, object] | None = None
    if a.mode == "dead":
        srv = None
        rec = Recorder(workdir)
        closed_proof = prove_port_closed()
        port = int(closed_proof["port"])  # type: ignore[arg-type]
    else:
        srv, rec, port = start_listener(workdir)
    overlay = otel_env(port, a.protocol, a.interval_ms, a.log_prompts)

    header = {
        "cli_version": cli_version(),
        "claude_bin": claude(),
        "mode": a.mode,
        "port": port,
        "listener": a.mode != "dead",
        "closed_port_proof": closed_proof,
        "drain_s": a.drain if a.mode != "dead" else None,
        "kill_after_s": a.kill_after if a.mode == "killed" else None,
        "protocol": a.protocol,
        "interval_ms": a.interval_ms,
        "log_prompts": bool(a.log_prompts),
        "env_overlay": overlay,
        "env_deleted": list(DISABLERS),
        "env_deleted_that_were_set": [k for k in DISABLERS if k in os.environ],
        "marker": MARKER,
        "out": str(workdir),
    }
    print(json.dumps(header, indent=2), flush=True)

    if a.mode == "dead":
        info = run_dead(a, rec, overlay, workdir, closed_proof or {})
    else:
        info = run_mode(a, rec, overlay, workdir)
    if srv is not None:
        srv.shutdown()

    rows = list(rec.requests)
    summary = {
        "header": header,
        "run": info,
        "request_count": len(rows),
        "requests": truncate(rows),
        "arrival_times": arrival_times(rows),
        "metric_names": metric_names(rows),
        "log_event_names": log_event_names(rows),
        "attribute_keys": attribute_keys(rows),
        "probes": probe(rows),
    }
    (workdir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({k: v for k, v in summary.items() if k != "requests"},
                     indent=2, default=str), flush=True)
    print(json.dumps({"wrote": str(workdir / "summary.json"),
                      "raw_bodies": str(workdir / "raw")}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
