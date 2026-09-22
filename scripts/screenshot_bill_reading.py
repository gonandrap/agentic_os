"""Screenshot the bill's "counted the old way" notes, for a PR's UI evidence.

Two turns whose result JSON is gone and which therefore cannot be re-derived: one read
before `USAGE_SCHEMA_VERSION` 2 (a fraction of its turn) and one before version 3 (the
whole resumed session). They are wrong in OPPOSITE directions, and the page has to say
which way, per line. Everything lives in a temp `JARVIS_HOME`, so it never touches the
live OS:

    uv run python scripts/screenshot_bill_reading.py
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


def envelope(version: int, cost: float, *, read: int, write: int, out: int) -> str:
    return json.dumps({
        "usage_v": version, "total_cost_usd": cost, "input": 2, "cache_write": write,
        "cache_read": read, "cache_1h": 0, "cache_5m": write, "output": out,
        "context_peak": read + write, "context_window": 1_000_000,
        "by_model": [{"model": "claude-opus-5", "input": 2, "cache_write": write,
                      "cache_read": read, "output": out, "cost_usd": cost}],
    })


def seed() -> tuple[str, str]:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"cold_prefix_floor": 5_000},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.set_state("catalog_path", str(catalog))
    central.conn.commit()
    central.close()

    wo = ops.create_work_order("jarvis_os", "an order counted before the fix")
    store = ProjectStore(project)
    try:
        for version, cost, tokens in ((1, 2.28, (45_689, 2_558, 941)),
                                      (2, 25.60, (2_100_000, 96_000, 11_200))):
            turn = store.create_turn(wo["id"], kind="message", prompt="p")
            store.finish_turn(turn["id"], "done", result="r", cost_usd=cost,
                              num_turns=4,
                              usage_json=envelope(version, cost, read=tokens[0],
                                                  write=tokens[1], out=tokens[2]))
    finally:
        store.close()
    return "jarvis_os", wo["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(project: str, wo_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"http://127.0.0.1:{PORT}/cost/{project}/{wo_id}")
        # The itemisation is a <details>: the dashboard has no JavaScript, so every
        # disclosure on the page is opened here rather than clicked one at a time.
        page.evaluate("document.querySelectorAll('details')"
                      ".forEach(d => d.open = true)")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "bill-counted-the-old-way.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(project, wo_id)
    print(SHOTS / "bill-counted-the-old-way.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
