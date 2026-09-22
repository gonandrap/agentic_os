"""Screenshot a `needs_review` work order that says why — issue 573's UI evidence.

Three pages: the order flagged, the same order after the "Got it" button (the reason
survives as "already seen"), and its timeline with the `acknowledged` event as prose.
Everything lives in a temp `JARVIS_HOME`, so it never touches the live OS:

    uv run python scripts/screenshot_needs_review_reason.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8801


def seed() -> str:
    """One work order in the shape `Daemon.settle_work_order` leaves behind."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.invariants import IDLE_NO_FINISH_BLOCKER
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    project = home / "shared_schedule"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "README.md").write_text("# shared_schedule\n")
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "shared_schedule", "path": str(project),
                      "description": "the household calendar"}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    wo = ops.create_work_order("shared_schedule", "Fix the recurring-event drift")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
        store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)
    finally:
        store.close()
    return wo["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{PORT}/wo/shared_schedule/{wo_id}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url)
        page.screenshot(path=SHOTS / "needs-review-flagged.png")

        page.locator("button", has_text="Got it").first.click()
        page.wait_for_load_state()
        page.goto(url)
        page.screenshot(path=SHOTS / "needs-review-already-seen.png")

        page.locator("button", has_text="Timeline").first.click()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "needs-review-timeline.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("needs-review-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
