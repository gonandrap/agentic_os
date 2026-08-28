"""Shared test/eval fixtures: isolated JARVIS_HOME, fixture git projects, and a fake
`claude` CLI that mimics the supervisor's observed behavior (bg roster, session ids,
resume semantics, headless -p calls).

Used by tests/, evals/, and tests_browser/ via their conftest re-exports, plus
`gate_test_environment` — the isolation gate the repo-root conftest installs before
collection. See that function for what a test run is not allowed to reach, and
tests/test_isolation_gate.py for the proof that it can't."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from . import agent_usage, systemd_units
from .bugreport import GH_BIN_ENV
from .claude_cli import CLAUDE_BIN_ENV, CREDENTIALS_ENV
from .notify import DISABLE_EXTERNAL_SINKS_ENV

#: Sandbox root the gate installed, or None if `gate_test_environment` never ran.
#: Tests assert against it; nothing in production reads it.
GATE_ROOT: Path | None = None

#: The process-wide floor home the gate installed. The autouse `jarvis_home` fixture
#: overrides `$JARVIS_HOME` per test, so this is what code running *outside* a test body
#: (collection, module import, a subprocess spawned at import time) sees.
GATE_HOME: Path | None = None

#: A `gh` that refuses to run. Points JARVIS_GH_BIN somewhere harmless so a test that
#: forgets the `fake_gh` fixture cannot file a public issue on the OS's real tracker.
BLOCKED_GH = r'''#!/usr/bin/env python3
import sys
sys.stderr.write(
    "blocked by the Jarvis test-isolation gate: this is a test run, and the real `gh` "
    "would file a real issue on a real tracker. Use the `fake_gh` fixture.\n")
sys.exit(1)
'''

#: Likewise for `claude`. A test that forgets the `fake_claude` fixture would otherwise
#: spawn REAL background Claude Code agents against real projects and bill real tokens.
BLOCKED_CLAUDE = r'''#!/usr/bin/env python3
import sys
sys.stderr.write(
    "blocked by the Jarvis test-isolation gate: this is a test run, and the real "
    "`claude` would spawn real agents and bill real tokens. Use the `fake_claude` "
    "fixture (or set JARVIS_EVALS_LLM=1 for the opt-in LLM evals).\n")
sys.exit(1)
'''

#: Opt-in flag for the LLM-graded evals, which exist precisely to call the real model.
#: When it is set, the gate leaves `claude` alone — the run has asked for the real thing.
LLM_EVALS_ENV = "JARVIS_EVALS_LLM"


def gate_test_environment(root: Path | None = None) -> Path:
    """Make this process incapable of reaching production. Returns the sandbox root.

    Called from `pytest_configure` in the repo-root conftest — before collection, so it
    covers module import time as well as test bodies, and it is inherited by every
    subprocess a test spawns (workers, the daemon, a nested pytest).

    Every escape route a test run has to the world outside its tmp dir:

    * ``JARVIS_HOME`` — central `os.db`, `neo.db`, logs, the daemon pidfile. A worker
      session inherits the PRODUCTION home, so without this the suite writes live state,
      and the live daemon then routes whatever it finds in the central inbox to the real
      sinks. That is not hypothetical: it Telegrammed the user twice on 2026-07-27.
    * the Telegram and desktop sinks — see `notify.DISABLE_EXTERNAL_SINKS_ENV`.
    * ``gh`` — `jarvis bug report` files a GitHub issue on a PUBLIC tracker.
    * ``claude`` — a test that forgets `fake_claude` would spawn real background agents
      against real projects and bill real tokens. Left alone when `JARVIS_EVALS_LLM` is
      set, since the LLM-graded evals exist to call the real model.
    * ``JARVIS_SPEND_HOME`` — where `agent_usage` files token-accounting rows. Redirected
      with the home in every ordinary run, so a suite against the fake `claude` cannot
      write invented spend into live state. Lifted only when the run BOTH reaches the
      real model and belongs to a work order — see `_bills_real_tokens`, and note it
      opens the usage-row write path alone, not the store or the sinks.

    The gate is a floor, not a substitute for per-test isolation: the autouse
    `jarvis_home` fixture still gives each test its own home under its own tmp_path, so
    two tests that never name the fixture cannot collide in a shared sandbox.
    """
    global GATE_ROOT, GATE_HOME
    if root is None:
        root = Path(tempfile.mkdtemp(prefix="jarvis-test-gate-"))
    env = gate_environment(root)
    home = Path(env["JARVIS_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    os.environ.update(env)
    GATE_ROOT, GATE_HOME = root, home
    return root


def gate_environment(root: Path) -> dict[str, str]:
    """The environment that makes a process incapable of reaching production.

    Split out from `gate_test_environment` so it can be asserted on without mutating
    this process. Writes the stub binaries into `root`; touches nothing else.
    """
    root.mkdir(parents=True, exist_ok=True)
    env = {
        "JARVIS_HOME": str(root / "jarvis-home"),
        DISABLE_EXTERNAL_SINKS_ENV: "1",
        GH_BIN_ENV: str(_blocked_bin(root, "gh", BLOCKED_GH)),
        # Worker turns stay on the plain-`Popen` transport for the whole suite. Without
        # this the auto-detection would be RIGHT and that is the problem: a suite run by
        # a Jarvis worker inherits the daemon's `.service` cgroup, so every fake-`claude`
        # turn would register a real transient unit on the developer's machine. The
        # systemd path is exercised by pointing `JARVIS_SYSTEMD_RUN_BIN` at a fake, which
        # is the same shape as the `claude` and `gh` stubs above.
        systemd_units.TRANSPORT_ENV: systemd_units.DIRECT,
        # The developer's own Claude Code sign-in, which `worker_session._auth_retry_at`
        # reads to decide whether an auth-paused turn may go again. Left ambient, a suite
        # run on a machine whose credentials were refreshed a second ago would resume
        # every fixture's auth pause — passing or failing on the state of the human's
        # login. Pointed at a path inside the sandbox that no test writes unless it means
        # to, so the default answer is "cannot tell, do not resume".
        CREDENTIALS_ENV: str(root / "claude-credentials.json"),
    }
    if not os.environ.get(LLM_EVALS_ENV):
        env[CLAUDE_BIN_ENV] = str(_blocked_bin(root, "claude", BLOCKED_CLAUDE))
    if not _bills_real_tokens():
        # Token accounting is redirected with everything else by default, so a suite
        # running against the fake `claude` cannot write invented rows into live state.
        env[agent_usage.SPEND_HOME_ENV] = str(root / "jarvis-home")
    return env


def _bills_real_tokens() -> bool:
    """Is this run spending real money that a real work order will be charged for?

    The one carve-out in the gate's accounting redirect, and it needs BOTH halves.
    `JARVIS_EVALS_LLM` says the run reaches the real model — the same signal that
    already stops the gate replacing the `claude` binary, for the same reason: these
    evals exist to call it. `JARVIS_WO_ID` says a work order is paying, which is who
    the row would be filed against. An ad-hoc eval run by a human has no work order and
    stays fully sandboxed; a worker's eval run has both and its spend reaches the ledger
    that the `jarvis cost` for that work order reads (ruled on wo-76e021aa, issue #103).

    Deliberately narrow: this opens the usage-row write path and nothing else. Every
    other route out of the sandbox — the notification sinks, `gh`, the rest of the
    central store — stays shut in both cases.
    """
    return bool(os.environ.get(LLM_EVALS_ENV) and os.environ.get("JARVIS_WO_ID"))


def _blocked_bin(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.write_text(source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def teardown_test_environment() -> None:
    """Remove the gate's sandbox. Called from `pytest_unconfigure`."""
    global GATE_ROOT, GATE_HOME
    if GATE_ROOT is not None:
        shutil.rmtree(GATE_ROOT, ignore_errors=True)
        GATE_ROOT = GATE_HOME = None


FAKE_CLAUDE = r'''#!/usr/bin/env python3
"""Fake `claude` CLI for tests.

Records every invocation to $FAKE_CLAUDE_DIR/calls.jsonl and keeps a background-session
roster in $FAKE_CLAUDE_DIR/sessions.json that `agents --json` serves back.
"""
import json, os, sys, time

state_dir = os.environ["FAKE_CLAUDE_DIR"]
calls_dir = os.path.join(state_dir, "calls")
turns_dir = os.path.join(state_dir, "turns")
sessions_path = os.path.join(state_dir, "sessions.json")

def load_sessions():
    try:
        with open(sessions_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_sessions(s):
    with open(sessions_path, "w") as f:
        json.dump(s, f)

argv = sys.argv[1:]
# One file per invocation, never a shared append target: worker turns are concurrent
# detached processes and a worker prompt is far past the 4KB atomic-append ceiling, so
# a single calls.jsonl loses and interleaves records under any real fan-out.
os.makedirs(calls_dir, exist_ok=True)
# The prompt-cache TTL is decided by the launch ENVIRONMENT and by nothing in argv, so a
# call record of argv alone cannot test the rate Jarvis pays. Only these two keys: the
# whole environment would spill every secret the daemon holds into a fixture on disk.
with open(os.path.join(calls_dir, f"{time.time_ns()}-{os.getpid()}.json"), "w") as f:
    json.dump({"argv": argv, "cwd": os.getcwd(),
               "cache_env": {k: os.environ[k] for k in
                             ("FORCE_PROMPT_CACHING_5M", "ENABLE_PROMPT_CACHING_1H")
                             if k in os.environ}}, f)

def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

def emit_headless(result_text):
    """A one-shot `claude -p --output-format json` envelope, usage included.

    THE USAGE OBJECT IS NOT DECORATION. Every OS-side call (Neo, a panel seat, a digest)
    reads it back and records what the call cost against the work order that caused it
    (`agent_usage`), so a fake that emitted only `result` would leave that whole path
    untested — and the numbers here are what the accounting assertions are written
    against. Same field shape as the worker-turn envelope above, one API call's worth.
    """
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "sess-headless", "result": result_text,
        "num_turns": 1, "total_cost_usd": 0.002, "duration_api_ms": 400,
        "usage": {
            "input_tokens": 5, "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 800, "output_tokens": 60,
            "cache_creation": {"ephemeral_1h_input_tokens": 0,
                               "ephemeral_5m_input_tokens": 200},
            "iterations": [{"type": "message", "input_tokens": 5, "output_tokens": 60,
                            "cache_read_input_tokens": 800,
                            "cache_creation_input_tokens": 200}],
        },
        "modelUsage": {opt("--model", "claude-fake-1"): {
            "inputTokens": 5, "outputTokens": 60, "cacheReadInputTokens": 800,
            "cacheCreationInputTokens": 200, "costUSD": 0.002,
            "contextWindow": 200000, "maxOutputTokens": 32000}},
    }))

if "--version" in argv:
    print("9.9.9 (fake claude)")
elif argv[:1] == ["agents"]:
    print(json.dumps(load_sessions()))
elif "--bg" in argv:
    # Let a test fail only the bg resume-FORK, so the daemon's headless-resume
    # fallback becomes reachable (FAKE_CLAUDE_RESUME would fail both).
    if "--resume" in argv and os.environ.get("FAKE_CLAUDE_BG_RESUME") == "fail":
        sys.stderr.write("bg resume-fork failed (test-forced)\n"); sys.exit(1)
    # like the real supervisor: assigns its own session id (ignores --session-id);
    # with --resume it forks the conversation under a fresh session id
    import hashlib
    sessions = load_sessions()
    name = opt("--name", "")
    resumed = opt("--resume")
    seed = name + (resumed or "") + str(len(sessions))
    sid = "sess-" + hashlib.sha1(seed.encode()).hexdigest()[:12]
    job_id = sid[5:13]
    # The conversation exists from the moment the session does, and outlives the agent
    # that held it — which is what makes it resumable after `claude stop`.
    os.makedirs(turns_dir, exist_ok=True)
    open(os.path.join(turns_dir, sid + ".jsonl"), "a").close()
    sessions.append({
        "id": job_id,
        "sessionId": sid,
        "cwd": os.getcwd(),
        "kind": "background",
        "name": name,
        # Claude Code's vocabulary, not Jarvis's: a live agent is "working". Emitting
        # "running" here (the work-order status word) once hid a real bug for weeks —
        # the daemon compared against it and every healthy worker read as blocked.
        "state": "working",
        "startedAt": 0,
        "resumedFrom": resumed,
        "prompt": argv[-1][:40],
    })
    save_sessions(sessions)
    # Job state the daemon polls for a turn's final assistant message (internal-format
    # stand-in). The supervisor publishes one per bg job. A forked (--resume) turn is a
    # single short exchange and lands right away; an initial dispatch stays working
    # until the test flips the session to done via set_session_state.
    jobs_root = os.environ.get("JARVIS_CLAUDE_JOBS_DIR")
    if jobs_root:
        jdir = os.path.join(jobs_root, job_id)
        os.makedirs(jdir, exist_ok=True)
        state = ({"state": "done", "output": {"result": f"ack: {argv[-1][:40]}"}}
                 if resumed else {"state": "working"})
        with open(os.path.join(jdir, "state.json"), "w") as f:
            json.dump(state, f)
    print(f"  claude stop {job_id}      stop this session")
elif argv[:1] == ["stop"]:
    sessions = load_sessions()
    remaining = [s for s in sessions if s["id"] != argv[1]]
    if len(remaining) == len(sessions):
        sys.stderr.write(f"no such session {argv[1]}\n"); sys.exit(1)
    # Releasing the agent does not delete the conversation: it stays resumable, which
    # is the whole basis of the migration path off background sessions.
    os.makedirs(turns_dir, exist_ok=True)
    for s in sessions:
        if s["id"] == argv[1]:
            open(os.path.join(turns_dir, s["sessionId"] + ".jsonl"), "a").close()
    save_sessions(remaining)
elif "-p" in argv and ("--session-id" in argv or "--resume" in argv):
    # A WORKER TURN: `claude -p --session-id|--resume <sid> [briefing] -- <prompt>`.
    # Mirrors the real CLI's two load-bearing properties (verified against 2.1.220):
    # the id passed in is the id that comes back, on the opening turn AND on every
    # resume — a headless resume does not fork.
    sid = opt("--session-id") or opt("--resume")
    prompt = argv[-1]
    os.makedirs(turns_dir, exist_ok=True)
    log = os.path.join(turns_dir, sid + ".jsonl")
    if "--resume" in argv and not os.path.exists(log):
        sys.stderr.write(f"No conversation found with session ID: {sid}\n")
        sys.exit(1)
    # Like the real CLI: a session a background agent still owns cannot be resumed.
    if "--resume" in argv and any(s["sessionId"] == sid for s in load_sessions()):
        sys.stderr.write(
            f"Error: Session {sid} is currently running as a background agent (bg).\n")
        sys.exit(1)
    # THE USAGE LIMIT, AND IT REFUSES BEFORE THE TRANSCRIPT IS WRITTEN — which is the
    # property the retry path turns on. The real CLI never reaches the API (0ms, $0), so
    # an opening turn refused this way leaves no session to `--resume`, and
    # `worker_session.retry` has to re-open with `--session-id` instead. Writing the log
    # first here would make the fake's opening refusal resumable and quietly hide that.
    if os.environ.get("FAKE_CLAUDE_TURN") == "rate_limit":
        reset = os.environ.get("FAKE_CLAUDE_LIMIT_RESET",
                               "11:50pm (America/Los_Angeles)")
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": True,
            "session_id": sid, "num_turns": 1, "total_cost_usd": 0,
            "duration_api_ms": 0,
            "terminal_reason": "api_error", "api_error_status": 429,
            # Verbatim from wo-2fa7c0e9 turn 4, middle dot included.
            "result": "You've hit your session limit · resets " + reset,
        }))
        sys.exit(0)
    with open(log, "a") as f:
        f.write(json.dumps(prompt) + "\n")
    seq = sum(1 for _ in open(log))
    # A turn the test wants to observe mid-flight: block until the file is removed.
    hold = os.environ.get("FAKE_CLAUDE_TURN_HOLD")
    if hold:
        while os.path.exists(hold):
            time.sleep(0.02)
    if os.environ.get("FAKE_CLAUDE_TURN") == "fail":
        sys.stderr.write("turn failed (test-forced)\n"); sys.exit(1)
    if os.environ.get("FAKE_CLAUDE_TURN") == "silent":
        sys.exit(0)  # process ends writing nothing — a crashed turn
    # THE AUTH REFUSAL, AND THE WHOLE POINT IS WHERE IT WRITES. `claude -p` exits with
    # NO result JSON and NO stderr — indistinguishable from `silent` out here — and puts
    # the reason in its own session transcript as a `<synthetic>` assistant message. A
    # fake that wrote to stdout or stderr would test a path the real incident never took
    # (wo-c2793bf0, 2026-08-27; kn-8d466c3d).
    if os.environ.get("FAKE_CLAUDE_TURN") == "auth":
        root = os.environ.get("JARVIS_TRANSCRIPT_ROOT")
        if root:
            said = os.environ.get(
                "FAKE_CLAUDE_AUTH_MESSAGE",
                "Failed to authenticate: OAuth session expired and could not be refreshed")
            d = os.path.join(root, "-fake")
            os.makedirs(d, exist_ok=True)
            # Sub-second, because the window it has to land inside is one turn of a test
            # that takes milliseconds: a stamp floored to the second lands BEFORE
            # `started_at` and `usage.said_in_session` drops it.
            now = time.time()
            stamp = (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
                     + f".{int(now % 1 * 1000):03d}Z")
            with open(os.path.join(d, sid + ".jsonl"), "a") as f:
                f.write(json.dumps({
                    "type": "assistant", "timestamp": stamp,
                    "message": {"id": f"synth-{seq}", "model": "<synthetic>",
                                "content": [{"type": "text", "text": said}],
                                "usage": {"input_tokens": 0, "output_tokens": 0}},
                }) + "\n")
        sys.exit(0)
    if os.environ.get("FAKE_CLAUDE_TURN") == "error":
        print(json.dumps({"type": "result", "subtype": "error_during_execution",
                          "is_error": True, "result": "model call failed",
                          "session_id": sid}))
        sys.exit(0)
    # THE TRANSPORT BREAKING MID-TURN, and note where this sits: BELOW the transcript
    # write, unlike the usage-limit refusal above it. That is the property under test.
    # A 500 reaches us after the turn has run — the prompt is in the conversation and
    # real work may already be done — which is exactly why the retry must nudge the
    # worker to continue rather than re-send the prompt. A fake that refused this one
    # early too would make both branches look identical and prove nothing.
    if os.environ.get("FAKE_CLAUDE_TURN") == "api_error":
        status = int(os.environ.get("FAKE_CLAUDE_API_ERROR_STATUS", "500"))
        print(json.dumps({
            "type": "result", "subtype": "success", "is_error": True,
            "session_id": sid, "num_turns": 3, "total_cost_usd": 0.42,
            # Non-zero, and load-bearing: `worker_session._reached_model` reads it to
            # decide between the nudge and the verbatim re-send.
            "duration_api_ms": 177098,
            "terminal_reason": "api_error", "api_error_status": status,
            "usage": {"input_tokens": 12, "output_tokens": 34},
            # The shape the CLI assembles for a 5xx, verbatim from wo-4f460495 turn 2.
            "result": f"API Error: {status} Internal server error. This is a "
                      "server-side issue, usually temporary — try again in a "
                      "moment. If it persists, check https://status.claude.com.",
        }))
        sys.exit(0)
    # The usage envelope mirrors the real CLI's result JSON (verified against a live
    # turn), so the whole reap-and-record path runs against the true field shape in
    # every pipeline test — `iterations` is one entry per API call, and the context
    # at a call is its input + cache_read + cache_creation.
    #
    # `modelUsage` IS DELIBERATELY LARGER THAN `usage`, in the same proportion the real
    # CLI reports (measured over 186 live result files: the top-level object runs at
    # 33-60% of the turn's true total, because it speaks for the tail of the turn while
    # `modelUsage` speaks for all of it). A fake whose two agreed is what let the OS
    # record the wrong one for months: every fixture agreed, so no test could tell which
    # was being read. Keep them apart — three API calls' worth against one call's tail.
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": sid, "result": f"final: {prompt[:60]}",
        "num_turns": seq, "total_cost_usd": 0.01,
        "duration_api_ms": 1200, "duration_ms": 1500,
        "usage": {
            "input_tokens": 3, "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 2000, "output_tokens": 100,
            "service_tier": "standard",
            "cache_creation": {"ephemeral_1h_input_tokens": 1000,
                               "ephemeral_5m_input_tokens": 0},
            "iterations": [{"type": "message", "input_tokens": 3,
                            "output_tokens": 100,
                            "cache_read_input_tokens": 2000,
                            "cache_creation_input_tokens": 1000,
                            "cache_creation": {"ephemeral_1h_input_tokens": 1000,
                                               "ephemeral_5m_input_tokens": 0}}],
        },
        "modelUsage": {"claude-fake-1": {
            "inputTokens": 9, "outputTokens": 300, "cacheReadInputTokens": 6000,
            "cacheCreationInputTokens": 3000, "costUSD": 0.01,
            "contextWindow": 200000, "maxOutputTokens": 32000}},
    }))
elif "-p" in argv and "--resume" not in argv:
    # headless one-shot (`claude -p ...`) — Neo's answering path. Deterministic
    # verdict driven by the prompt so tests control escalation.
    prompt = argv[argv.index("-p") + 1]
    system = opt("--append-system-prompt", "")
    # A VALIDATION SEAT, AND THIS BRANCH IS FIRST OF ALL. `chair` IS A LEGAL SEAT NAME IN
    # BOTH ROSTERS: a validator chair answered by the Neo seat branch below comes back a
    # perfectly well-formed Neo verdict carrying no pass and no reject at all, and a
    # lenient `validation.decide` would read that as a pass — a green suite that exercised
    # nothing. The two headers are different literals precisely so this branch can tell
    # them apart, and the per-seat failure variable is separate for the same reason.
    vseat = next((s for s in ("tester", "security", "architect", "maintainer", "chair")
                  if ("# Jarvis validation seat: " + s) in system), None)
    if vseat:
        if vseat in [s for s in
                     os.environ.get("FAKE_VALIDATION_SEAT_FAIL", "").split(",") if s]:
            sys.stderr.write(f"validation seat {vseat} failed (test-forced)\n")
            sys.exit(1)
        if f"FORCE_VALIDATION_GARBAGE_{vseat.upper()}" in prompt:
            emit_headless(f"on reflection the {vseat} question is a hard one")
            sys.exit(0)
        if vseat == "chair":
            # The chair's schema is `outcome`/`reason` and NOTHING else — no `escalate`,
            # no `answer`. That difference from the Neo chair's reply is what a test
            # asserts to prove this branch, and not the one below, answered the call.
            reply = {"outcome": "passed", "reason": ""}
            if "FORCE_VALIDATION_REJECT" in prompt:
                reply = {"outcome": "rejected",
                         "reason": "your change is not covered by the evidence you "
                                   "declared."}
            elif "FORCE_VALIDATION_NO_OUTCOME" in prompt:
                reply = {"reason": "a reply with no outcome at all"}
        else:
            reply = {"verdict": "pass", "blocking": False,
                     "reason": f"the {vseat} question is answered by this change",
                     "asks": []}
            if f"FORCE_BLOCK_{vseat.upper()}" in prompt:
                # `blocking` true from EVERY seat, veto-holder or not. Arbitration, not
                # the seat, decides what that forces — which is exactly the row the
                # architect and maintainer tests need staged.
                reply = {"verdict": "reject", "blocking": True,
                         "reason": f"test-forced {vseat} objection",
                         "asks": [f"answer the {vseat} objection"]}
            elif f"FORCE_REJECT_{vseat.upper()}" in prompt:
                reply = {"verdict": "reject", "blocking": False,
                         "reason": f"test-forced {vseat} concern that blocks nothing",
                         "asks": []}
        emit_headless(json.dumps(reply))
        sys.exit(0)
    # A DASHBOARD DIGEST, AND IT COMES FIRST FOR THE SAME REASON THE SEAT BRANCH DOES:
    # the call is identified by its system prompt, never by the user prompt — which is
    # the worker's question verbatim, so a digest of a gate question would otherwise
    # fall through to the gate branch below and answer with a verdict.
    if "# Jarvis dashboard digest" in system:
        if "FORCE_DIGEST_FAIL" in prompt:
            sys.stderr.write("digest call failed (test-forced)\n"); sys.exit(1)
        if "FORCE_DIGEST_GARBAGE" in prompt:
            # No `headline`: the shape the validator must refuse. `structured.request`
            # retries once and then raises, which is what the daemon records.
            emit_headless(json.dumps({"bullets": ["a", "b"]}))
            sys.exit(0)
        head = prompt.strip().splitlines()[0][:80]
        emit_headless(json.dumps({
            "headline": f"digest of: {head}",
            # Seven, so a test can prove the FIVE-item cap is enforced in the validator
            # and not merely requested in the prompt.
            "bullets": [f"point {i}" for i in range(1, 8)],
            "options": ["option A — cheap", "option B — thorough"],
            "recommendation": "option A",
        }))
        sys.exit(0)
    # A PANEL SEAT, AND THIS BRANCH COMES FIRST DELIBERATELY. Seat identity travels in
    # --append-system-prompt, never in the user prompt: a premise-seat call on a gate
    # question carries "PRIVILEGED ACTION REQUEST" in its prompt, so without this the
    # branch below would answer it with a well-formed gate verdict AND NO `route` KEY —
    # after which a lenient `panel.decide` defaults the route and the test passes having
    # exercised nothing.
    seat = next((s for s in ("premise", "record", "blast", "taste", "chair")
                 if ("# Neo panel seat: " + s) in system), None)
    if seat:
        # Per-seat failure, NOT the shared FORCE_FAIL below: that one keys on the user
        # prompt, which every seat on one question shares, so it could only ever fail all
        # of them at once. A degradation test needs to fail exactly one.
        if seat in [s for s in os.environ.get("FAKE_SEAT_FAIL", "").split(",") if s]:
            sys.stderr.write(f"seat {seat} failed (test-forced)\n"); sys.exit(1)
        if "FORCE_SEAT_GARBAGE" in prompt and seat == "premise":
            emit_headless("the premise here is, well, hard to say")
            sys.exit(0)
        tail = prompt.splitlines()[-1][:60]
        # NOTE: no seat name appears in any `answer` below, and that is load-bearing
        # rather than tidy. Panel deliberation must never reach the worker, and the test
        # that pins it asserts no seat name is in the delivered message — a canned answer
        # containing the word "chair" would fail it for the wrong reason and, worse, a
        # later loosening of that assertion would go unnoticed.
        if seat == "premise":
            reply = {"escalate": False, "answer": f"first-read decision on: {tail}",
                     "reason": "premise finding", "route": "panel"}
            if "FORCE_ROUTE_FAST" in prompt:
                reply["route"] = "fast"
            if "FORCE_NO_ROUTE" in prompt:
                reply.pop("route")
            if "FORCE_PROPOSE_DISMISS" in prompt:
                reply["verdict"] = "dismiss"
            elif "FORCE_PROPOSE_APPROVE" in prompt:
                reply["verdict"] = "approve"
            if "FORCE_FRAME_ESCALATE" in prompt:
                reply = {"escalate": True, "answer": "", "route": "panel",
                         "reason": "test-forced escalation on the framing"}
        elif seat == "chair":
            reply = {"escalate": False, "answer": f"synthesised decision on: {tail}",
                     "reason": "the panel's verdict"}
            if "FORCE_CHAIR_ESCALATE" in prompt:
                reply = {"escalate": True, "answer": "",
                         "reason": "test-forced escalation by the panel"}
            elif "FORCE_CHAIR_APPROVE" in prompt:
                reply["verdict"] = "approve"
            elif "FORCE_CHAIR_DISMISS" in prompt:
                reply["verdict"] = "dismiss"
        else:
            # `record`, `blast` and `taste`. No verdict of their own — the arbitration
            # reads `escalate`, `veto` and `contradiction`, never a verdict — but a reply
            # that differs per seat, so "the seats deliberated" is provable rather than
            # assumed.
            #
            # NO SEAT NAME APPEARS IN ANY OF THESE, and for these three that is
            # load-bearing twice over: the delivered message must name no seat, AND a
            # forced escalation delivers the forcing seat's own `reason` verbatim, so a
            # canned reason reading "blast finding" would fail the no-leak assertion for
            # the fake's prose instead of for the code's.
            finding = {"record": "what was already settled",
                       "blast": "what it costs if wrong",
                       "taste": "what the user meant"}[seat]
            reply = {"escalate": False, "answer": f"{finding}, for: {tail}",
                     "reason": f"a reading of {finding}"}
            if seat == "blast" and "FORCE_RADIUS_ESCALATE" in prompt:
                reply = {"escalate": True, "veto": False, "answer": "",
                         "reason": "test-forced escalation on the cost of being wrong"}
            elif seat == "blast" and "FORCE_RADIUS_VETO" in prompt:
                reply = {"escalate": False, "veto": True, "answer": "",
                         "reason": "test-forced veto of the proposal on the table"}
            elif seat == "record" and "FORCE_LEDGER_CONTRADICTION" in prompt:
                reply = {"escalate": False, "contradiction": "unresolvable", "answer": "",
                         "reason": "test-forced unresolvable contradiction"}
            elif seat == "taste" and "FORCE_INTENT_ESCALATE" in prompt:
                reply = {"escalate": True, "veto": True, "answer": "",
                         "reason": "test-forced objection that must force nothing"}
        emit_headless(json.dumps(reply))
        sys.exit(0)
    if "FORCE_FAIL" in prompt:
        sys.stderr.write("model call failed (test-forced)\n"); sys.exit(1)
    if "FORCE_ESCALATE" in prompt:
        verdict = {"escalate": True, "answer": "",
                   "reason": "test-forced escalation"}
    elif "FORCE_GARBAGE" in prompt:
        emit_headless("I think you should maybe do the thing?")
        sys.exit(0)
    elif "PRIVILEGED ACTION REQUEST" in prompt:
        # A gate review, which speaks a different verdict shape: the decision lives in
        # `verdict`, not in prose. Default is to escalate, matching the real reviewer's
        # instruction to send anything it cannot verify to the user — a fake that
        # approved by default would let every gate test pass without asserting anything.
        if "FORCE_APPROVE" in prompt:
            verdict = {"escalate": False, "verdict": "approve",
                       "reason": "test-forced approval"}
        elif "FORCE_DENY" in prompt:
            verdict = {"escalate": False, "verdict": "deny",
                       "reason": "test-forced denial"}
        elif "FORCE_DISMISS" in prompt:
            verdict = {"escalate": False, "verdict": "dismiss",
                       "reason": "test-forced dismissal: not a privileged action"}
        elif "FORCE_LEGACY_APPROVE" in prompt:
            # An older Neo that has never heard of `verdict`. Kept as a distinct switch
            # because the two contracts must coexist across a release: the persona ships
            # in the code, Neo's learnings live in the production state directory.
            verdict = {"escalate": False, "approve": True,
                       "reason": "test-forced approval, pre-`verdict` shape"}
        else:
            verdict = {"escalate": True, "verdict": "deny",
                       "reason": "test default: gate reviews escalate unless forced"}
    elif "Release this plan?" in prompt:
        # A feature order's plan review — the third question kind, and the third verdict
        # shape. Same defaulting rule as the gate above and for the same reason: a fake
        # that released plans by default would let a feature-order test pass while
        # asserting nothing about the review.
        if "FORCE_APPROVE" in prompt:
            verdict = {"escalate": False, "verdict": "approve",
                       "reason": "test-forced plan release"}
        elif "FORCE_REJECT" in prompt:
            verdict = {"escalate": False, "verdict": "reject",
                       "reason": "test-forced rejection: child two needs more context"}
        else:
            verdict = {"escalate": True, "verdict": "reject",
                       "reason": "test default: plan reviews escalate unless forced"}
    else:
        verdict = {"escalate": False,
                   "answer": f"neo-decision for: {prompt.splitlines()[-1][:60]}",
                   "reason": "test verdict"}
    if "FORCE_DISPATCH" in prompt:
        # Neo spotting a self-contradicting ledger and filing the pre-approved cleanup.
        # Rides on top of whatever verdict was chosen above, because the real thing does
        # too: answering and noticing the record is wrong are independent.
        verdict["dispatch"] = {"title": "test-forced ledger cleanup",
                               "description": "entries A and B contradict; B won"}
    emit_headless(json.dumps(verdict))
else:
    sys.stderr.write(f"fake claude: unhandled argv {argv}\n"); sys.exit(2)
'''


FAKE_GH = r'''#!/usr/bin/env python3
"""Fake `gh` CLI for tests: records invocations, files issues, serves PR states."""
import json, os, sys

state_dir = os.environ["FAKE_GH_DIR"]
argv = sys.argv[1:]
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
with open(os.path.join(state_dir, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")

fail = os.environ.get("FAKE_GH_FAIL")
if fail:
    sys.stderr.write(fail + "\n")
    sys.exit(1)
if argv[:2] == ["issue", "create"]:
    print(os.environ["FAKE_GH_ISSUE_URL"])
elif argv[:2] == ["pr", "view"]:
    # `gh pr view <url> --json <fields>`. The roster comes from the fixture; a URL
    # nobody registered gets gh's own "no pull requests found" shape, because a test
    # about an unreadable PR should exercise the same path a real deleted one does.
    prs = json.loads(os.environ.get("FAKE_GH_PRS", "{}"))
    pr = prs.get(argv[2] if len(argv) > 2 else "")
    if pr is None:
        sys.stderr.write("no pull requests found for this URL\n")
        sys.exit(1)
    print(json.dumps(pr))
else:
    sys.stderr.write(f"fake gh: unhandled argv {argv}\n")
    sys.exit(2)
'''


FAKE_SYSTEMD_RUN = r"""#!/usr/bin/env python3
'''Fake `systemd-run --user` for tests.

Stands in for the real thing on the ONE property the transient-unit transport rests on:
the command it starts does not inherit this process's environment. It gets a deliberately
bare base plus exactly what `--setenv=` carried, which is what makes a test able to prove
that JARVIS_HOME, the PATH the fleet depends on and the prompt-cache flag reach a worker
turn — the failure mode a real systemd would only show in production.

Registers the unit in $FAKE_SYSTEMD_DIR/units/<unit>.json so the fake `systemctl` beside
it can answer MainPID/ActiveState and stop it. $FAKE_SYSTEMD_FAIL makes every call fail,
which is how the fallback-to-Popen path is tested.
'''
import json, os, subprocess, sys

state_dir = os.environ["FAKE_SYSTEMD_DIR"]
units_dir = os.path.join(state_dir, "units")
os.makedirs(units_dir, exist_ok=True)
argv = sys.argv[1:]
with open(os.path.join(state_dir, "run-calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": argv}) + "\n")

fail = os.environ.get("FAKE_SYSTEMD_FAIL")
if fail:
    sys.stderr.write(fail + "\n")
    sys.exit(1)

unit, workdir, stdout, stderr, setenv = None, None, None, None, {}
rest = []
i = 0
while i < len(argv):
    a = argv[i]
    if a == "--":
        rest = argv[i + 1:]
        break
    if a.startswith("--unit="):
        unit = a.split("=", 1)[1]
    elif a.startswith("--working-directory="):
        workdir = a.split("=", 1)[1]
    elif a.startswith("--setenv="):
        k, _, v = a.split("=", 1)[1].partition("=")
        setenv[k] = v
    elif a.startswith("--property=StandardOutput=file:"):
        stdout = a.split("file:", 1)[1]
    elif a.startswith("--property=StandardError=file:"):
        stderr = a.split("file:", 1)[1]
    i += 1

if not unit or not rest:
    sys.stderr.write(f"fake systemd-run: unusable argv {argv}\n"); sys.exit(2)

# A transient unit inherits the systemd USER MANAGER's environment, not the caller's.
env = {k: os.environ[k] for k in ("PATH", "HOME", "XDG_RUNTIME_DIR", "LANG")
       if k in os.environ}
env.update(setenv)
out = open(stdout, "w") if stdout else subprocess.DEVNULL
err = open(stderr, "w") if stderr else subprocess.DEVNULL
proc = subprocess.Popen(rest, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                        stdout=out, stderr=err, start_new_session=True)
with open(os.path.join(units_dir, unit + ".json"), "w") as f:
    json.dump({"unit": unit, "pid": proc.pid, "argv": rest, "cwd": workdir,
               "setenv": setenv}, f)
"""


FAKE_SYSTEMCTL = r"""#!/usr/bin/env python3
'''Fake `systemctl --user` for tests: answers about units the fake systemd-run made.

Only the three verbs the turn transport uses — `show -p MainPID`, `show -p ActiveState`
and `stop`. An unknown unit answers exactly as a `--collect`ed one does: MainPID 0,
ActiveState inactive. That is the normal case after a turn finishes, not an error.
'''
import json, os, signal, sys

state_dir = os.environ["FAKE_SYSTEMD_DIR"]
units_dir = os.path.join(state_dir, "units")
argv = [a for a in sys.argv[1:] if a not in ("--user", "--no-block")]
with open(os.path.join(state_dir, "ctl-calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")

def load(unit):
    try:
        with open(os.path.join(units_dir, unit + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A detached child nobody waits on is a zombie, and signalling one succeeds.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except OSError:
        return True

if argv[:1] == ["show"]:
    rec = load(argv[1])
    up = bool(rec) and alive(rec["pid"])
    if "--property=MainPID" in argv:
        print(rec["pid"] if up else 0)
    elif "--property=ActiveState" in argv:
        print("active" if up else "inactive")
    else:
        sys.stderr.write(f"fake systemctl: unhandled show {argv}\n"); sys.exit(2)
elif argv[:1] == ["stop"]:
    rec = load(argv[1])
    if rec and alive(rec["pid"]):
        try:
            os.killpg(os.getpgid(rec["pid"]), signal.SIGTERM)
        except OSError:
            pass
else:
    sys.stderr.write(f"fake systemctl: unhandled argv {argv}\n"); sys.exit(2)
"""


@pytest.fixture()
def fake_systemd(tmp_path, monkeypatch):
    """Put worker turns on the transient-unit transport, against a fake systemd.

    The test-isolation gate pins every run to the direct transport (see
    `gate_environment`), so a test that wants the systemd path has to say so — which is
    this fixture. It flips `JARVIS_TURN_TRANSPORT` to `systemd`, so nothing here depends
    on whether the machine running the suite happens to have a real one.
    """
    sdir = tmp_path / "fake-systemd"
    (sdir / "units").mkdir(parents=True)
    run_bin, ctl_bin = sdir / "systemd-run", sdir / "systemctl"
    for path, body in ((run_bin, FAKE_SYSTEMD_RUN), (ctl_bin, FAKE_SYSTEMCTL)):
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_SYSTEMD_DIR", str(sdir))
    monkeypatch.setenv(systemd_units.SYSTEMD_RUN_BIN_ENV, str(run_bin))
    monkeypatch.setenv(systemd_units.SYSTEMCTL_BIN_ENV, str(ctl_bin))
    monkeypatch.setenv(systemd_units.TRANSPORT_ENV, systemd_units.SYSTEMD)

    class Handle:
        dir = sdir

        @property
        def runs(self) -> list[dict]:
            path = sdir / "run-calls.jsonl"
            return ([json.loads(l) for l in path.read_text().splitlines()]
                    if path.exists() else [])

        @property
        def units(self) -> dict[str, dict]:
            return {p.stem: json.loads(p.read_text())
                    for p in (sdir / "units").glob("*.json")}

        def fail(self, message: str = "fake systemd-run: refused") -> None:
            """Make every subsequent spawn fail, so the direct fallback is exercised."""
            monkeypatch.setenv("FAKE_SYSTEMD_FAIL", message)

    return Handle()


@pytest.fixture()
def fake_gh(tmp_path, monkeypatch):
    """Install a fake `gh` binary; returns a handle to its recorded state."""
    gdir = tmp_path / "fake-gh"
    gdir.mkdir()
    binpath = gdir / "gh"
    binpath.write_text(FAKE_GH)
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    url = "https://github.com/example/repo/issues/7"
    monkeypatch.setenv("FAKE_GH_DIR", str(gdir))
    monkeypatch.setenv("FAKE_GH_ISSUE_URL", url)
    monkeypatch.setenv("JARVIS_GH_BIN", str(binpath))

    class Handle:
        dir = gdir
        issue_url = url
        prs: dict[str, dict] = {}

        @property
        def calls(self) -> list[dict]:
            path = gdir / "calls.jsonl"
            if not path.exists():
                return []
            return [json.loads(l) for l in path.read_text().splitlines()]

        def fail(self, message: str) -> None:
            """Make every subsequent `gh` call fail with `message` on stderr."""
            monkeypatch.setenv("FAKE_GH_FAIL", message)

        def set_pr(self, pr_url: str, state: str, merged_at: str | None = None,
                   mergeable: str | None = None, base_ref: str = "main") -> None:
            """Register what `gh pr view <pr_url>` answers. Re-calling re-states it,
            which is how a test walks a pull request from OPEN to MERGED — or from
            MERGEABLE to CONFLICTING and back.

            `mergeable` defaults to MERGEABLE for an open pull request and to null for
            any other state, which is what GitHub itself answers."""
            if mergeable is None:
                mergeable = "MERGEABLE" if state == "OPEN" else None
            self.prs[pr_url] = {"state": state, "mergedAt": merged_at,
                                "mergeable": mergeable, "baseRefName": base_ref}
            monkeypatch.setenv("FAKE_GH_PRS", json.dumps(self.prs))

    return Handle()


@pytest.fixture(autouse=True)
def jarvis_home(tmp_path, monkeypatch):
    """Per-test central state. Autouse: isolation is not something a test can forget.

    Naming it as a parameter is still the way to get the path; the only thing autouse
    changes is that a test which does NOT name it is isolated anyway, instead of writing
    whatever `$JARVIS_HOME` the shell carried in (in a worker session: production).

    This replaces the session-scoped `isolate_jarvis_home` fixture (#30), and keeps the
    property that motivated it. Its concern was that function-scoped monkeypatching
    restores the *pre-test* value at teardown, handing the real home back to anything
    still running — a daemon thread that outlived its test, a subprocess mid-flight.
    That still holds here, because the value restored is no longer the real home: the
    root conftest's `gate_test_environment` has already overwritten `os.environ` (not
    via monkeypatch, so nothing undoes it) before collection. Teardown falls back onto
    the gate's sandbox, never onto production.

    Per-test rather than per-session so two tests that both forget the fixture cannot
    collide in one shared home.

    Token accounting follows the home unless the run is genuinely billing a work order
    for real tokens (`gate_environment._bills_real_tokens`), which no ordinary test is.
    Without moving it, a test asserting on `agent_calls` would look in its own home while
    the rows landed in the gate's shared one.
    """
    home = tmp_path / "jarvis-home"
    monkeypatch.setenv("JARVIS_HOME", str(home))
    if not _bills_real_tokens():
        monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(home))
    return home


@pytest.fixture()
def allow_external_sinks(monkeypatch):
    """Lift the external-sink kill switch for a test that must exercise sink internals.

    Only for tests that have already neutered the transport themselves — a stubbed
    `urlopen`, a fake binary. Grep for this fixture name to audit every place the gate
    is lifted; if the list ever grows past the sink-rendering tests, something is wrong.
    """
    monkeypatch.delenv(DISABLE_EXTERNAL_SINKS_ENV, raising=False)


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Install a fake `claude` binary; returns a handle to its recorded state."""
    fdir = tmp_path / "fake-claude"
    fdir.mkdir()
    (fdir / "jobs").mkdir()
    binpath = fdir / "claude"
    binpath.write_text(FAKE_CLAUDE)
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(fdir))
    monkeypatch.setenv("JARVIS_CLAUDE_BIN", str(binpath))
    monkeypatch.setenv("JARVIS_CLAUDE_JOBS_DIR", str(fdir / "jobs"))

    class Handle:
        dir = fdir

        @property
        def calls(self) -> list[dict]:
            """Every invocation, oldest first (one file each; the name is a timestamp)."""
            cdir = fdir / "calls"
            if not cdir.is_dir():
                return []
            out = []
            for path in sorted(cdir.iterdir()):
                try:
                    out.append(json.loads(path.read_text()))
                except (OSError, json.JSONDecodeError):
                    continue  # mid-write; the caller polls
            return out

        def wait_calls(self, matching, count: int = 1, timeout: float = 15.0
                       ) -> list[dict]:
            """Wait for `count` recorded calls satisfying `matching(call)`.

            A worker turn is a detached process, so it records itself a moment AFTER the
            call that launched it returns. Every argv assertion has to wait for that;
            otherwise the test is racing the process it just started.
            """
            deadline = time.monotonic() + timeout
            found: list[dict] = []
            while time.monotonic() < deadline:
                found = [c for c in self.calls if matching(c)]
                if len(found) >= count:
                    return found
                time.sleep(0.02)
            return found

        @property
        def sessions(self) -> list[dict]:
            path = fdir / "sessions.json"
            return json.loads(path.read_text()) if path.exists() else []

        @property
        def turns(self) -> dict[str, list[str]]:
            """Every headless worker turn, as {session id: [prompt, …]}.

            The key assertion this enables: one session id accumulating many turns is
            exactly what "the id does not move" looks like from outside.
            """
            tdir = fdir / "turns"
            if not tdir.is_dir():
                return {}
            return {
                path.stem: [json.loads(line) for line in
                            path.read_text().splitlines() if line]
                for path in sorted(tdir.iterdir())
            }

        def fail_seat(self, *seats: str, roster: str = "neo") -> None:
            """Make the named panel seats' calls fail, and only those.

            Per-seat rather than the shared `FORCE_FAIL`, which keys on the user prompt —
            identical across every seat on one question — and so could only fail the whole
            panel at once. Degradation is per seat: one seat abstains and the rest proceed.

            `roster` picks WHICH panel, and it is not decoration: `chair` is a legal seat
            name in Neo's roster and in the validator's, so one shared variable could not
            take a validation chair down without taking Neo's down with it — and a test
            that failed both would prove nothing about either.
            """
            env = "FAKE_SEAT_FAIL" if roster == "neo" else "FAKE_VALIDATION_SEAT_FAIL"
            monkeypatch.setenv(env, ",".join(seats))

        def turns_fail(self, mode: str = "fail") -> None:
            """Make subsequent turns fail. `fail` = non-zero exit, `silent` = exits
            writing nothing at all (a crashed process), `error` = a well-formed result
            with `is_error`, `rate_limit` = the CLI refusing the turn because the
            account's usage window is spent (see `turns_rate_limited`), `api_error` =
            the API failing mid-turn (see `turns_api_error`)."""
            monkeypatch.setenv("FAKE_CLAUDE_TURN", mode)

        def turns_rate_limited(self, reset: str = "11:50pm (America/Los_Angeles)"
                               ) -> None:
            """Refuse every subsequent turn for the usage limit, resetting at `reset`.

            Call `turns_recover()` to reopen the window — which is what the OS is
            waiting for, so it is also how a test proves the retry works rather than
            just that it happens.
            """
            monkeypatch.setenv("FAKE_CLAUDE_LIMIT_RESET", reset)
            monkeypatch.setenv("FAKE_CLAUDE_TURN", "rate_limit")

        def turns_api_error(self, status: int = 500) -> None:
            """Break every subsequent turn with an API error, AFTER it has run.

            The mirror image of `turns_rate_limited`, and the difference is the point:
            this one writes the transcript and reports API time first, so the turn it
            kills is one that really happened. `status` picks the code — pass 429 to
            check that the usage-limit path keeps its claim on that one.
            """
            monkeypatch.setenv("FAKE_CLAUDE_API_ERROR_STATUS", str(status))
            monkeypatch.setenv("FAKE_CLAUDE_TURN", "api_error")

        def turns_auth_failed(
                self, said: str = ("Failed to authenticate: OAuth session expired "
                                   "and could not be refreshed"),
                root: Path | None = None) -> Path:
            """Refuse every subsequent turn the way an expired login does — SILENTLY.

            No result JSON, no stderr: the reason goes into the session transcript as a
            `<synthetic>` message and nowhere else, which is the whole reason the OS
            could not see it. Points `JARVIS_TRANSCRIPT_ROOT` at `root` (a temp dir by
            default) and returns it, so a test can also read what was written.
            """
            root = root or (tmp_path / "auth-transcripts")
            root.mkdir(parents=True, exist_ok=True)
            monkeypatch.setenv("JARVIS_TRANSCRIPT_ROOT", str(root))
            monkeypatch.setenv("FAKE_CLAUDE_AUTH_MESSAGE", said)
            monkeypatch.setenv("FAKE_CLAUDE_TURN", "auth")
            return root

        def turns_recover(self) -> None:
            """Undo `turns_fail`/`turns_rate_limited`/`turns_api_error`/
            `turns_auth_failed`: turns succeed again."""
            monkeypatch.delenv("FAKE_CLAUDE_TURN", raising=False)

        def hold_turns(self) -> Path:
            """Make subsequent turns block until the returned path is deleted, so a
            test can observe a work order mid-turn."""
            gate = fdir / "turn-hold"
            gate.write_text("held")
            monkeypatch.setenv("FAKE_CLAUDE_TURN_HOLD", str(gate))
            return gate

        def set_session_state(self, session_id: str, state: str) -> None:
            """Move a session's state, keeping its job result file in step.

            The supervisor publishes the turn's final assistant message when the job
            reaches `done`; the daemon reads it from there, so the fake must too.
            """
            sessions = self.sessions
            for s in sessions:
                if s["sessionId"] != session_id:
                    continue
                s["state"] = state
                payload: dict = {"state": state}
                if state == "done":
                    payload["output"] = {"result": f"final: {s.get('prompt', '')}"}
                jdir = fdir / "jobs" / s["id"]
                jdir.mkdir(parents=True, exist_ok=True)
                (jdir / "state.json").write_text(json.dumps(payload))
            (fdir / "sessions.json").write_text(json.dumps(sessions))

    return Handle()


def _settle_turns(store: Any, timeout: float = 15.0) -> bool:
    """Block until every in-flight turn in this project has been reaped.

    Worker turns are detached processes, so a test that launched one has to wait for it
    the way the daemon does — by polling. Returns False on timeout rather than raising,
    so the caller's own assertion reports the failure.
    """
    from . import worker_session

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker_session.poll(store)
        if not store.running_turns():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def settle_turns():
    """`settle_turns(store)` — block until every in-flight turn has been reaped."""
    return _settle_turns


#: Where every fixture project keeps a design document, and what a test plan names in its
#: `design_doc`. Every plan must stand on one, so without a real file here each test that
#: submits a plan would have to write one first. See §7 of
#: docs/superpowers/specs/2026-08-23-the-work-order-record.md.
FIXTURE_DESIGN_DOC = "docs/specs/exporter.md"

FIXTURE_DESIGN_DOC_BODY = """# Exporter design

## 1. Shape

The exporter is one module with one entry point.

## 2. Data model

Rows are dicts; the header is the union of keys, first-seen order.
"""


def make_git_project(root: Path, name: str, readme: str | None = "# proj\n") -> Path:
    path = root / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if readme is not None:
        (path / "README.md").write_text(readme)
    doc = path / FIXTURE_DESIGN_DOC
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(FIXTURE_DESIGN_DOC_BODY)
    return path


@pytest.fixture(autouse=True)
def claude_json(tmp_path, monkeypatch):
    """Point trust checks at a scratch claude.json; tests opt paths in as trusted."""
    path = tmp_path / "claude.json"
    path.write_text(json.dumps({"projects": {}}))
    monkeypatch.setenv("JARVIS_CLAUDE_JSON", str(path))

    def trust(project_path):
        data = json.loads(path.read_text())
        data["projects"][str(project_path)] = {"hasTrustDialogAccepted": True}
        path.write_text(json.dumps(data))

    return trust


@pytest.fixture()
def signin(tmp_path, monkeypatch):
    """Control what `claude_cli.signin_changed_at` sees — an auth pause's whole clock.

    Calling the returned function is the user running `/login`: it writes a valid
    credentials file, stamped now unless `at` back-dates it. Not autouse, because the
    gate already points the path at a file that does not exist, and "cannot tell, do not
    resume" is what every other test wants from this.
    """
    path = tmp_path / "credentials.json"
    monkeypatch.setenv(CREDENTIALS_ENV, str(path))

    def sign_in(at: float | None = None, *, refresh_expires_in: float = 30 * 86400):
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "at", "refreshToken": "rt",
            "expiresAt": int((time.time() + 3600) * 1000),
            "refreshTokenExpiresAt": int((time.time() + refresh_expires_in) * 1000),
        }}))
        if at is not None:
            os.utime(path, (at, at))
        return path

    return sign_in


@pytest.fixture()
def project(tmp_path, claude_json):
    p = make_git_project(tmp_path, "proj_a")
    claude_json(p)  # trusted, like a real project the user works in
    return p


@pytest.fixture()
def catalog_file(tmp_path, project):
    data = {
        "os": {
            "defaults": {"model": "sonnet"},  # permission_mode falls to default (auto)
            "notifications": {"sinks": ["log"]},
        },
        "projects": [
            {"name": "proj_a", "path": str(project), "description": "test project"},
        ],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path
