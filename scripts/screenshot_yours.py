"""Screenshot /yours against a throwaway fleet, for a PR's UI evidence"""

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
    """One ask in each state, one with a child filed into another project"""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore, db
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    stores = {}
    central = CentralStore()
    for name in ("jarvis_os", "shared_schedule"):
        path = make_git_project(home, name)
        central.upsert_project(name, str(path), name)
        stores[name] = ProjectStore(path)
    # The cost column reads the catalog
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({"projects": [
        {"name": n, "path": str(home / n), "description": n} for n in stores]}))
    central.set_state("catalog_path", str(catalog))
    central.conn.commit()
    central.close()
    a, b = stores["jarvis_os"], stores["shared_schedule"]
    need = a.create_work_order("Fix login redirect loop", requestor="user", status="needs_review")
    a.update_work_order(need["id"], needs_attention=1)
    fo = a.create_feature_order("Yours page and tree notice", requestor="user")
    a.set_feature_status(fo["id"], "executing")
    a.create_work_order("plan", kind="planner", parent_id=fo["id"], status="completed")
    a.create_work_order("Add the /yours page", parent_id=fo["id"], status="running")
    io = a.create_feature_order("Slow calendar sync", kind="improvement", requestor="user")
    far = b.create_work_order("Cache the ICS feed", status="completed")
    a.update_feature_order(io["id"], plan=db.to_json({"findings": [{"key": "f1", "created_orders": [
        {"id": far["id"], "project": "shared_schedule"}]}]}))
    a.set_feature_status(io["id"], "completed")
    a.add_event(need["id"], "request_settled", {"root_id": io["id"]})
    done = a.create_work_order("Bump the release notes", requestor="user", status="completed")
    ops.mark_request_seen("jarvis_os", done["id"])
    a.create_work_order("OS self-work, not shown", requestor="os", status="running")
    for s in stores.values():
        s.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"http://127.0.0.1:{PORT}/yours")
        for d in page.locator("details").all():
            d.evaluate("e => e.open = true")
        page.screenshot(path=SHOTS / "yours-page.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / "yours-page.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
