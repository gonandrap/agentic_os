"""The help terminal side panel in a real browser (docs/superpowers/specs/2026-11-12-onboarding-help-terminal-panel.md §3)"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from jarvis import interview, paths, wiring
from jarvis.testing import make_git_project

# clock-exempt: class 3 — deadline waits on real PTY subprocesses behind a browser
NAME = "newproj"

_BUFFER_JS = """() => {
  const t = window.helpTerm;
  if (!t) return '';
  const b = t.buffer.active, out = [];
  for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l) out.push(l.translateToString(true)); }
  return out.join('\\n');
}"""


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def draft(server, tmp_path, monkeypatch) -> str:
    # `wiring.discover` execs `claude`; the view reads one fixed lever instead
    item = wiring.Item(kind="skill", name="s", source="user", lever=f"{wiring.LEVER_SKILL}s")
    monkeypatch.setattr(interview, "wiring_choices",
                        lambda draft, *, document: {"items": [item]})
    interview.new_draft(make_git_project(tmp_path / "work", NAME), NAME)
    return server


def _wait_text(page, needle: str, timeout: float = 10.0) -> str:
    page.wait_for_function(f"(n) => ({_BUFFER_JS})().includes(n)", arg=needle,
                           timeout=timeout * 1000)
    return page.evaluate(_BUFFER_JS)


def _records() -> list[Path]:
    d = paths.jarvis_home() / "ui" / "help-pty"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _child_pid() -> int:
    recs = _records()
    assert len(recs) == 1, recs
    return int(recs[0].stem.split("-")[1])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a zombie still answers kill(0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def _until(cond, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not cond():
        time.sleep(0.05)
    return cond()


def _open(page, base: str) -> None:
    page.goto(f"{base}/onboard/{NAME}/identity")
    assert page.locator("#help-panel").is_hidden()
    page.get_by_role("button", name="Ask for help").click()
    _wait_text(page, "fake-claude interactive @onboard_helper")


def test_type_navigate_paste_and_close(page, draft):
    _open(page, draft)
    assert page.locator("#help-status").inner_text() == "live"
    page.locator("#help-term").click()
    page.keyboard.type("hello")
    page.keyboard.press("Enter")
    _wait_text(page, "echo: hello")
    pid = _child_pid()

    # Navigation: the panel reattaches to the same helper and replays its scrollback
    page.locator("a[rel=next]").click()
    page.wait_for_url(f"**/onboard/{NAME}/*")
    assert "echo: hello" in _wait_text(page, "echo: hello")
    assert _child_pid() == pid

    # A knob's `?` feeds the same session the new context
    page.locator("form[action$='/help'] button[title^='ask for help on']").first.click()
    _wait_text(page, "pasted: [context]")
    assert _child_pid() == pid and not page.locator("#help-panel").is_hidden()

    # Close leaves no PTY. Control: alive until then
    assert _alive(pid)
    page.locator("#help-close").click()
    assert _until(lambda: not _records() and not _alive(pid)), (_records(), pid)
    assert page.locator("#help-panel").is_hidden()
    # And a reload does not bring it back
    page.reload()
    assert page.locator("#help-panel").is_hidden()


def test_dragging_the_handle_resizes_the_pty(page, draft):
    page.set_viewport_size({"width": 1600, "height": 900})
    _open(page, draft)
    before = page.evaluate("() => [helpTerm.rows, helpTerm.cols]")
    box = page.locator("#help-grip").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x - 300, y, steps=5)
    page.mouse.up()
    page.wait_for_function(f"() => helpTerm.cols !== {before[1]}")
    rows, cols = page.evaluate("() => [helpTerm.rows, helpTerm.cols]")
    _wait_text(page, f"size: {rows} {cols}")
    assert [rows, cols] != before


def test_a_failed_spawn_falls_back_and_says_why(page, draft, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CLAUDE_BIN", str(tmp_path / "no-such-claude"))
    page.goto(f"{draft}/onboard/{NAME}/identity")
    page.get_by_role("button", name="Ask for help").click()
    page.wait_for_url(f"**/onboard/{NAME}/help")
    err = page.locator("#help-pty-error").inner_text()
    assert err.startswith("The terminal panel could not start: ")
    assert len(err) > len("The terminal panel could not start: ")
    assert not _records()
