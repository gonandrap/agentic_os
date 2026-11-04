"""Screenshot /backlog and one item page against a throwaway fleet, for a PR's UI evidence"""

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


def seed() -> str:
    """Items in every status, one with three origins and a folded duplicate"""
    from jarvis.central_store import CentralStore

    c = CentralStore()
    try:
        c.upsert_project("jarvis_os", tempfile.mkdtemp(), "the OS itself")

        def propose(title, **kw):
            f = {"item_type": "task", "severity": "medium", "suggested_kind": "wo",
                 "origin_refs": [{"kind": "order", "id": "wo-1a2b3c4d"}]}
            f.update(kw)
            return c.propose_backlog("jarvis_os", title, **f)["id"]

        kept = propose("Stuck sweep misses budget-capped orders", item_type="bug",
                       severity="major", origin_fo_id="fo-89ba3166",
                       origin_refs=[{"kind": "neo_question", "id": 412},
                                    {"kind": "order", "id": "wo-1a2b3c4d"},
                                    {"kind": "gate", "project": "jarvis_os", "id": 77}])
        c.review_backlog(kept, "quarantined", rule="neo-scope", note="scope unclear",
                         neo_question_id=412)
        dup = propose("Budget-capped orders show as stuck")
        c.review_backlog(dup, "dropped", rule="dedupe", note="restates it",
                         duplicate_of=kept)
        opened = propose("Cost page window selector", severity="minor", suggested_kind="fo")
        c.review_backlog(opened, "open", rule="auto-open", note="small and clear")
        propose("Search ranks settled rows last", severity="critical", item_type="bug")
        done = c.add_backlog("jarvis_os", "Typed by hand, finished")["id"]
        c.mark_backlog(done, "done")
        return kept
    finally:
        c.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(kept: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 800})
        page.goto(f"{base}/backlog")
        page.screenshot(path=SHOTS / "backlog-default.png")
        page.goto(f"{base}/backlog?status=dropped")
        page.screenshot(path=SHOTS / "backlog-dropped.png")
        page.goto(f"{base}/backlog/{kept}")
        page.screenshot(path=SHOTS / "backlog-item.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, str(REPO / "src"))
    kept = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(kept)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("backlog-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
