"""Screenshot a decision order's page against a throwaway fleet, for a PR's UI evidence"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8798


def seed() -> tuple[str, str]:
    """One project with a delivered decision order"""
    from jarvis import db, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    path = Path(tempfile.mkdtemp()) / "jarvis_os"
    path.mkdir()
    central = CentralStore()
    central.upsert_project("jarvis_os", str(path), "the OS itself")
    central.close()
    do = ops.create_decision_order(
        "jarvis_os", "drop the validation panel?",
        "Gather the panel's cost and catch rate so I can decide whether to drop it.")
    store = ProjectStore(path)
    try:
        wo = store.create_work_order("research it", kind="researcher", parent_id=do["id"],
                                     status="completed")
        store.update_feature_order(do["id"], plan_wo_id=wo["id"], plan=db.to_json({
            "summary": "The panel cost $412 over 30 days and caught 9 real defects; "
                       "dropping it saves ~$14/day.",
            "findings": [
                {"key": "f1", "title": "spend", "detail": "$412 across 118 rounds.",
                 "evidence": ["jarvis cost --fleet --days 30: panel $412.10"]},
                {"key": "f2", "title": "catches", "detail": "9 rejections later confirmed.",
                 "evidence": ["jarvis validation show wo-1a2b3c4d"]}],
            "options": [
                {"key": "a", "title": "keep it", "pros": ["9 catches"], "cons": ["$412/mo"],
                 "cost": "$412/mo"},
                {"key": "b", "title": "tester seat only", "pros": ["~70% cheaper"],
                 "cons": ["misses design flaws"], "cost": "$120/mo"}],
            "recommendation": {"option": "b", "why": "keeps 7 of the 9 catches"},
            "proposed_orders": [
                {"key": "p1", "as": "wo", "project": "", "title": "run the tester seat only",
                 "description": "set validation.seats to tester"}]}))
        store.set_feature_status(do["id"], "awaiting_decision")
        store.flag_feature_attention(
            do["id"], f"decision awaiting you — jarvis do show {do['id']}")
    finally:
        store.close()
    return "jarvis_os", do["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, do_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"http://127.0.0.1:{PORT}/do/{project}/{do_id}")
        page.screenshot(path=SHOTS / "decision-order-page.png", full_page=True)
        page.goto(f"http://127.0.0.1:{PORT}/project/{project}")
        page.click("button[data-panel=tab-decisions]")
        page.screenshot(path=SHOTS / "decision-order-project-tab.png")
        page.goto(f"http://127.0.0.1:{PORT}/")
        page.click("button[data-panel=tab-new-do]")
        page.locator("#tab-new-do").screenshot(path=SHOTS / "decision-order-create-form.png")
        browser.close()
    print(SHOTS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
