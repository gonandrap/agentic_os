"""Browser test fixtures: a real uvicorn server over the fixture stores, driven by
headless Chromium (Playwright).

Run with: pytest tests_browser -q   (after `playwright install chromium`)
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from jarvis.testing import (  # noqa: F401
    catalog_file,
    claude_json,
    fake_claude,
    jarvis_home,
    live_daemon,
    make_git_project,
    project,
)

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")
from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.set_config` refuses one.

    Autouse here rather than named per test: the UI server runs in a thread of THIS
    process, so a leftover `JARVIS_WO_ID` locks out the page's own write path too, not
    just the fixture's setup. Same trap as tests/test_config_console.py (kn-650b6f24).
    """
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(catalog_path):
    """Start the OS against `catalog_path` and a UI server over it; yields the URL."""
    import uvicorn

    from jarvis import ops
    from jarvis.ui.app import create_app

    ops.start_os(str(catalog_path), foreground=True)
    port = _free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                            log_level="critical")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def server(jarvis_home, fake_claude, catalog_file, live_daemon):
    """OS started (foreground bootstrap) + live UI server; yields the base URL."""
    yield from _serve(catalog_file)


@pytest.fixture()
def gated_catalog(tmp_path, project):
    """A catalog whose only project gates every privileged action."""
    import json

    from jarvis import gates

    data = {
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }
    path = tmp_path / "catalog-gated.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def gated_server(jarvis_home, fake_claude, gated_catalog, live_daemon):
    """The same UI, over a fleet where privileged actions need approval."""
    yield from _serve(gated_catalog)


_SQUEEZED_JS = """(selector) => {
  const out = [];
  for (const el of document.querySelectorAll(selector)) {
    if (el.offsetParent === null || el.closest('pre')) continue;
    const text = el.innerText.trim();
    if (text.length <= 30) continue;
    const cs = getComputedStyle(el);
    const lh = cs.lineHeight === 'normal' ? 1.2 * parseFloat(cs.fontSize) : parseFloat(cs.lineHeight);
    const lines = Math.max(1, Math.round(el.getBoundingClientRect().height / lh));
    if (text.length / lines < 12) out.push(`${lines} lines: ${text.slice(0, 80)}`);
  }
  return out;
}"""


@pytest.fixture()
def readable():
    """`check(page, selector)` fails on any text squeezed to a word or two per line — §2.6 of
    docs/superpowers/specs/2026-10-29-dashboard-full-width-failures-first-row-details.md"""
    def check(page, selector: str = "main td, main th, main p") -> None:
        squeezed = page.evaluate(_SQUEEZED_JS, selector)
        assert not squeezed, f"text squeezed into a narrow column at {page.viewport_size}: {squeezed}"
    return check


@pytest.fixture()
def daemon(catalog_file):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def gated_daemon(gated_catalog):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon
    return Daemon(load_catalog(gated_catalog))
