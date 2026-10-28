"""Screenshot the learnings tab of `/neo` — where each ruling came from (wo-ad3c2268)"""

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
SHOT = "learning-provenance.png"
PORT = 8798


def seed() -> None:
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore

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
    central.conn.commit()
    central.close()

    neo = NeoStore()
    try:
        neo.add_learning(
            "RULE (from the user's review of wo-5b159a6a): for a routine assumption, "
            "the user accepted it. Reason: config writes are mine to approve.",
            project="jarvis_os", source="review", wo_id="wo-5b159a6a",
            origin_kind="assumption", origin_id="41,42")
        neo.add_learning(
            "The analyst proposed one order per finding; the user refused: a finding "
            "with no owning module is not actionable.",
            project="jarvis_os", source="review", wo_id="wo-9c1e77aa",
            origin_kind="finding", origin_id="io-51469add:first-turn-reads")
        neo.add_learning(
            "A two-hour turn with no file written is never explicable — escalate it.",
            project="jarvis_os", source="review", seat="supervisor",
            wo_id="wo-3fbed35e", origin_kind="alarm", origin_id="al-4b2c91de")
        neo.add_learning(
            "When a worker asked about the delimiter I answered JSON; the user "
            "corrected me: always default to CSV.",
            project="jarvis_os", source="review", question_id=2246,
            wo_id="wo-ad3c2268", origin_kind="question", origin_id="2246")
        # The honest blank: a row written before provenance existed, and a user-typed one
        neo.add_learning("I always prefer squash merges", project="jarvis_os")
        neo.conn.commit()
    finally:
        neo.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/neo#tab-learnings")
        page.click('button[data-panel="tab-learnings"]')
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / SHOT, full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / SHOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
