"""Screenshot the /config tab against a throwaway fleet, for a PR's UI evidence.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_config_console.py
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
PORT = 8799


def seed() -> Path:
    """A fleet with two projects and three configuration versions, so the page has a
    history to show and every provenance case appears at least once."""
    from jarvis import ops
    from jarvis.central_store import CentralStore

    home = Path(tempfile.mkdtemp())
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log", "telegram"]}},
        "projects": [
            {"name": "jarvis_os", "path": str(home / "jarvis_os"),
             "description": "the OS itself"},
            {"name": "shared_schedule", "path": str(home / "shared_schedule"),
             "description": "the household calendar"},
        ],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()

    ops.adopt_config(reason="recording the catalog as it shipped")
    ops.set_config("validation.enabled", True, project="jarvis_os",
                   reason="trying the review panel on the OS first")
    ops.set_config("os.neo.panel.fast_path", False)
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
        page.screenshot(path=SHOTS / "config-console-current.png")

        # A project, reached by the picker rather than by scrolling past the fleet, and
        # then one node of it: the two readings the page is built around.
        page.goto(f"{base}/config?scope=projects.jarvis_os")
        page.screenshot(path=SHOTS / "config-console-project.png")
        page.goto(f"{base}/config?scope=projects.jarvis_os&node=validation")
        page.screenshot(path=SHOTS / "config-console-node.png")
        page.goto(f"{base}/config?scope=os&q=model")
        page.screenshot(path=SHOTS / "config-console-search.png")

        page.goto(f"{base}/config")
        page.locator("h2", has_text="History").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "config-console-history.png")

        # The diff is the last thing on the page, so `scroll_into_view_if_needed` stops
        # with its heading at the bottom edge and shows none of it.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "config-console-diff.png")

        # The refusal and the write, driven the way a user meets them: a safety setting
        # toggled with an empty reason box, then with one. `ops` raises the first;
        # the page flashes what it said and writes nothing.
        page.goto(f"{base}/config?scope=os&node=validation")
        row = page.locator("tr", has=page.locator("td", has_text="validation.enabled"))
        row.first.locator("button").click()
        page.wait_for_load_state()
        page.screenshot(path=SHOTS / "config-console-refusal.png")

        row = page.locator("tr", has=page.locator("td", has_text="validation.enabled"))
        row.first.locator("input[name=reason]").fill("turning the panel on for a week")
        row.first.locator("button").click()
        page.wait_for_load_state()
        page.screenshot(path=SHOTS / "config-console-toggled.png")

        # ...and the thing the boolean toggle could not do at all: a number, edited.
        page.goto(f"{base}/config?scope=os&node=defaults")
        row = page.locator("tr", has=page.locator("td",
                                                  has_text="defaults.autocompact_window"))
        row.first.locator("input[name=value]").fill("250000")
        row.first.locator("input[name=reason]").fill("shorter turns on this fleet")
        row.first.locator("button").click()
        page.wait_for_load_state()
        page.screenshot(path=SHOTS / "config-console-edited.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)  # `ops.set_config` refuses a worker session
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("config-console-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
