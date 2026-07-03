"""Shared fixtures: isolated JARVIS_HOME, fixture git projects, fake `claude` CLI."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

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
    sessions = load_sessions()
    sessions.append({
        "id": (opt("--session-id") or "x")[:8],
        "sessionId": opt("--session-id"),
        "cwd": os.getcwd(),
        "kind": "background",
        "name": opt("--name", ""),
        "state": "running",
        "startedAt": 0,
    })
    save_sessions(sessions)
elif "--resume" in argv and "-p" in argv:
    behavior = os.environ.get("FAKE_CLAUDE_RESUME", "ok")
    if behavior == "fail":
        sys.stderr.write("resume failed\n"); sys.exit(1)
    print(json.dumps({"result": f"ack: {argv[argv.index('-p') + 1][:40]}"}))
else:
    sys.stderr.write(f"fake claude: unhandled argv {argv}\n"); sys.exit(2)
'''


@pytest.fixture()
def jarvis_home(tmp_path, monkeypatch):
    home = tmp_path / "jarvis-home"
    monkeypatch.setenv("JARVIS_HOME", str(home))
    return home


@pytest.fixture()
def fake_claude(tmp_path, monkeypatch):
    """Install a fake `claude` binary; returns a handle to its recorded state."""
    fdir = tmp_path / "fake-claude"
    fdir.mkdir()
    binpath = fdir / "claude"
    binpath.write_text(FAKE_CLAUDE)
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(fdir))
    monkeypatch.setenv("JARVIS_CLAUDE_BIN", str(binpath))

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

        def set_session_state(self, session_id: str, state: str) -> None:
            sessions = self.sessions
            for s in sessions:
                if s["sessionId"] == session_id:
                    s["state"] = state
            (fdir / "sessions.json").write_text(json.dumps(sessions))

    return Handle()


def make_git_project(root: Path, name: str, readme: str | None = "# proj\n") -> Path:
    path = root / name
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if readme is not None:
        (path / "README.md").write_text(readme)
    return path


@pytest.fixture()
def project(tmp_path):
    return make_git_project(tmp_path, "proj_a")


@pytest.fixture()
def catalog_file(tmp_path, project):
    data = {
        "os": {
            "defaults": {"model": "sonnet", "permission_mode": "acceptEdits"},
            "notifications": {"sinks": ["log"]},
        },
        "projects": [
            {"name": "proj_a", "path": str(project), "description": "test project"},
        ],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path
