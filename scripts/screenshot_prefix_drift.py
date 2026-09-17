"""Screenshot a work order whose prompt prefix moved, for a PR's UI evidence.

Finding 4 action 1. The `prefix_drift` event has ONE surface — the work order's
timeline — so what `timeline._describe` renders is the whole of what a reader ever sees
of it, and its detail line is long enough to be a wrapping question rather than a
string-assertion one. `scripts/screenshot_wo_alarm_record.py` is the shape this copies;
everything lives in a temp `JARVIS_HOME` and a temp catalog, so it never touches the
live OS.

    uv run python scripts/screenshot_prefix_drift.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8799


def seed() -> str:
    """One order carrying both shapes of the event: a version that moved, which is
    quoted, and a digest that moved, which is not."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    project = make_git_project(home, "jarvis_os")
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    wo = ops.create_work_order(
        "jarvis_os", "Rebuild the citation export",
        description="The CSV writer drops the DOI column on multi-author rows.")
    _, path, _ = ops.find_work_order(wo["id"], "jarvis_os")
    pstore = ProjectStore(path)
    pstore.add_event(wo["id"], "prefix_drift", {
        "changed": ["cli_version"],
        "before": {"cli_version": "2.1.271"},
        "after": {"cli_version": "2.1.272"},
        "session_id": "8f21c4e0-5a3b-4d19-9f77-1c0ab5d2e6f4", "source": "resume"})
    pstore.add_event(wo["id"], "prefix_drift", {
        "changed": ["git_briefing", "memory"],
        "before": {"git_briefing": "1a2b3c4d5e6f7081", "memory": "aa11bb22cc33dd44"},
        "after": {"git_briefing": "90fedcba87654321", "memory": "ff99ee88dd77cc66"},
        "session_id": "8f21c4e0-5a3b-4d19-9f77-1c0ab5d2e6f4", "source": "resume"})
    pstore.close()
    return f"/wo/jarvis_os/{wo['id']}"


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(url: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}{url}")
        page.locator("button", has_text="Timeline").click()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "wo-prefix-drift-timeline.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    url = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(url)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("wo-prefix-drift-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
