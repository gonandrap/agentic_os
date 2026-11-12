"""The onboarding help PTY registry and its interactive launch (docs/superpowers/specs/2026-11-12-onboarding-help-terminal-panel.md §1)"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jarvis import interview, ops, wiring
from jarvis.central_store import CentralStore
from jarvis.testing import make_git_project
from jarvis.ui import help_pty
from jarvis.ui.help_pty import HelpSessions

NAME = "newproj"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture(autouse=True)
def no_inventory(monkeypatch):
    """`wiring.discover` execs `claude`; the view reads one fixed lever instead"""
    item = wiring.Item(kind="skill", name="s", source="user", lever=f"{wiring.LEVER_SKILL}s")
    monkeypatch.setattr(interview, "wiring_choices",
                        lambda draft, *, document: {"items": [item]})


@pytest.fixture
def anyio_backend():
    return "asyncio"  # add_reader is asyncio's


@pytest.fixture()
def draft(tmp_path) -> str:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"os": {}, "projects": []}))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    interview.new_draft(make_git_project(tmp_path / "work", NAME), NAME)
    return ops.onboard_view(NAME)["sections"][0]["key"]


def _launch(tmp_path: Path, prompt: str = "hello prompt", section: str = "sec",
            focus: str = "") -> dict:
    return {"argv": [os.environ["JARVIS_CLAUDE_BIN"], "--name", "[onboard] t · s",
                     "--agent", "onboard_helper", "--", prompt],
            "cwd": tmp_path, "env": dict(os.environ), "prompt": prompt,
            "session_name": "[onboard] t · s", "section": section, "focus": focus}


async def _read_until(q: asyncio.Queue, needle: bytes, seen: bytes = b"",
                      timeout: float = 5.0) -> bytes:
    deadline = time.monotonic() + timeout
    while needle not in seen:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            chunk = await asyncio.wait_for(q.get(), left)
        except asyncio.TimeoutError:
            break
        if chunk is None:
            break
        seen += chunk
    return seen


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie still answers kill(0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def _wait(cond, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _flag_values(argv: list[str], flag: str) -> list[str]:
    i = argv.index(flag) + 1
    out = []
    while i < len(argv) and not argv[i].startswith("--"):
        out.append(argv[i])
        i += 1
    return out


def test_launch_is_interactive_and_read_only(draft, fake_claude):
    argv = ops.onboard_help_launch(NAME, draft)["argv"]

    assert argv[argv.index("--agent") + 1] == "onboard_helper"
    assert argv.count("--add-dir") == 2
    assert _flag_values(argv, "--disallowedTools") == ["Edit", "Write", "NotebookEdit"]
    allowed = _flag_values(argv, "--allowedTools")
    assert allowed == ["Bash(jarvis onboard explain:*)", "Bash(jarvis onboard questions:*)",
                       "Bash(jarvis config show:*)", "Bash(jarvis config get:*)", "Read"]
    prefixes = [p[len("Bash("):-len(":*)")] for p in allowed if p.startswith("Bash(")]
    for writer in ("jarvis onboard apply", "jarvis config set"):
        assert not any(writer.startswith(p) for p in prefixes)
    # Control: the read verbs themselves match
    assert any("jarvis onboard explain foo".startswith(p) for p in prefixes)
    assert "--bg" not in argv and "-p" not in argv
    assert argv[-2] == "--" and argv[-1] == ops.onboard_help_launch(NAME, draft)["prompt"]
    # Control: the background fallback still backgrounds
    assert "--bg" in ops.onboard_help(NAME, draft)["argv"]


@pytest.mark.anyio
async def test_typed_line_reaches_the_helper(tmp_path, fake_claude):
    reg = HelpSessions(owner_dir=tmp_path / "owners")
    try:
        s, created = await reg.start_or_feed("d", _launch(tmp_path))
        _, q = s.attach()
        assert b"fake-claude interactive @onboard_helper" in await _read_until(
            q, b"interactive")
        s.write(b"hello\r")
        assert b"echo: hello" in await _read_until(q, b"echo: hello")
    finally:
        await reg.close_all()


@pytest.mark.anyio
async def test_second_start_feeds_the_same_session(tmp_path, fake_claude):
    reg = HelpSessions(owner_dir=tmp_path / "owners")
    try:
        first, created = await reg.start_or_feed("d", _launch(tmp_path))
        _, q = first.attach()
        await _read_until(q, b"interactive")
        again, created_again = await reg.start_or_feed(
            "d", _launch(tmp_path, "line one\nline two", section="other", focus="os.x"))
        assert (created, created_again) == (True, False)
        assert again.pid == first.pid
        out = await _read_until(q, b"pasted: ")
        assert b"pasted: [context] The user is now on section other, asking about os.x." in out
    finally:
        await reg.close_all()


@pytest.mark.anyio
async def test_close_kills_and_forgets(tmp_path, fake_claude):
    owners = tmp_path / "owners"
    reg = HelpSessions(owner_dir=owners)
    try:
        s, _ = await reg.start_or_feed("d", _launch(tmp_path))
        assert not _gone(s.pid) and reg.live_pids() == [s.pid]  # control
        assert len(list(owners.glob("*.json"))) == 1
        assert await reg.close("d") is True
        with pytest.raises(ProcessLookupError):
            os.kill(s.pid, 0)
        assert list(owners.glob("*.json")) == []
        assert reg.live_pids() == [] and reg.get("d") is None
    finally:
        await reg.close_all()


@pytest.mark.anyio
async def test_idle_session_is_closed_attached_one_survives(tmp_path, fake_claude):
    reg = HelpSessions(idle_seconds=0.2, owner_dir=tmp_path / "owners")
    try:
        lone, _ = await reg.start_or_feed("lone", _launch(tmp_path))
        held, _ = await reg.start_or_feed("held", _launch(tmp_path))
        held.attach()
        deadline = time.monotonic() + 2
        while not _gone(lone.pid) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert _gone(lone.pid)
        assert not _gone(held.pid) and reg.get("held") is held  # control
    finally:
        await reg.close_all()


def test_reap_stale_kills_only_a_dead_dashboards_helper(tmp_path):
    owners = tmp_path / "owners"
    owners.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    mine = subprocess.Popen(["sleep", "61"], start_new_session=True)
    reused = subprocess.Popen(["sleep", "62"], start_new_session=True)
    try:
        def rec(owner, proc, argv):
            path = owners / f"{owner}-{proc.pid}.json"
            path.write_text(json.dumps({"dashboard_pid": owner, "pid": proc.pid,
                                        "argv": argv}))
            return path

        rec(dead.pid, orphan, ["sleep", "60"])
        kept = rec(os.getpid(), mine, ["sleep", "61"])
        reused_rec = rec(dead.pid, reused, ["claude", "--agent", "onboard_helper"])

        assert HelpSessions.reap_stale(_Reg(owners)) == [orphan.pid]
        assert orphan.wait(timeout=5) is not None
        assert mine.poll() is None and kept.exists()
        assert reused.poll() is None and not reused_rec.exists()
    finally:
        for p in (orphan, mine, reused):
            p.kill()
            p.wait()


class _Reg:
    """reap_stale needs only owner_dir, and no running loop"""

    def __init__(self, owner_dir: Path) -> None:
        self.owner_dir = owner_dir


@pytest.mark.anyio
async def test_helper_env_is_not_a_workers(draft, tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-parent")
    monkeypatch.setenv("JARVIS_TURN_TRANSPORT", "headless")
    launch = ops.onboard_help_launch(NAME, draft)
    reg = HelpSessions(owner_dir=tmp_path / "owners")
    try:
        await reg.start_or_feed("d", launch)
        call = fake_claude.wait_calls(lambda c: "--allowedTools" in c["argv"], timeout=5)[0]
        assert "JARVIS_WO_ID" not in call["env"]
        assert "JARVIS_TURN_TRANSPORT" not in call["env"]
        assert call["env"]["TERM"] == "xterm-256color"
        assert os.environ["JARVIS_WO_ID"] == "wo-parent"  # control: the parent had it
    finally:
        await reg.close_all()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PDEATHSIG is Linux")
def test_helper_dies_with_a_killed_dashboard(tmp_path, fake_claude):
    script = textwrap.dedent(f"""
        import asyncio, os, sys
        from pathlib import Path
        from jarvis.ui.help_pty import HelpSessions

        async def main():
            reg = HelpSessions(owner_dir=Path({str(tmp_path / 'owners')!r}))
            argv = [os.environ["JARVIS_CLAUDE_BIN"], "--agent", "onboard_helper", "--", "p"]
            s, _ = await reg.start_or_feed("d", {{"argv": argv, "cwd": {str(tmp_path)!r},
                                             "env": dict(os.environ), "prompt": "p"}})
            print(s.pid, flush=True)
            await asyncio.sleep(60)

        asyncio.run(main())
    """)
    dash = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
    try:
        pid = int(dash.stdout.readline())
        assert not _gone(pid)  # control
        dash.send_signal(signal.SIGKILL)
        dash.wait(timeout=5)
        assert _wait(lambda: _gone(pid), 5)
    finally:
        dash.kill()
        dash.wait()


@pytest.mark.anyio
async def test_resize_reaches_the_helper(tmp_path, fake_claude):
    reg = HelpSessions(owner_dir=tmp_path / "owners")
    try:
        s, _ = await reg.start_or_feed("d", _launch(tmp_path))
        _, q = s.attach()
        seen = await _read_until(q, b"interactive")
        s.resize(30, 100)
        assert b"size: 30 100" in await _read_until(q, b"size: 30 100", seen)
    finally:
        await reg.close_all()


@pytest.mark.anyio
async def test_missing_binary_raises_with_the_os_message(tmp_path, fake_claude):
    reg = HelpSessions(owner_dir=tmp_path / "owners")
    launch = _launch(tmp_path)
    launch["argv"][0] = str(tmp_path / "no-such-claude")
    try:
        with pytest.raises(help_pty.HelpPtyError, match="No such file"):
            await reg.start_or_feed("d", launch)
        assert reg.live_pids() == []
        # Control: the real fake starts
        assert (await reg.start_or_feed("d", _launch(tmp_path)))[1] is True
    finally:
        await reg.close_all()
