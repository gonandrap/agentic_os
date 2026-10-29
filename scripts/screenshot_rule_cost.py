"""Screenshot the per-rule cost column on /evolution and the cost row on one rule's page (wo-85906675)"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
SHOTS = REPO / "docs" / "screenshots"


def spend() -> str:
    """One armed fire on the panel detector: a Neo gate review and a filed order with a retry"""
    from jarvis import ops, rules
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    central = CentralStore()
    try:
        det = next(d for d in central.list_detectors() if d["gap_class"] == "stale-panel-hold")
        rm = central.add_remedy_rule(det["id"], "file_work_order",
                                     argument="fix the stale hold at its root")
        central.record_rule_fire(
            detector_id=det["id"], project="proj_a", order_id="wo-104",
            order_kind="work_order", fingerprint="fp", mode=rules.ARMED,
            outcome=rules.PROPOSED, remedy_rule_id=rm["id"], alarm_id="al-1",
            question_id=901)
        for usd in (0.31, 0.12):
            central.add_agent_call("neo", project="proj_a", wo_id="wo-104",
                                   question_id=901, usage={"total_cost_usd": usd})
        fix = ops.create_work_order("proj_a", "Fix the stale panel hold at its root")
        central.note_fire_orders("al-1", [fix["id"]])
        central.conn.commit()
    finally:
        central.close()
    store = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        for usd in (2.40, 0.85):
            turn = store.create_turn(fix["id"], kind="message", prompt="work")
            store.finish_turn(turn["id"], "done", result="done", cost_usd=usd,
                              usage_json=json.dumps({"total_cost_usd": usd}))
    finally:
        store.close()
    return str(det["id"])


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    import screenshot_evolution as base
    from playwright.sync_api import sync_playwright

    base.boot()
    base.empty_registry()
    base.seed()
    det_id = spend()
    threading.Thread(target=base.serve, daemon=True).start()
    time.sleep(2)
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(f"http://127.0.0.1:{base.PORT}/evolution")
        page.locator("table").first.screenshot(path=SHOTS / "rule-cost-list.png")
        page.goto(f"http://127.0.0.1:{base.PORT}/evolution/rule/{det_id}")
        page.locator("table").first.screenshot(path=SHOTS / "rule-cost-detail.png")
        browser.close()
    print(SHOTS / "rule-cost-list.png", SHOTS / "rule-cost-detail.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
