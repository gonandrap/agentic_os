"""Screenshot what an idle manager now looks like, for issue #264's UI evidence.

The claim a passing UI test cannot make: a person reading the dashboard and the work
order can tell that this manager wants nothing from them. So the seed pairs it with an
ordinary work order genuinely parked in `waiting_input` — the picture has to show the
difference, not just the absence.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_idle_manager.py
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


def seed() -> tuple[str, str]:
    """The manager and its control, under one feature order."""
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
    store.set_feature_status(fo["id"], "executing")
    manager = store.create_work_order(
        title=f"Project manager — {fo['title']}",
        description=("Coordinate this feature order. You write no code and open no pull "
                     "request: you wait for messages and file work orders under the "
                     "feature."),
        kind="manager", parent_id=fo["id"],
    )
    store.update_work_order(manager["id"], session_id="8f2c1e40-idle-manager")
    turn = store.create_turn(manager["id"], "dispatch", "coordinate it")
    store.finish_turn(turn["id"], "done",
                      result="I'm the project manager for this feature order. Idle "
                             "until a message arrives — no action to take right now.")
    store.set_status(manager["id"], "idle")

    # The control, in the same project: genuinely blocked on the user.
    parked = store.create_work_order(title="Add the --since flag to the exporter",
                                     description="filter rows by start date")
    store.update_work_order(parked["id"], session_id="1b90aa77-real-block")
    store.set_status(parked["id"], "waiting_input")
    store.flag_attention(parked["id"], "worker is waiting on your input")
    store.close()
    print(f"seeded {manager['id']} (idle) and {parked['id']} (waiting_input)")
    return str(manager["id"]), str(parked["id"])


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(manager_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # The listing: the manager is NOT in the needs-me strip, the control is.
        page.goto(f"{base}/project/jarvis_os")
        page.screenshot(path=SHOTS / "idle-manager-project.png")
        # The page itself: the badge, and the line that says what it waits for.
        page.goto(f"{base}/wo/jarvis_os/{manager_id}")
        page.screenshot(path=SHOTS / "idle-manager-work-order.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    manager_id, _ = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(manager_id)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("idle-manager-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
