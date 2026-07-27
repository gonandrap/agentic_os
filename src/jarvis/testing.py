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
from pathlib import Path

import pytest

from .bugreport import GH_BIN_ENV
from .claude_cli import CLAUDE_BIN_ENV
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
    }
    if not os.environ.get(LLM_EVALS_ENV):
        env[CLAUDE_BIN_ENV] = str(_blocked_bin(root, "claude", BLOCKED_CLAUDE))
    return env


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
import json, os, sys

state_dir = os.environ["FAKE_CLAUDE_DIR"]
calls_path = os.path.join(state_dir, "calls.jsonl")
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
with open(calls_path, "a") as f:
    f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")

def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default

if "--version" in argv:
    print("9.9.9 (fake claude)")
elif argv[:1] == ["agents"]:
    print(json.dumps(load_sessions()))
elif "--bg" in argv:
    # like the real supervisor: assigns its own session id (ignores --session-id);
    # with --resume it forks the conversation under a fresh session id
    import hashlib
    sessions = load_sessions()
    name = opt("--name", "")
    resumed = opt("--resume")
    seed = name + (resumed or "") + str(len(sessions))
    sid = "sess-" + hashlib.sha1(seed.encode()).hexdigest()[:12]
    job_id = sid[5:13]
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
    save_sessions(remaining)
elif "-p" in argv and "--resume" not in argv:
    # headless one-shot (`claude -p ...`) — Neo's answering path. Deterministic
    # verdict driven by the prompt so tests control escalation.
    prompt = argv[argv.index("-p") + 1]
    if "FORCE_FAIL" in prompt:
        sys.stderr.write("model call failed (test-forced)\n"); sys.exit(1)
    if "FORCE_ESCALATE" in prompt:
        verdict = {"escalate": True, "answer": "",
                   "reason": "test-forced escalation"}
    elif "FORCE_GARBAGE" in prompt:
        print(json.dumps({"result": "I think you should maybe do the thing?"}))
        sys.exit(0)
    else:
        verdict = {"escalate": False,
                   "answer": f"neo-decision for: {prompt.splitlines()[-1][:60]}",
                   "reason": "test verdict"}
    print(json.dumps({"result": json.dumps(verdict)}))
elif "--resume" in argv and "-p" in argv:
    behavior = os.environ.get("FAKE_CLAUDE_RESUME", "ok")
    if behavior == "fail":
        sys.stderr.write("resume failed\n"); sys.exit(1)
    sid = argv[argv.index("--resume") + 1]
    # like the real CLI: refuse to resume a session still owned by a bg agent
    if any(s["sessionId"] == sid for s in load_sessions()):
        sys.stderr.write(f"Error: Session {sid} is currently running as a background agent (bg).\n")
        sys.exit(1)
    print(json.dumps({"result": f"ack: {argv[argv.index('-p') + 1][:40]}"}))
else:
    sys.stderr.write(f"fake claude: unhandled argv {argv}\n"); sys.exit(2)
'''


FAKE_GH = r'''#!/usr/bin/env python3
"""Fake `gh` CLI for tests: records invocations, prints an issue URL."""
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
else:
    sys.stderr.write(f"fake gh: unhandled argv {argv}\n")
    sys.exit(2)
'''


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

        @property
        def calls(self) -> list[dict]:
            path = gdir / "calls.jsonl"
            if not path.exists():
                return []
            return [json.loads(l) for l in path.read_text().splitlines()]

        def fail(self, message: str) -> None:
            """Make every subsequent `gh` call fail with `message` on stderr."""
            monkeypatch.setenv("FAKE_GH_FAIL", message)

    return Handle()


@pytest.fixture(autouse=True)
def jarvis_home(tmp_path, monkeypatch):
    """Per-test central state. Autouse: isolation is not something a test can forget.

    Naming it as a parameter is still the way to get the path; the only thing autouse
    changes is that a test which does NOT name it is isolated anyway, instead of writing
    whatever `$JARVIS_HOME` the shell carried in (in a worker session: production).
    `gate_test_environment` is the process-wide floor under this.
    """
    home = tmp_path / "jarvis-home"
    monkeypatch.setenv("JARVIS_HOME", str(home))
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
            path = fdir / "calls.jsonl"
            if not path.exists():
                return []
            return [json.loads(l) for l in path.read_text().splitlines()]

        @property
        def sessions(self) -> list[dict]:
            path = fdir / "sessions.json"
            return json.loads(path.read_text()) if path.exists() else []

        def job_state(self, job_id: str) -> dict:
            path = fdir / "jobs" / job_id / "state.json"
            return json.loads(path.read_text()) if path.exists() else {}

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


def make_git_project(root: Path, name: str, readme: str | None = "# proj\n") -> Path:
    path = root / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if readme is not None:
        (path / "README.md").write_text(readme)
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
