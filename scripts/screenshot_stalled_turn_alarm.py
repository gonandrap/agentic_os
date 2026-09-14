"""Screenshot `/alarms` carrying a `stalled-turn` finding, for a PR's UI evidence.

§7.4 of docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md (issue 227). The
page's point is the contrast: the new kind sits beside the three that say money is
going out and says that none is. `scripts/screenshot_wo_alarm_record.py` is the shape
this copies, temp `JARVIS_HOME` and all, so it never touches the live OS:

    uv run python scripts/screenshot_stalled_turn_alarm.py
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
PORT = 8804


def seed() -> None:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.inspection import STALL_ALARM, TURN_ALARM
    from jarvis.project_store import ProjectStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    project = make_git_project(home, "jarvis_os")
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    dead = ops.create_work_order(
        "jarvis_os", "Make a validation round cheap enough to be complete",
        description="The panel pays a cache write per seat.")
    busy = ops.create_work_order(
        "jarvis_os", "Rebuild the citation export",
        description="The CSV writer drops the DOI column on multi-author rows.")
    _, path, _ = ops.find_work_order(dead["id"], "jarvis_os")
    pstore = ProjectStore(path)
    # The live case, as it now reads: no API call, so no claim about money.
    pstore.add_alarm(dead["id"], STALL_ALARM, 3,
                     "this turn has been open 65 minutes and has made no API call at "
                     "all — the work never started, and nothing has been spent on it "
                     f"— `jarvis inspect {dead['id']}`")
    pstore.flag_attention(dead["id"], "a turn open with no API call")
    # Beside a turn that really is burning, which is the contrast worth seeing.
    pstore.add_alarm(busy["id"], TURN_ALARM, 2,
                     "this turn has been running 74 minutes and is still being billed "
                     "(31 API calls, the last one 2m ago, 4,812,043 tokens for $18.44 "
                     f"so far) — `jarvis inspect {busy['id']}`")
    pstore.close()


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
        page.goto(f"http://127.0.0.1:{PORT}/alarms")
        page.screenshot(path=SHOTS / "stalled-turn-alarm.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / "stalled-turn-alarm.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
