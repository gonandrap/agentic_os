"""Screenshot a VOIDED validation round on a work-order page, for a PR's UI evidence.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_validation_void.py
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


def seed() -> str:
    """One work order per round outcome that settles, so the new badge is read BESIDE
    the ones it has to be told apart from rather than on its own."""
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.close()

    store = ProjectStore(project)
    wo = store.create_work_order("ship jarvis-0.10.5 to production")
    store.update_work_order(wo["id"], result_summary="staged and shipped 0.10.5")
    store.set_status(wo["id"], "completed")
    rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="8f2c41ad90bb1e77")
    store.close_validation_round(
        rnd["id"], "void",
        "this submission changed no files, and everything it did deliver is an effect "
        "the OS verifies itself rather than one a reviewer can judge, so no seat was "
        "asked: shipped jarvis-0.10.5: release branch and annotated tag pushed to "
        "origin, production deployed to that tag and its venv rebuilt "
        "(hand-off state: staged)")
    store.add_event(wo["id"], "validation_submitted",
                    {"round": 1, "round_id": rnd["id"],
                     "fingerprint": "8f2c41ad90bb1e77", "files": 0})
    store.add_event(wo["id"], "validation_void",
                    {"round": 1, "round_id": rnd["id"],
                     "reason": "nothing for a reviewer to judge"})

    other = store.create_work_order("add the CSV exporter")
    first = store.open_validation_round(wo_id=other["id"], fingerprint="1a2b3c4d5e6f7a8b")
    store.close_validation_round(first["id"], "rejected",
                                 "no test under tests/ touches the new branch")
    second = store.open_validation_round(wo_id=other["id"],
                                         fingerprint="9f8e7d6c5b4a3210", round=2)
    store.close_validation_round(second["id"], "passed", "the new case covers it")
    store.close()
    return wo["id"], other["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(voided: str, judged: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"{base}/wo/jarvis_os/{voided}")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "validation-void-work-order.png", full_page=True)
        page.goto(f"{base}/wo/jarvis_os/{judged}")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "validation-void-beside-judged.png",
                        full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    voided, judged = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(voided, judged)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("validation-void-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
