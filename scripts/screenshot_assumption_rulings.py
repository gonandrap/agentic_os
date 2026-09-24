"""Screenshot the OS's ruling under each assumption, for a PR's UI evidence (issue 712).

The claim a passing UI test cannot make: a reader can tell an assumption Neo escalated,
one the keyword net held, one still out with Neo and one nobody has looked at APART —
all four on one work order, which is the case that made the defect invisible.

`scripts/screenshot_auto_review.py` is the shape this copies, including the temp
`JARVIS_HOME` that keeps it off the live OS:

    uv run python scripts/screenshot_assumption_rulings.py
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


def seed() -> None:
    """One work order carrying all five outcomes at once.

    Written through the stores rather than driven through a daemon, for
    `screenshot_auto_review.py`'s reason: the picture is of a RENDERED record, and a
    Neo drain would make it a picture of the fake model.
    """
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore
    from jarvis.project_store import (
        ASSUMPTION_DECIDER_OS,
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
    decided = store.add_assumption(wo["id"], "named the helper `_render_row`, matching "
                                             "the two beside it")
    store.review_assumption(decided, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="a naming convention, not a decision",
                            model="claude-opus-5", config_version="cfg-0007")
    store.add_event(wo["id"], "autoreview_accepted",
                    {"assumption_id": decided, "n": 1, "decided_by": "neo",
                     "model": "claude-opus-5", "neo_question_id": 41,
                     "reason": "a naming convention, not a decision"})

    held = store.add_assumption(wo["id"], "reused the production API key rather than "
                                          "minting a second one")
    store.add_event(wo["id"], "autoreview_held",
                    {"assumption_id": held, "n": 2, "code": "high_stakes",
                     "reason": "assumption #2 mentions 'production' — the OS does not "
                               "decide those for you, whatever it thinks of them"})

    escalated = store.add_assumption(wo["id"], "dropped the `--since` flag from the "
                                               "exporter — the spec did not mention it")
    neo = NeoStore()
    q = neo.ask("jarvis_os", wo["id"], "ASSUMPTION REVIEW — rule on assumption #3",
                kind="assumption")
    reason = ("Neo would have turned this down: dropping a flag changes the CLI "
              "surface, which is yours to decide")
    neo.mark(q["id"], "escalated", reason=reason)
    asked = neo.ask("jarvis_os", wo["id"], "ASSUMPTION REVIEW — rule on assumption #4",
                    kind="assumption")
    neo.close()
    store.link_assumption_question(escalated, q["id"])
    store.add_event(wo["id"], "autoreview_escalated",
                    {"assumption_id": escalated, "n": 3, "neo_question_id": q["id"],
                     "stakes": "routine", "overridden": False, "reason": reason})

    out = store.add_assumption(wo["id"], "wrote the CSV header in lower case")
    store.link_assumption_question(out, asked["id"])
    store.add_event(wo["id"], "autoreview_asked",
                    {"assumption_id": out, "n": 4, "neo_question_id": asked["id"]})

    # The fifth says NOTHING, and that is the discriminating row: silence and a hold are
    # different facts, and a renderer that labelled every pending assumption would pass
    # every other assertion here.
    store.add_assumption(wo["id"], "put the new test beside the module's other tests")
    store.flag_attention(wo["id"], "4 assumptions pending your review")
    store.close()
    print(f"seeded {wo['id']}")


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    central = CentralStore()
    path = Path(central.list_projects()[0]["path"])
    central.close()
    store = ProjectStore(path)
    wo_id = store.list_work_orders()[0]["id"]
    store.close()

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.screenshot(path=SHOTS / "assumption-rulings-work-order.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / "assumption-rulings-work-order.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
