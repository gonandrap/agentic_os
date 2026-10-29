"""Screenshot /alarms against a throwaway fleet, for a PR's UI evidence (wo-10a5f151)"""

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


def seed() -> None:
    """One project, three settled carriers: an orphan escalation, an acked alarm, an old one"""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import NO_TURN, ProjectStore

    home = Path(tempfile.mkdtemp())
    (home / "jarvis_os").mkdir()
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(home / "jarvis_os")}]}))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.upsert_project("jarvis_os", str(home / "jarvis_os"))
    store.close()
    ops.adopt_config(reason="screenshot fleet")

    for title, kind, status, verdict in (
            ("cut release 0.13.5", "cache-1h-dispatched", "escalated", "escalate"),
            ("a long design review", "long-turn", "acked", "ack"),
            ("an old refactor", "big-rewrite", "raised", None)):
        pstore = ProjectStore(home / "jarvis_os")
        try:
            wo = pstore.create_work_order(title, status="completed")
            alarm = pstore.add_alarm(wo["id"], kind, NO_TURN, f"{kind} fired on {title}")
            if verdict:
                pstore.update_alarm(alarm["id"], status=status, verdict=verdict,
                                    verdict_reason="the supervisor's reasoning",
                                    note="what the user is told", decided_at=alarm["ts"])
        finally:
            pstore.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"{base}/alarms")
        page.screenshot(path=SHOTS / "alarms-escalated-first.png", full_page=True)
        page.goto(f"{base}/alarms?state=escalated")
        page.screenshot(path=SHOTS / "alarms-state-filter.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("alarms-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
