"""Screenshot a work order's record carrying a cost alarm, for a PR's UI evidence.

§4 of docs/superpowers/specs/2026-08-31-the-supervisor.md. Writes PNGs to
docs/screenshots/; `scripts/screenshot_config_console.py` is the shape it copies.
Everything lives in a temp `JARVIS_HOME` and a temp catalog, so it never touches the
live OS, and the supervisor is never turned on — every column it would fill is written
here directly, because a picture is not worth a real model call:

    uv run python scripts/screenshot_wo_alarm_record.py
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
    """One order with an alarm the supervisor escalated and Neo then settled — the
    longest of the four shapes, so all four event kinds appear on one page."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
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

    wo = ops.create_work_order(
        "jarvis_os", "Rebuild the citation export",
        description="The CSV writer drops the DOI column on multi-author rows.")
    _, path, _ = ops.find_work_order(wo["id"], "jarvis_os")
    pstore = ProjectStore(path)
    alarm = pstore.add_alarm(wo["id"], "cache_write", 4,
                             "turn 4 re-wrote 312k tokens of cache (prefix-miss)")
    pstore.add_event(wo["id"], "cost_alarm",
                     {"kind": "cache_write", "seq": 4, "alarm_id": alarm["id"],
                      "reason": "turn 4 re-wrote 312k tokens of cache (prefix-miss)"})
    pstore.add_event(wo["id"], "alarm_escalated",
                     {"alarm_id": alarm["id"], "neo_question_id": 12})
    pstore.add_event(wo["id"], "alarm_advice",
                     {"alarm_id": alarm["id"], "neo_question_id": 12,
                      "answer": "A prefix miss on a worker that ran `uv sync` mid-turn "
                                "— the tool output landed before the cached prefix. It "
                                "will not repeat on the next turn; let it finish."})
    pstore.add_event(wo["id"], "alarm_reviewed",
                     {"alarm_id": alarm["id"], "verdict": "ack",
                      "reason": "explicable: one-off prefix miss, not a stuck turn",
                      "note": "This turn re-sent its whole conversation once, which "
                              "cost about $4. Nothing is wrong with the work — it is "
                              "still going."})
    pstore.update_alarm(alarm["id"], status="acked", verdict="ack",
                        neo_question_id=12,
                        verdict_reason="Neo: one-off prefix miss, not a stuck turn",
                        note="This turn re-sent its whole conversation once.")
    pstore.close()
    return f"/wo/jarvis_os/{wo['id']}"


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(url: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}{url}")
        page.screenshot(path=SHOTS / "wo-alarm-conversation.png")

        page.locator("button", has_text="Timeline").click()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "wo-alarm-timeline.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    url = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(url)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("wo-alarm-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
