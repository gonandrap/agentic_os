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
    isolate_jarvis_home,
    jarvis_home,
    make_git_project,
    project,
)

pytest.importorskip("playwright")
pytest.importorskip("uvicorn")
from playwright.sync_api import sync_playwright  # noqa: E402


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
def server(jarvis_home, fake_claude, catalog_file):
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
def gated_server(jarvis_home, fake_claude, gated_catalog):
    """The same UI, over a fleet where privileged actions need approval."""
    yield from _serve(gated_catalog)


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
