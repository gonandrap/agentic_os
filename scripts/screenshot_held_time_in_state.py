"""Screenshot the "Time in state" panel under a usage-limit hold, for issue #887.

`scripts/screenshot_held_gate.py` and `scripts/screenshot_auth_hold_badge.py` are the
shape this copies: a work-order page with a hold on it. THREE IMAGES, because the claim
has three halves — the hold OPEN (the badge must read the active age, not wall), the hold
CLOSED (wall survives, beside active), and an order never held at all (the panel is
untouched for the common case).

Every shot spans the tab strip as well as the panel: the badge is the headline complaint
of the issue, and a panel shot alone would not show it.

Writes to docs/screenshots/. Everything lives in a temp `JARVIS_HOME` and a temp catalog,
so it never reads or writes the live OS:

    uv run python scripts/screenshot_held_time_in_state.py
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

#: The issue's own numbers: 5h24m in `running`, of which the order was let work 45m.
#: Away from a rounding edge, since the render happens a fraction of a second after `now`.
WALL = 5 * 3600 + 24 * 60
WORKED = 45 * 60


def _event_at(store, wo_id: str, kind: str, ts: float, payload: dict) -> None:
    """An event dated when it happened, not when this script ran."""
    store.add_event(wo_id, kind, payload)
    store.conn.execute(
        "UPDATE wo_events SET ts=? WHERE id=(SELECT MAX(id) FROM wo_events WHERE wo_id=?)",
        (ts, wo_id))


def held_order(store, title: str, *, resumed: bool) -> str:
    """Issue 887 reproduced: running since T0, one turn T0..T0+45m, then paused on the
    usage limit and never let go (`resumed=False`) or retried now (`resumed=True`)."""
    from jarvis.worker_session import PAUSE_USAGE_LIMIT

    now = time.time()
    t0 = now - WALL
    wo = store.create_work_order(title)
    store.set_status(wo["id"], "running")
    spans = [r["id"] for r in store.conn.execute(
        "SELECT id FROM wo_state_spans WHERE order_id=? ORDER BY id", (wo["id"],))]
    for span_id, ts in zip(spans, (t0 - 3600, t0)):
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?", (ts, span_id))
    store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                       (t0 - 3600, wo["id"]))
    turn = store.create_turn(wo["id"], kind="message", prompt="go")
    store.finish_turn(turn["id"], state="done")
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (t0, t0 + WORKED, turn["id"]))
    _event_at(store, wo["id"], "turn_paused", t0 + WORKED,
              {"reason": PAUSE_USAGE_LIMIT, "seq": 1})
    if resumed:
        _event_at(store, wo["id"], "turn_resumed", now, {"retried_seq": 1})
    store.conn.commit()
    return wo["id"]


def plain_order(store, title: str) -> str:
    """The control: no pause of any kind, so the panel must render as it always did."""
    now = time.time()
    wo = store.create_work_order(title)
    store.set_status(wo["id"], "running")
    spans = [r["id"] for r in store.conn.execute(
        "SELECT id FROM wo_state_spans WHERE order_id=? ORDER BY id", (wo["id"],))]
    for span_id, ts in zip(spans, (now - WALL - 3600, now - WALL)):
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?", (ts, span_id))
    store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                       (now - WALL - 3600, wo["id"]))
    turn = store.create_turn(wo["id"], kind="message", prompt="go")
    store.finish_turn(turn["id"], state="done")
    store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                       (now - WALL, now - WALL + WORKED, turn["id"]))
    store.conn.commit()
    return wo["id"]


def seed() -> dict[str, str]:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    project = make_git_project(home, "jarvis_os")
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    try:
        return {
            "held-now": held_order(pstore, "Export the fleet's spend as CSV",
                                   resumed=False),
            "after-resume": held_order(pstore, "Export the fleet's spend as CSV",
                                       resumed=True),
            "never-held": plain_order(pstore, "Export the fleet's spend as CSV"),
        }
    finally:
        pstore.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(page, wo_id: str, out: Path) -> str:
    """One clip from the tab strip to the bottom of the Gantt panel.

    The strip is in the clip on purpose: the badge and the totals line are the two
    surfaces the issue says disagree, and separate shots would not show they now agree.
    """
    page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
    page.locator("button[role=tab][data-panel=tab-states]").click()
    page.wait_for_timeout(300)
    tab = page.locator("button[role=tab][data-panel=tab-states]").bounding_box()
    panels = page.locator("#tab-states div.panel")
    last = panels.nth(panels.count() - 1).bounding_box()
    page.screenshot(path=out, clip={
        "x": 0, "y": max(tab["y"] - 16, 0), "width": 1100,
        "height": last["y"] + last["height"] - tab["y"] + 32})
    return page.locator("#tab-states").inner_text()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    orders = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)

    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1100},
                                device_scale_factor=2)
        for name, wo_id in orders.items():
            out = SHOTS / f"held-time-in-state-{name}.png"
            text = shoot(page, wo_id, out)
            print(f"\n== {out}")
            print(" ".join(text.split())[:400])
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
