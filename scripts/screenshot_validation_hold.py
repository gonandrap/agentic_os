"""Screenshot a validation round HELD by the usage window, for a PR's UI evidence.

Issue #235. The held round is shot BESIDE a round genuinely stuck on the user, because
the whole claim is that the two no longer read alike: one says a moment, the other asks
for a decision.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_validation_hold.py
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
PORT = 8796

REFUSAL = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"


def seed() -> tuple[str, str]:
    from jarvis.central_store import CentralStore
    from jarvis.invariants import usage_hold_note
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
    reopens = time.time() + 11.2 * 3600  # the live outage's own duration, wo-752eced8

    held = store.create_work_order("file every follow-up finding as a backlog item")
    store.set_status(held["id"], "validating")
    rnd = store.open_validation_round(wo_id=held["id"], fingerprint="7f8a9b0c1d2e3f40")
    store.close_validation_round(int(rnd["id"]), "failed", usage_hold_note(reopens))
    store.add_event(held["id"], "validation_submitted",
                    {"round": 1, "round_id": int(rnd["id"]),
                     "fingerprint": "7f8a9b0c1d2e3f40", "files": 9})
    store.add_event(held["id"], "validation_failed",
                    {"round": 1, "cause": "usage_limit", "reopens_at": reopens,
                     "error": REFUSAL})

    # The pair: a round that really did give up and really does want the user. Shot so
    # the two are compared rather than each believed on its own.
    stuck = store.create_work_order("add the CSV exporter")
    store.set_status(stuck["id"], "needs_review")
    other = store.open_validation_round(wo_id=stuck["id"], fingerprint="1a2b3c4d5e6f7a8b")
    store.close_validation_round(
        int(other["id"]), "escalated",
        "the review could not be run: the validator was unreachable 3 times in a row. "
        "Nobody has judged the work.")
    store.add_event(stuck["id"], "validation_escalated",
                    {"round": 1, "round_id": int(other["id"]),
                     "reason": "the validator was unreachable 3 times in a row"})
    store.flag_attention(stuck["id"],
                         "the review could not be satisfied — the work needs your "
                         "judgement")
    store.close()
    return held["id"], stuck["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(held: str, stuck: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"{base}/wo/jarvis_os/{held}")
        page.wait_for_timeout(250)
        page.screenshot(path=SHOTS / "validation-hold-work-order.png", full_page=True)
        page.goto(f"{base}/")
        page.wait_for_timeout(250)
        page.screenshot(path=SHOTS / "validation-hold-beside-escalated.png",
                        full_page=True)
        page.goto(f"{base}/wo/jarvis_os/{stuck}")
        page.wait_for_timeout(250)
        page.screenshot(path=SHOTS / "validation-hold-the-real-give-up.png",
                        full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    held, stuck = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(held, stuck)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("validation-hold-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
