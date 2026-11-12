"""The help terminal's routes, websocket and lifespan (docs/superpowers/specs/2026-11-12-onboarding-help-terminal-panel.md §2)"""

from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from jarvis import claude_cli, interview, ops, paths, wiring  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.testing import make_git_project  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402

# clock-exempt: class 3 — deadline waits on real PTY subprocesses behind a websocket
NAME = "newproj"
LOCAL = "http://127.0.0.1"
PHONE = "http://phone.tailnet.example:8787"


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


@pytest.fixture()
def section(tmp_path) -> str:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"os": {"ui": {"base_url": PHONE}}, "projects": []}))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    interview.new_draft(make_git_project(tmp_path / "work", NAME), NAME)
    return ops.onboard_view(NAME)["sections"][0]["key"]


def _client() -> TestClient:
    # Host must be loopback, or the loopback control is refused
    return TestClient(create_app(), base_url=LOCAL)


def _start(c: TestClient, section: str, origin: str | None = LOCAL, **form):
    headers = {"origin": origin} if origin else {}
    return c.post(f"/onboard/{NAME}/help/pty", data={"section": section, **form},
                  headers=headers)


def _ws(c: TestClient, token: str, origin: str = LOCAL, name: str = NAME):
    # Absolute: websocket_connect resolves a relative URL against ws://testserver, not base_url
    return c.websocket_connect(f"ws://127.0.0.1/onboard/{name}/help/pty/ws?token={token}",
                               headers={"origin": origin})


def _read_until(ws, needle: bytes, timeout: float = 5.0) -> bytes:
    """receive_bytes has no timeout: enforce one off-thread so a missing echo fails, not hangs"""
    seen = b""
    deadline = time.monotonic() + timeout
    pool = concurrent.futures.ThreadPoolExecutor(1)
    try:
        while needle not in seen:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                seen += pool.submit(ws.receive_bytes).result(timeout=left)
            except concurrent.futures.TimeoutError:
                break
    finally:
        pool.shutdown(wait=False)
    return seen


def _close_code(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        _read_until(ws, b"\0never")
    return e.value.code


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:  # a zombie still answers kill(0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return False


def _wait(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        time.sleep(0.02)
    return cond()


def _pid(c: TestClient) -> int:
    return c.app.state.help_sessions.get(NAME).pid


def test_start_refuses_a_foreign_or_missing_origin(section, fake_claude):
    with _client() as c:
        assert _start(c, section, origin=None).status_code == 403
        assert _start(c, section, origin="http://evil.example").status_code == 403
        assert c.app.state.help_sessions.live_pids() == []
        # Control: loopback and the catalog's ui_base_url are this dashboard
        ok = _start(c, section)
        assert ok.status_code == 200 and ok.json()["token"]
        assert ok.json()["ws_url"] == f"/onboard/{NAME}/help/pty/ws"
        assert ok.json()["session_name"] == f"[onboard] {NAME} · {section}"
        assert _start(c, section, origin=PHONE).status_code == 200


def test_unknown_section_is_404(section, fake_claude):
    with _client() as c:
        resp = _start(c, "no-such-section")
        assert resp.status_code == 404 and "no section" in resp.json()["error"]


def test_ws_refuses_bad_token_bad_origin_and_no_session(section, fake_claude):
    with _client() as c:
        with _ws(c, "nothing-yet") as ws:
            assert _close_code(ws) == 4404
        token = _start(c, section).json()["token"]
        with _ws(c, "wrong") as ws:
            assert _close_code(ws) == 4403
        with _ws(c, token, origin="http://evil.example") as ws:
            assert _close_code(ws) == 4403
        # Control: right token and Origin get the scrollback
        with _ws(c, token) as ws:
            assert b"fake-claude interactive @onboard_helper" in _read_until(
                ws, b"interactive")


def test_typed_bytes_reach_the_helper_and_resize_too(section, fake_claude):
    with _client() as c:
        token = _start(c, section).json()["token"]
        with _ws(c, token) as ws:
            _read_until(ws, b"interactive")
            ws.send_bytes(b"hi\r")
            assert b"echo: hi" in _read_until(ws, b"echo: hi")
            ws.send_text("not json")  # ignored, session stays
            ws.send_text(json.dumps({"resize": [30, 100]}))
            assert b"size: 30 100" in _read_until(ws, b"size: 30 100")


def test_second_start_feeds_the_live_session(section, fake_claude):
    with _client() as c:
        first = _start(c, section).json()
        with _ws(c, first["token"]) as ws:
            _read_until(ws, b"interactive")
            again = _start(c, section, focus="os.x").json()
            assert first["created"] is True and again["created"] is False
            assert again["token"] == first["token"]
            assert b"pasted: [context]" in _read_until(ws, b"pasted: [context]")


def test_disconnect_keeps_the_helper_close_kills_it(section, fake_claude):
    with _client() as c:
        token = _start(c, section).json()["token"]
        pid = _pid(c)
        with _ws(c, token) as ws:
            _read_until(ws, b"interactive")
        assert not _gone(pid)
        resp = c.post(f"/onboard/{NAME}/help/pty/close", data={"token": token},
                      headers={"origin": LOCAL})
        assert resp.json() == {"closed": True}
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_close_refuses_bad_origin_and_stale_token(section, fake_claude):
    with _client() as c:
        token = _start(c, section).json()["token"]
        pid = _pid(c)
        url = f"/onboard/{NAME}/help/pty/close"
        assert c.post(url, data={"token": token},
                      headers={"origin": "http://evil.example"}).status_code == 403
        stale = c.post(url, data={"token": "stale"}, headers={"origin": LOCAL})
        assert stale.status_code == 200 and stale.json() == {"closed": False}
        assert not _gone(pid)
        # Control: the right token kills it
        assert c.post(url, data={"token": token},
                      headers={"origin": LOCAL}).json() == {"closed": True}
        assert _wait(lambda: _gone(pid))


def test_shutdown_leaves_no_helper(section, fake_claude):
    with _client() as c:
        _start(c, section)
        pids = c.app.state.help_sessions.live_pids()
        assert pids and not _gone(pids[0])  # control
    assert all(_gone(p) for p in pids)


def test_missing_claude_binary_is_502_with_the_os_error(section, fake_claude,
                                                        monkeypatch, tmp_path):
    monkeypatch.setenv(claude_cli.CLAUDE_BIN_ENV, str(tmp_path / "no-claude"))
    with _client() as c:
        resp = _start(c, section)
        assert resp.status_code == 502
        assert "No such file" in resp.json()["error"]


def test_newer_attach_replaces_the_older(section, fake_claude):
    with _client() as c:
        token = _start(c, section).json()["token"]
        with _ws(c, token) as old:
            _read_until(old, b"interactive")
            with _ws(c, token) as new:
                _read_until(new, b"interactive")
                assert _close_code(old) == 4409
                new.send_bytes(b"still\r")
                assert b"echo: still" in _read_until(new, b"echo: still")


def test_helper_exit_closes_the_socket_1000(section, fake_claude):
    with _client() as c:
        token = _start(c, section).json()["token"]
        with _ws(c, token) as ws:
            _read_until(ws, b"interactive")
            ws.send_bytes(b"/exit\r")
            assert _close_code(ws) == 1000


def test_startup_reaps_a_dead_dashboards_helper(section, fake_claude):
    owners = paths.jarvis_home() / "ui" / "help-pty"
    owners.mkdir(parents=True, exist_ok=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        (owners / f"{dead.pid}-{orphan.pid}.json").write_text(json.dumps(
            {"dashboard_pid": dead.pid, "pid": orphan.pid, "argv": ["sleep", "60"]}))
        assert orphan.poll() is None  # control
        with _client():
            assert orphan.wait(timeout=5) is not None
    finally:
        orphan.kill()
        orphan.wait()
