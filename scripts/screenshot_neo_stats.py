"""Screenshot `/neo/stats` — Neo's own report (§6 of
docs/superpowers/specs/2026-10-01-neo-observability.md), for a PR's UI evidence.

Every block the page claims has to render with real numbers, because an empty page proves
nothing: questions over a week with a RISING escalation rate, escalations carrying causes
from all three classes plus one with no cause at all (the "predate cause recording" line),
and `agent_calls` across several `agent_usage.NEO_KINDS` with `latency_ms` set on some and
NULL on others (the "measured/calls timed" rendering). Everything lives in a temp
`JARVIS_HOME`, so it never touches the live OS:

    uv run python scripts/screenshot_neo_stats.py
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
SHOT = "neo-stats.png"
PORT = 8797
DAYS = 7


def seed() -> None:
    from jarvis import db
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.set_state("catalog_path", str(catalog))
    central.conn.commit()

    store = ProjectStore(project)
    try:
        for n in range(6):
            store.create_work_order(wo_id=f"wo-{n}", title=f"order {n}", description="")
    finally:
        store.close()

    neo = NeoStore()
    try:
        def question(status: str, *, kind: str = "question", cause: str = "",
                     answered_by: str | None = None, age_days: float = 0.0,
                     wo_id: str = "wo-0") -> None:
            q = neo.ask("jarvis_os", wo_id,
                        f"a {kind} asked {age_days:.0f} days ago ({status})", kind=kind)
            neo.conn.execute(
                "UPDATE questions SET status=?, answered_by=?, escalation_cause=?, ts=? "
                "WHERE id=?",
                (status, answered_by, cause or None,
                 db.now() - age_days * 86400 - 3600, q["id"]))

        # A RISING escalation rate down the week, so the trend's bars have a shape: the
        # answered count falls as the escalated count climbs.
        rising = [(6, 9, 0), (5, 8, 1), (4, 7, 1), (3, 6, 2), (2, 5, 3), (1, 4, 4),
                  (0, 3, 5)]
        kinds = ("question", "assumption", "approval", "plan", "triage")
        for age, answered, escalated in rising:
            for i in range(answered):
                question("answered", kind=kinds[i % len(kinds)], answered_by="neo",
                         age_days=age, wo_id=f"wo-{i % 6}")
            for i in range(escalated):
                question("escalated", kind=kinds[i % len(kinds)],
                         cause=("high-stakes", "stakes-high", "neo-denied",
                                "ambiguous-intent", "scope-over-cap")[i % 5],
                         age_days=age, wo_id=f"wo-{i % 6}")
        # One per class the page groups separately, so none of the three panels is empty.
        question("escalated", cause="privileged-action", age_days=2)
        question("escalated", kind="assumption", cause="stakes-unclassified", age_days=3)
        question("failed", cause="transport-unreachable", age_days=1)
        question("failed", cause="unparseable-reply", age_days=4)
        # NO CAUSE: the row that makes the "predate cause recording" line appear.
        question("escalated", cause="", age_days=5)
        question("escalated", cause="", age_days=6)
        # Still open, and superseded — both out of the rate's denominator.
        question("queued", age_days=0)
        question("answered", answered_by="os", age_days=2)
        neo.conn.commit()
    finally:
        neo.close()

    try:
        # Several NEO_KINDS, and `latency_ms` NULL on some of them: the latency block's
        # "0/N timed" rendering only shows when a kind has no timed call at all.
        calls = [("neo_answer", "claude-sonnet-4-5", 240, 48_000, 1_900, 900),
                 ("panel_seat", "claude-opus-5", 36, 21_000, 3_400, 2_600),
                 ("validation_seat", "claude-opus-5", 18, 90_000, 5_100, 4_200),
                 ("stakes_classifier", "claude-haiku-4-5", 180, 1_200, 60, None),
                 ("digest", "claude-haiku-4-5", 14, 7_800, 420, 1_100),
                 ("supervisor", "claude-sonnet-4-5", 7, 12_000, 800, None)]
        for kind, model, n, read, out, latency in calls:
            for i in range(n):
                row = central.add_agent_call(
                    kind, project="jarvis_os", wo_id=f"wo-{i % 6}", model=model,
                    latency_ms=None if latency is None else latency + 90 * (i % 11),
                    usage={"input": 900, "cache_write": 2_400, "cache_read": read,
                           "output": out, "cache_5m": 2_400, "cache_1h": 0,
                           "total_cost_usd": 0.019, "by_model": [{"model": model}]})
                central.conn.execute(
                    "UPDATE agent_calls SET ts=? WHERE id=?",
                    (db.now() - (i % DAYS) * 86400 - 3600, row))
        central.conn.commit()
    finally:
        central.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"http://127.0.0.1:{PORT}/neo/stats?days={DAYS}")
        page.wait_for_timeout(200)
        # FULL PAGE: spend and latency are two of the six numbered requirements and sit
        # below one viewport, so a viewport shot would cut off what the PR has to show.
        page.screenshot(path=SHOTS / SHOT, full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / SHOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
