"""Screenshot the budget box on the dashboard's two create forms, for a PR's UI evidence.

`scripts/screenshot_budget.py` is the shape this copies — it shot the budget on the order
pages, which is where a ceiling could be set before this change. Two shots:

  budget-at-creation.png          both create forms as a user first meets them: the
                                  budget box and whether its placeholder actually fits
  budget-at-creation-filled.png   the same two, carrying the amounts they accept

There is deliberately no shot of the browser REFUSING a malformed amount: Chromium's
constraint bubble is a native popup and is not composited into a headless screenshot.
`tests_browser/test_ui_browser.py::test_a_typo_in_the_budget_box_never_costs_the_description`
is the evidence for that half.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME` and
a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_budget_at_creation.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8804

WO_FORM = "form[action='/wo/create']"
FO_FORM = "form[action='/fo/create']"


def seed() -> None:
    from jarvis import ops
    from jarvis.central_store import CentralStore

    home = Path(tempfile.mkdtemp())
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "README.md").write_text("# jarvis_os\n")
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)


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
        page.goto(f"http://127.0.0.1:{PORT}/")
        page.get_by_text("New work order").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "budget-at-creation.png")

        page.fill(f"{WO_FORM} input[name='title']", "Trace every cache write to its cause")
        page.fill(f"{WO_FORM} textarea[name='description']",
                  "Label cold-start, ttl-expiry and prefix-miss apart.")
        page.fill(f"{WO_FORM} input[name='budget']", "$5.00")
        page.fill(f"{FO_FORM} input[name='title']", "A budget per order")
        page.fill(f"{FO_FORM} textarea[name='description']",
                  "Cap what one order may spend, and stop it at the cap.")
        page.fill(f"{FO_FORM} input[name='budget']", "$50.00")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "budget-at-creation-filled.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(q) for q in sorted(SHOTS.glob("budget-at-creation*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
