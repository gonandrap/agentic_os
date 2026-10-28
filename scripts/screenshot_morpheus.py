"""Screenshot the /morpheus tab and its check modal against a throwaway store, for a PR's UI evidence"""

from __future__ import annotations

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
    # A usage-window hold, and an offer entry written before the cause was recorded (#1171)
    pr = morpheus.PullRequestBody(number=9, url="https://github.com/o/r/pull/9",
                                  title="[wo-1234abc9] thing 9",
                                  body="## How to validate in production\nlook\n")
    waiting = morpheus.enqueue(store, MorpheusConfig(), pr, project="jarvis_os", repo="o/r")
    morpheus._save_session(store, {"last_turn_kind": "scripts", "turns": 4,
                                   "usage_hold_until": time.time() + 3600,
                                   "last_error": "You've hit your session limit · resets 6:20am",
                                   "offers": {waiting["id"]: {"n": 1, "ts": time.time()}}})
    # And a kill hold on top of it: both are session-wide holds and can stand at once
    store.set_state(morpheus.RELEASE_KEY, "jarvis-0.1.1")  # the hold lifts when this tag moves
    morpheus._save_session(store, {**morpheus.session_state(store),
                                   "kills": morpheus.MAX_KILLS,
                                   "kill_hold_until": time.time() + morpheus.KILL_HOLD_SECONDS,
                                   "kill_hold_release": "jarvis-0.1.1"})
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
        page.locator("div.panel").first.screenshot(path=SHOTS / "morpheus-session-hold.png")
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
