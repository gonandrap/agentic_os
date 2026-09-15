"""Screenshot the surfaces auto-review changed, for a PR's UI evidence.

The claim under test is one a passing UI test cannot make: that a person reading a work
order can tell an assumption the OS decided from one they decided themselves. So the
seeded order carries one of each, plus one the OS declined to touch.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS
(`scripts/screenshot_config_console.py` is the shape this copies):

    uv run python scripts/screenshot_auto_review.py
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


def seed() -> None:
    """One work order with all three outcomes on its record at once.

    Written through the stores rather than driven through a daemon: the point of the
    picture is the RENDERING of a settled record, and standing a Neo drain up to reach
    the same rows would make the screenshot a test of the fake model instead.
    """
    from jarvis.central_store import CentralStore
    from jarvis.project_store import (
        ASSUMPTION_DECIDER_OS,
        ASSUMPTION_DECIDER_USER,
        ProjectStore,
    )

    home = Path(tempfile.mkdtemp())
    proj = home / "jarvis_os"
    proj.mkdir(parents=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "validation": {"enabled": True}},
        "projects": [{"name": "jarvis_os", "path": str(proj),
                      "description": "the OS itself",
                      "validation": {"auto_review": True}}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("jarvis_os", str(proj), "the OS itself")
    central.close()

    store = ProjectStore(proj)
    wo = store.create_work_order(title="Export the schedule as CSV",
                                 description="one row per shift, ISO dates")
    store.update_work_order(wo["id"], status="needs_review",
                            result_summary="added the exporter and its tests; "
                                           "opened a pull request")
    a1 = store.add_assumption(wo["id"], "named the helper `_render_row`, matching the "
                                        "two beside it")
    a2 = store.add_assumption(wo["id"], "put the new test beside the module's other "
                                        "tests rather than in a new file")
    a3 = store.add_assumption(wo["id"], "reused the production API key rather than "
                                        "minting a second one")
    store.review_assumption(a1, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="a naming convention, not a decision",
                            model="claude-opus-5", config_version="cfg-0007")
    store.review_assumption(a2, "accepted", decided_by=ASSUMPTION_DECIDER_USER,
                            reason="fine")
    store.add_event(wo["id"], "autoreview_accepted",
                    {"assumption_id": a1, "n": 1, "decided_by": "neo",
                     "model": "claude-opus-5", "neo_question_id": 41,
                     "reason": "a naming convention, not a decision"})
    store.add_event(wo["id"], "autoreview_held",
                    {"assumption_id": a3, "n": 3, "code": "high_stakes",
                     "reason": "assumption #3 mentions 'production API key' — the OS "
                               "does not decide those for you, whatever it thinks of "
                               "them"})
    # ...and a FOURTH: one Neo escalated rather than accepted, so `/neo` has the branch
    # that stops the user typing a reply into a worker that finished long ago.
    a4 = store.add_assumption(wo["id"], "dropped the `--since` flag from the exporter — "
                                        "the spec did not mention it")
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    q = neo.ask("jarvis_os", wo["id"],
                f"ASSUMPTION REVIEW — rule on assumption #4 of {wo['id']} in "
                f"jarvis_os, and on nothing else.\n\n# The assumption\n"
                f"dropped the `--since` flag from the exporter — the spec did not "
                f"mention it", kind="assumption")
    neo.mark(q["id"], "escalated",
             reason="Neo would have turned this down: dropping a flag changes the CLI "
                    "surface, which is yours to decide")
    neo.close()
    store.link_assumption_question(a4, q["id"])
    store.add_event(wo["id"], "autoreview_escalated",
                    {"assumption_id": a4, "n": 4, "neo_question_id": q["id"],
                     "stakes": "routine", "overridden": False,
                     "reason": "Neo would have turned this down: dropping a flag "
                               "changes the CLI surface, which is yours to decide"})
    store.flag_attention(wo["id"], "2 assumptions pending your review")
    store.close()
    print(f"seeded {wo['id']}")


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    from jarvis.project_store import ProjectStore
    from jarvis.central_store import CentralStore

    central = CentralStore()
    path = Path(central.list_projects()[0]["path"])
    central.close()
    store = ProjectStore(path)
    wo_id = store.list_work_orders()[0]["id"]
    store.close()

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"{base}/wo/jarvis_os/{wo_id}")
        page.screenshot(path=SHOTS / "auto-review-work-order.png")
        # The other changed surface: an escalated assumption on Neo's own page, where
        # the reply box would otherwise invite the user to message a finished worker.
        page.goto(f"{base}/neo")
        page.screenshot(path=SHOTS / "auto-review-neo-escalated.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("auto-review-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
