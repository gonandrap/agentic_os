"""Screenshot `/stuck` NOT judging a round that is waiting for GitHub's checks.

§1 and §3 of
docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md.
`scripts/screenshot_health_surfaces.py` is the shape this copies, and
`scripts/screenshot_ci_hold_badge.py` is where the hold event comes from: a
`validation_failed` event carrying `round`, `cause=VALIDATION_CI_CAUSE` and `reopens_at`.
`scripts/screenshot_held_time_in_state.py` is where the AGEING comes from — the span rows
are back-dated, because 20 minutes of real waiting is not a thing a script can buy.

ONE PICTURE, because the claim has one half on this page: 20 minutes in `validating`
against a 15-minute threshold, and the order is on the record with `excluded` reading
`ci_pending` instead of over its threshold. The prose above the table is in the clip on
purpose — the sentence that enumerates the exclusions is the other half of the change.

Writes to docs/screenshots/. Everything lives in a temp `JARVIS_HOME` and a temp catalog,
so it never reads or writes the live OS:

    uv run python scripts/screenshot_ci_hold_excluded.py
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
OUT = SHOTS / "ci-hold-excluded-stuck.png"
PORT = 8812

#: 20 minutes in `validating`, past the 15-minute default threshold
#: (`catalog.DEFAULT_FLEET_HEALTH_THRESHOLDS`) — so the row is only off the stuck list
#: because the hold excludes it, not because it is young.
WAITED = 20 * 60
#: The hold is LIVE: `validation_hold` excludes only while `reopens_at` is in the future.
REOPENS_IN = 5 * 60
CHECKS = "unit (3.13), evals"


def order(store) -> str:
    """One order in `validating` for 20 minutes with a live `ci_pending` hold."""
    from jarvis.project_store import VALIDATION_CI_CAUSE

    now = time.time()
    since = now - WAITED
    wo = store.create_work_order(
        "Judge the panel's round only once the checks report",
        description="A round held for CI is the OS deliberately not acting.")
    store.set_status(wo["id"], "validating")
    # The span rows carry the age: `ops.state_durations` reads `wo_state_spans`, and the
    # whole figure the sweep judges is the open span's entry moment.
    spans = [r["id"] for r in store.conn.execute(
        "SELECT id FROM wo_state_spans WHERE order_id=? ORDER BY id", (wo["id"],))]
    for span_id, ts in zip(spans, (since - 60, since)):
        store.conn.execute("UPDATE wo_state_spans SET ts=? WHERE id=?", (ts, span_id))
    store.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                       (since - 60, wo["id"]))
    round_row = store.open_validation_round(wo_id=wo["id"], fingerprint="9d3c1b7fa204")
    store.add_event(wo["id"], "validation_failed", {
        "round": int(round_row["round"]), "cause": VALIDATION_CI_CAUSE,
        "reopens_at": now + REOPENS_IN,
        "reason": f"waiting for GitHub to finish the checks on this pull request "
                  f"before judging it: {CHECKS}"}, ts=since)
    store.conn.commit()
    return wo["id"]


def seed() -> str:
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
                      "description": "the OS itself",
                      "validation": {"enabled": True}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    try:
        return order(pstore)
    finally:
        pstore.close()


def check(wo_id: str) -> None:
    """The picture is only evidence if the reader agrees — so read it before shooting."""
    from jarvis import ops
    from jarvis.project_store import VALIDATION_CI_CAUSE

    rows = [r for r in ops.stuck_report() if r["id"] == wo_id]
    assert rows, f"{wo_id} is not on the stuck report at all"
    row = rows[0]
    assert row["status"] == "validating", row["status"]
    assert row["excluded"] == VALIDATION_CI_CAUSE, row["excluded"]
    assert not row["stuck"], row["reason"]
    assert row["seconds_in_status"] >= WAITED, row["seconds_in_status"]
    assert row["threshold_seconds"] == 15 * 60, row["threshold_seconds"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> str:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1180, "height": 1000},
                                device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{PORT}/stuck")
        page.wait_for_timeout(300)
        head = page.locator("h2", has_text="On the record").first.bounding_box()
        panel = page.locator("div.panel").nth(1).bounding_box()
        page.screenshot(path=OUT, full_page=True, clip={
            "x": max(head["x"] - 10, 0), "y": head["y"] - 10,
            "width": max(head["width"], panel["width"]) + 20,
            "height": panel["y"] + panel["height"] - head["y"] + 20})
        text = page.locator("div.panel").nth(1).inner_text()
        browser.close()
    return text


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed()
    check(wo_id)
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    text = shoot()
    assert "ci_pending" in text, text
    print(OUT, OUT.stat().st_size)
    print(" ".join(text.split())[:600])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
