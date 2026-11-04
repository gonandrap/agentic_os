"""Screenshot the promote popup naming a dropped dependency, against a throwaway fleet"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8797


def seed() -> str:
    """One lone item whose only dependency was dropped: the popup used to skip this shape"""
    from jarvis.central_store import CentralStore

    store = CentralStore()
    store.upsert_project("jarvis_os", tempfile.mkdtemp())
    gone = store.add_backlog("jarvis_os", "Old approach, rejected in review")
    store.mark_backlog(gone["id"], "dropped")
    item = store.add_backlog("jarvis_os", "Ship the new approach", depends_on=[gone["id"]])
    store.close()
    return item["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(item_id: str) -> Path:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / "promote-dialog-dead-deps.png"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 700})
        page.goto(f"http://127.0.0.1:{PORT}/backlog/{item_id}")
        page.locator(f'form[data-promote-plan="/backlog/{item_id}/promote-plan"] button').click()
        page.wait_for_selector("#promote-dialog[open]")
        print(page.locator("#promote-dialog-dead").inner_text())
        page.screenshot(path=out)
        browser.close()
    return out


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    item_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    print(shoot(item_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
