"""Screenshot `/stuck` naming a project it does not judge (issue 1450)"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs" / "screenshots" / "stuck-unjudged-banner.png"
PORT = 8813


def seed() -> None:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    projects = [make_git_project(home, name) for name in ("jarvis_os", "shared_schedule")]
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "notifications": {"sinks": ["log"]}},
        "projects": [{"name": p.name, "path": str(p), "description": p.name}
                     for p in projects],
    }))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)
    ops.create_work_order("jarvis_os", "open, but nobody judges it")


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(6)
    from playwright.sync_api import sync_playwright

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 700})
        page.goto(f"http://127.0.0.1:{PORT}/stuck")
        page.wait_for_timeout(300)
        page.screenshot(path=OUT, full_page=True)
        text = page.locator("main, body").first.inner_text()
        browser.close()
    assert "jarvis config set jarvis_os os.fleet_health.enabled true" in text, text
    assert "shared_schedule" in text, text
    print(OUT, OUT.stat().st_size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
