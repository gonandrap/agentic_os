"""Screenshot `/evolution/rule/<id>` before and after arming from the page (wo-ded7522d)"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import screenshot_evolution as base


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(base.REPO / "src"))
    base.boot()
    base.empty_registry()
    base.seed()
    from jarvis.central_store import CentralStore

    store = CentralStore()
    det = next(d for d in store.list_detectors() if d["gap_class"] == "stranded-dependency")
    store.close()
    threading.Thread(target=base.serve, daemon=True).start()
    time.sleep(2)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{base.PORT}/evolution/rule/{det['id']}")
        page.screenshot(path=base.SHOTS / "evolution-rule-actions.png", full_page=True)
        page.fill('#rule-actions form[action$="/arm"] input[name=reason]',
                  "two clean dry-run hits on stranded edges")
        page.click('#rule-actions form[action$="/arm"] button')
        page.wait_for_load_state()
        page.screenshot(path=base.SHOTS / "evolution-rule-armed.png", full_page=True)
        browser.close()
    print(base.SHOTS / "evolution-rule-actions.png", base.SHOTS / "evolution-rule-armed.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
