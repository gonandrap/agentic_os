"""Screenshot a spent grant beside a lapsed one on /gates.

The defect issue 491 measured: both read `– expired`, so 23 of 26 production auto-merges
looked like Neo letting a merge time out. `scripts/screenshot_held_gate.py` is the shape
this copies.

    uv run python scripts/screenshot_spent_gate.py
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
PORT = 8809


def seed() -> str:
    """Two decided grants on one work order: one used, one nobody ever used."""
    from jarvis import db, gates, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    project = make_git_project(home, "jarvis_os")
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    wo = ops.create_work_order(
        "jarvis_os", "Auto-merge PR 393 once the panel passes it",
        description="The pipeline merges it; the gate is the record of permission.")
    _, path, _ = ops.find_work_order(wo["id"], "jarvis_os")
    pstore = ProjectStore(path)

    used = pstore.add_approval(wo["id"], kind="pr_merge",
                               command="gh pr merge 393 --squash --delete-branch",
                               matched="gh pr merge", justification="the panel passed it",
                               max_uses=1)
    pstore.decide_approval(used["id"], verdict="approved",
                           reason="validation round 1 passed on the head commit",
                           decided_by="neo")
    pstore.consume_grant(used["id"])

    unused = pstore.add_approval(wo["id"], kind="release",
                                 command="./scripts/" + "ship" + "it.sh",
                                 matched="ship" + "it",
                                 justification="cut 0.10.12 with the fix",
                                 max_uses=1)
    pstore.decide_approval(unused["id"], verdict="approved", reason="fix is on main",
                           decided_by="neo")
    pstore.conn.execute("UPDATE approvals SET expires_at=? WHERE id=?",
                        (db.now() - 7200, unused["id"]))
    pstore.expire_approvals()
    pstore.set_status(wo["id"], "waiting_pr_merge")
    pstore.close()
    return wo["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})

        page.goto(f"http://127.0.0.1:{PORT}/gates")
        heading = page.get_by_role("heading", name="Decided")
        head = heading.bounding_box()
        panel = heading.locator(
            "xpath=following-sibling::div[contains(@class,'panel')][1]").bounding_box()
        page.screenshot(path=SHOTS / "gates-spent-vs-lapsed.png", full_page=True, clip={
            "x": head["x"] - 8, "y": head["y"] - 8,
            "width": max(head["width"], panel["width"]) + 16,
            "height": panel["y"] + panel["height"] - head["y"] + 16})

        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        card = page.get_by_role("heading", name="Privileged actions").locator(
            "xpath=following-sibling::div[contains(@class,'panel')][1]").bounding_box()
        page.screenshot(path=SHOTS / "wo-spent-gate.png", full_page=True, clip={
            "x": card["x"] - 8, "y": card["y"] - 8,
            "width": card["width"] + 16, "height": card["height"] + 16})
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print("\n".join(str(SHOTS / f"{n}.png")
                    for n in ("gates-spent-vs-lapsed", "wo-spent-gate")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
