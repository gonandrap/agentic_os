"""Screenshot the Learnings section of a work order page (wo-9f943f10)"""

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
SHOT = "wo-learnings.png"
PORT = 8843
WO = {}


def seed() -> None:
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.set_state("catalog_path", str(catalog))
    ps = ProjectStore(str(project))
    try:
        WO.update(ps.create_work_order("fix the held-panel badge"))
    finally:
        ps.close()
    central.add_knowledge("HELD PANELS HIDE STUCK ORDERS: the badge reads the hold, "
                          "not the order's status.", project="jarvis_os", topic="ui",
                          wo_id=WO["id"])
    central.add_knowledge("Run only targeted UI tests for template changes.",
                          project="", topic="testing", wo_id=WO["id"])
    old = central.add_knowledge("The badge is derived from pr_state.", project="jarvis_os",
                                topic="ui")
    central.retract_knowledge(old["id"], "superseded: derived from the timeline",
                              wo_id=WO["id"])
    central.conn.commit()
    central.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{WO['id']}#learnings")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / SHOT)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(8)
    shoot()
    print(SHOTS / SHOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
