"""Screenshot what a reasonless safety write looks like on /config, for a PR's evidence.

Companion to `screenshot_config_console.py` and seeded the same way — a throwaway
`JARVIS_HOME` and a temp catalog, never the live OS:

    uv run python scripts/screenshot_config_reasonless_write.py
"""

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


def seed() -> Path:
    """The production case: a catalog with no validation block anywhere, then the two
    writes that used to be refused — pinning the default in, and taking it back out."""
    from jarvis import ops
    from jarvis.central_store import CentralStore

    home = Path(tempfile.mkdtemp())
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787}},
        "projects": [
            {"name": "jarvis_os", "path": str(home / "jarvis_os"),
             "description": "the OS itself"},
        ],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()

    ops.adopt_config(reason="recording the catalog as it shipped")
    ops.set_config("validation.enabled", False, project="jarvis_os")  # pinned
    ops.unset_config("validation.enabled", project="jarvis_os")       # unpinned
    return catalog


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})

        page.goto(f"{base}/config")
        page.locator("h2", has_text="History").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "config-document-only-history.png")

        # The gate that is NOT weakened: a toggle that moves the effective value still
        # refuses an empty reason box, and the flash now names the command to retry.
        page.goto(f"{base}/config?scope=projects.jarvis_os&node=validation")
        row = page.locator("tr", has=page.locator("td", has_text="validation.enabled"))
        row.first.locator("button").click()
        page.wait_for_load_state()
        page.screenshot(path=SHOTS / "config-reason-refusal-names-the-retry.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)  # `ops.set_config` refuses a worker session
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("config-document-only*.png"))
                    + sorted(SHOTS.glob("config-reason-refusal*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
