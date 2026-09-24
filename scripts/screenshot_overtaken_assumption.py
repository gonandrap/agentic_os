"""Screenshot a planner assumption whose feature has already landed children (§2.6).

The claim a passing UI test cannot make: a reader ruling on a still-pending planner
assumption can SEE what a rejection now costs — children of that plan have merged, so
turning it down buys a follow-up fix, not an unwind. Derived from the timeline; nothing
extra is stored, and the row stays pending.

Spec: docs/superpowers/specs/2026-09-24-a-planner-assumption-holds-its-feature.md §2.6.
`scripts/screenshot_assumption_rulings.py` is the shape this copies, including the temp
`JARVIS_HOME` that keeps it off the live OS:

    uv run python scripts/screenshot_overtaken_assumption.py
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
PORT = 8797
SHOT = "overtaken-assumption-work-order.png"


def seed() -> str:
    """A feature, its planner, three children, one merged, one pending assumption.

    Built from rows the way `tests/test_feature_orders.py::a_bare_feature` does: what the
    note reads is a `plan_wo_id`, the children of that feature and a `pr_merged` event —
    no planner session and no plan machinery.
    """
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    proj = home / "jarvis_os"
    proj.mkdir(parents=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "validation": {"enabled": True}},
        "projects": [{"name": "jarvis_os", "path": str(proj),
                      "description": "the OS itself"}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("jarvis_os", str(proj), "the OS itself")
    central.close()

    store = ProjectStore(proj)
    fo = store.create_feature_order("Export the schedule as CSV",
                                    description="one row per shift, ISO dates")
    planner = store.create_work_order("Plan the CSV export", kind="planner",
                                      parent_id=fo["id"], status="completed")
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"], status="executing")
    store.update_work_order(planner["id"], status="needs_review",
                            result_summary="planned the export as three work orders: "
                                           "schema, exporter, docs")

    kids = [store.create_work_order(t, parent_id=fo["id"], status=s)
            for t, s in [("Add the export column", "completed"),
                         ("Write the exporter", "running"),
                         ("Document the CSV format", "pending")]]
    store.add_event(kids[0]["id"], "pr_merged", {})

    store.add_assumption(planner["id"],
                         "the export table gets a new column rather than a join table")
    store.flag_attention(planner["id"], "1 assumption pending your review")
    store.close()
    print(f"seeded {planner['id']} (planner of {fo['id']})")
    return planner["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.wait_for_selector("text=have already merged")
        page.screenshot(path=SHOTS / SHOT, full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print(SHOTS / SHOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
