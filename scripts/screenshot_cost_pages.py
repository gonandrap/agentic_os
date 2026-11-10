"""Screenshot the `/cost` order list, collapsed and paged, for a PR's UI evidence (docs/superpowers/specs/2026-11-09-cost-overview-first.md §4)"""

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
PORT = 8811
PROJECT = "jarvis_os"
ORDERS = 53

#: The clip: the order-list panel, from its top to the pager
CLIP = """() => {
  const r = document.querySelector('details.cost-orders').closest('.panel').getBoundingClientRect();
  return {x: 0, y: Math.max(0, r.top - 14), width: 1280, height: Math.min(r.height + 28, 900)};
}"""


def seed() -> None:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.testing import FleetCostFixture, make_git_project

    home = Path(os.environ["JARVIS_HOME"])
    project = make_git_project(home, PROJECT)
    root = home / "transcripts"
    (root / "-proj").mkdir(parents=True)
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(root)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"cold_prefix_floor": 5_000},
        "projects": [{"name": PROJECT, "path": str(project), "description": "the OS itself"}],
    }))
    central = CentralStore()
    try:
        central.upsert_project(PROJECT, str(project), "the OS itself")
        central.set_state("catalog_path", str(catalog))
        central.conn.commit()
    finally:
        central.close()

    fix = FleetCostFixture(home, project, root, catalog, name=PROJECT)
    at = ops.cost_window(window="week", offset=0)["since"] + 1_800
    for i in range(ORDERS):
        wo_id = fix.order(f"paged order {i:02d}", session_id=f"sess-{i}")
        fix.turn(wo_id, started_at=at, ended_at=at + 600, cost_usd=0.1 * (i + 1))
        fix.transcript(f"sess-{i}", [fix.call_row(at=at, write=10_000 * (i + 1), out=500,
                                                  mid=f"m-{i}")])


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)

    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        for name, query in (("wo-cost-list-collapsed", ""), ("wo-cost-list-page-2", "?page=2")):
            for _ in range(60):  # the first visit may render `computing`
                page.goto(f"http://127.0.0.1:{PORT}/cost{query}")
                if page.query_selector("details.cost-orders"):
                    break
                page.wait_for_timeout(500)
            page.screenshot(path=SHOTS / f"{name}.png", clip=page.evaluate(CLIP))
            print(SHOTS / f"{name}.png")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
