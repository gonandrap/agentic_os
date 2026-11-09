"""Screenshot the requestor filter on /project and /inbox against a throwaway fleet"""

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
PORT = 8798


def seed() -> None:
    """One project: two orders the user typed, two the OS opened, an inbox row for each"""
    from jarvis import ops
    from jarvis.central_store import CentralStore

    home = Path(tempfile.mkdtemp())
    (home / "proj_a").mkdir()
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(home / "proj_a"),
                      "description": "demo"}]}))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("proj_a", str(home / "proj_a"), "demo")
    mine = ops.create_work_order("proj_a", "fix the login bug")
    ops.create_feature_order("proj_a", "export calendar to ics", description="d")
    theirs = ops.create_work_order("proj_a", "fleet health: trim stale worktrees",
                                   requestor="os")
    ops.create_improvement_order("proj_a", "slow validation rounds", description="d",
                                 refs=["wo-1"], requestor="os")
    central.add_inbox("proj_a", "login bug fixed", wo_id=mine["id"])
    central.add_inbox("proj_a", "worktrees trimmed", wo_id=theirs["id"])
    central.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        # PLAYWRIGHT_CHROME: an already-installed chromium when this playwright wants a newer one
        browser = p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROME") or None)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        for name, url in (("project-any", "/project/proj_a"),
                          ("project-you", "/project/proj_a?requestor=you"),
                          ("project-os", "/project/proj_a?requestor=os"),
                          ("inbox-you", "/inbox?requestor=you")):
            page.goto(base + url)
            page.locator("form.filter-bar").first.scroll_into_view_if_needed()
            page.screenshot(path=SHOTS / f"requestor-{name}.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)  # a worker session would stamp every create `os`
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("requestor-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
