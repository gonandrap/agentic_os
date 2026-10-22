"""Screenshot the /morpheus tab and its check modal against a throwaway store, for a PR's UI evidence"""

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

SCRIPT = "\n".join(
    ["#!/usr/bin/env bash", "set -euo pipefail", "# exit 0 = validated, 75 = not yet"]
    + [f"jarvis status --json | jq -e '.morpheus.items[{n}].state == \"validated\"' >/dev/null"
       for n in range(30)]
    + ["exit 0"])


def seed() -> None:
    from jarvis import morpheus
    from jarvis.catalog import MorpheusConfig
    from jarvis.central_store import CentralStore

    store = CentralStore()
    for n in (7, 8):
        pr = morpheus.PullRequestBody(number=n, url=f"https://github.com/o/r/pull/{n}",
                                      title=f"[wo-1234abc{n}] thing {n}",
                                      body="## How to validate in production\nlook\n")
        item = morpheus.enqueue(store, MorpheusConfig(), pr, project="jarvis_os", repo="o/r")
        store.update_morpheus_item(item["id"], script=SCRIPT, script_source="agent",
                                   state="monitoring", checks=3, last_exit=75,
                                   last_output="\n".join(f"line {k}: not yet" for k in range(40)))
    # Two script-less items: one mid-backoff, one Morpheus gave up on (#1161)
    now = time.time()
    queued = []
    for n in (9, 10):
        pr = morpheus.PullRequestBody(number=n, url=f"https://github.com/o/r/pull/{n}",
                                      title=f"[wo-1234abc{n}] thing {n}",
                                      body="## How to validate in production\nlook\n")
        queued.append(morpheus.enqueue(store, MorpheusConfig(), pr,
                                       project="jarvis_os", repo="o/r")["id"])
    store.set_state(morpheus.SESSION_KEY, json.dumps({
        "session_id": "4f1c9a2e-7b3d-4e8f-9a1b-2c3d4e5f6a7b", "turns": 7, "compactions": 1,
        "context_share": 0.18, "context_peak": 36000, "context_window": 200000,
        "last_turn_ts": now - 900, "last_turn_kind": "scripts", "compact_due": False,
        "offers": {queued[0]: {"n": 1, "ts": now - 900},
                   queued[1]: {"n": morpheus.MAX_OFFERS, "ts": now - 3 * 6 * 3600}}}))
    store.close()


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
        page.goto(f"http://127.0.0.1:{PORT}/morpheus")
        page.screenshot(path=SHOTS / "morpheus-table.png", full_page=True)
        page.locator("button.peek", has_text="agent").first.click()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "morpheus-check-modal.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("morpheus-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
