"""Screenshot an `unverified` monitoring order against a throwaway store, for a PR's UI evidence"""

from __future__ import annotations

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
    from jarvis import morpheus
    from jarvis.catalog import MorpheusConfig
    from jarvis.central_store import CentralStore

    store = CentralStore()
    ids = []
    for n, state in ((7, "validated"), (8, "unverified"), (9, "failed")):
        pr = morpheus.PullRequestBody(number=n, url=f"https://github.com/o/r/pull/{n}",
                                      title=f"[wo-1234abc{n}] thing {n}",
                                      body="## How to validate in production\nlook\n")
        item = morpheus.enqueue(store, MorpheusConfig(), pr, project="jarvis_os", repo="o/r")
        store.update_morpheus_item(item["id"], script="exit 1", script_source="agent", state="failed")
        ids.append(item["id"])
        if state == "unverified":
            morpheus.settle_unverified(store, item["id"], "inv-1234abcd",
                                       "shipped behaviour is correct; the script's predicate is wrong")
        elif state == "validated":
            store.update_morpheus_item(item["id"], state="validated")
    store.close()
    return ids[1]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(item_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 700})
        page.goto(f"http://127.0.0.1:{PORT}/mo")
        page.screenshot(path=SHOTS / "mo-unverified-list.png", full_page=True)
        page.goto(f"http://127.0.0.1:{PORT}/mo/{item_id}")
        page.screenshot(path=SHOTS / "mo-unverified-item.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, str(REPO / "src"))
    item_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(item_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
