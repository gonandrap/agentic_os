"""Screenshot `/evolution` twice — seeded and empty — for a PR's UI evidence.

§9 of docs/superpowers/specs/2026-09-27-self-evolution.md. The pair is the point:

* `evolution-fleet.png` — a registry with two detectors, fires across three ISO weeks,
  closed fires so a median exists, and two recurrences. Every section has rows.
* `evolution-absent.png` — an EMPTY registry, which is what every install starts with.
  It must read "no rule has been armed yet, so there is no mechanical share to report —
  which is not a share of zero" where a lesser page would print `0%`.

Seeding copies `tests/test_evolution_report.py`: fires are backdated by moving
`jarvis.db.now`, which is the only sanctioned way — `record_rule_fire` stamps the clock
itself and takes no `ts`. Offsets are relative to the REAL clock, not the test's frozen
constant, so every fire lands inside the report's 90-day window whenever this is re-run.

    uv run python scripts/screenshot_evolution.py
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

DAY = 86400.0
WEEK = 7 * DAY


def boot() -> None:
    """A one-project fleet, the shape `scripts/screenshot_alarm_question.py` boots."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    project = make_git_project(home, "proj_a")
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "the project the rules were learned on"}],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)


def empty_registry() -> None:
    """Clear the builtin seed rows, so the absent shot is a genuinely empty registry."""
    from jarvis.central_store import CentralStore

    store = CentralStore()
    for table in ("rule_recurrences", "rule_fires", "remedy_rules", "detectors"):
        store.conn.execute(f"DELETE FROM {table}")
    store.conn.commit()
    store.close()


def seed() -> None:
    """Two detectors, fires across three ISO weeks, closed fires, two recurrences."""
    from jarvis import db, rules
    from jarvis.central_store import CentralStore

    real_now = db.now
    holder = {"t": real_now()}
    db.now = lambda: holder["t"]                     # the fixture's backdating idiom
    store = CentralStore()
    try:
        holder["t"] = real_now() - 3 * WEEK
        panel = store.add_detector(
            "stale-panel-hold",
            {"all": [{"field": "status", "op": "eq", "value": "needs_review"},
                     {"field": "seconds_in_status", "op": "gte", "value": 3600}]},
            project="proj_a", summary="a panel hold nobody clears",
            source="io", io_id="io-1", fix_wo_id="wo-9")
        store.add_remedy_rule(panel["id"], "nudge",
                              argument="where are you on this review?")
        holder["t"] = real_now() - 2 * WEEK - DAY
        stranded = store.add_detector(
            "stranded-dependency",
            {"all": [{"field": "status", "op": "eq", "value": "pending"},
                     {"field": "seconds_in_status", "op": "gte", "value": 7200}]},
            project="proj_a", summary="a work order held behind an edge that can never "
                                      "clear", source="io", io_id="io-2",
            fix_wo_id="wo-14")
        store.add_remedy_rule(stranded["id"], "unblock")

        def fire(detector_id, order_id, *, at, mode, cleared_after=None):
            holder["t"] = at
            row = store.record_rule_fire(detector_id=detector_id, project="proj_a",
                                         order_id=order_id, order_kind="work_order",
                                         fingerprint=f"fp-{order_id}", mode=mode,
                                         outcome=rules.APPLIED)
            if cleared_after is not None:
                store.close_rule_fire(row["id"],
                                      now=float(row["ts"]) + cleared_after)

        # Three ISO weeks, so `by_gap_class` and the week table both have rows, and more
        # than `evolution.min_samples` closed fires on each side so a median and the
        # dry-run/armed comparison both exist.
        for n, (order, secs) in enumerate(
                (("wo-101", 7200), ("wo-102", 5400), ("wo-103", 4800))):
            fire(panel["id"], order, at=real_now() - 2 * WEEK - n * DAY,
                 mode=rules.DRY_RUN, cleared_after=secs)
        for n, (order, secs) in enumerate(
                (("wo-104", 900), ("wo-105", 1200), ("wo-106", 600))):
            fire(panel["id"], order, at=real_now() - WEEK - n * DAY,
                 mode=rules.ARMED, cleared_after=secs)
        fire(stranded["id"], "wo-107", at=real_now() - 2 * DAY, mode=rules.DRY_RUN,
             cleared_after=1800)
        fire(stranded["id"], "wo-108", at=real_now() - DAY, mode=rules.DRY_RUN)

        holder["t"] = real_now() - 4 * DAY
        store.add_recurrence(gap_class="stale-panel-hold", detector_id=panel["id"],
                             project="proj_a", order_id="wo-109",
                             verdict=rules.MISSED, io_id="io-1",
                             original_fix_wo_id="wo-9",
                             note="the hold was cleared by a gate denial the condition "
                                  "does not read")
        holder["t"] = real_now() - 2 * DAY
        store.add_recurrence(gap_class="stranded-dependency",
                             detector_id=stranded["id"], project="proj_a",
                             order_id="wo-110", verdict=rules.REMEDY_FAILED,
                             io_id="io-2", original_fix_wo_id="wo-14",
                             note="unblock cut nothing: every edge was still live")
        store.conn.commit()
    finally:
        store.close()
        db.now = real_now


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(name: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/evolution")
        page.screenshot(path=SHOTS / name, full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    boot()
    empty_registry()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot("evolution-absent.png")
    seed()
    shoot("evolution-fleet.png")
    print("\n".join(str(SHOTS / n) for n in ("evolution-fleet.png",
                                             "evolution-absent.png")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
