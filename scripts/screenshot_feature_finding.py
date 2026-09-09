"""Screenshot `/alarms` and one alarm page carrying a FEATURE-subject finding.

§1 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
`scripts/screenshot_wo_alarm_record.py` is the shape it copies, and the seed is a
DIVERGENT one on purpose: the feature and its carrier have different titles and
different statuses, so the picture shows which of the two each surface renders. Nothing
here turns the supervisor on — the columns it would fill are written directly, because
a picture is not worth a real model call:

    uv run python scripts/screenshot_feature_finding.py
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
PORT = 8799


def seed() -> str:
    """One feature order with two findings carried by two different work orders, plus
    one ordinary cost alarm — the two subject kinds side by side on one page."""
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

    burning = ops.create_work_order(
        "jarvis_os", "Rebuild the citation export",
        description="The CSV writer drops the DOI column on multi-author rows.")
    _, path, _ = ops.find_work_order(burning["id"], "jarvis_os")
    pstore = ProjectStore(path)

    alarm = pstore.add_alarm(burning["id"], "cache_write", 4,
                             "turn 4 re-wrote 312k tokens of cache (prefix-miss)")
    pstore.add_event(burning["id"], "cost_alarm",
                     {"kind": "cache_write", "seq": 4, "alarm_id": alarm["id"],
                      "reason": "turn 4 re-wrote 312k tokens of cache (prefix-miss)"})
    pstore.flag_attention(burning["id"], "turn 4 re-wrote 312k tokens of cache")

    fo = pstore.create_feature_order("Ship the reference importer",
                                     "OpenAlex, Crossref and a BibTeX fallback.")
    pstore.set_feature_status(fo["id"], "executing")
    manager = pstore.create_work_order("Coordinate the reference importer",
                                       parent_id=fo["id"], kind="manager",
                                       status="running")
    child = pstore.create_work_order("Parse the BibTeX fallback",
                                     parent_id=fo["id"], status="running")
    stalled = pstore.add_finding(
        manager["id"], kind="stalled-plan", source="health", probe="stalled-plan",
        subject_kind="feature_order", fo_id=fo["id"],
        reason="four of the six children have been pending for three days and the two "
               "that ran both failed on the same import")
    pstore.add_finding(
        child["id"], kind="failing-children", source="health",
        probe="failing-children", subject_kind="feature_order", fo_id=fo["id"],
        reason="the BibTeX child has failed twice with the same traceback")
    pstore.update_alarm(
        stalled["id"], status="acked", verdict="ack",
        verdict_reason="the two failures share a cause and the plan is not wrong",
        note="Both failures are the same missing parser dependency, not a bad plan. "
             "The feature is stalled behind one fix, not six.",
        decided_at=stalled["ts"])
    pstore.flag_attention(manager["id"],
                          f"{fo['id']}: the plan has not moved in three days")
    pstore.flag_attention(child["id"], f"{fo['id']}: this child has failed twice")
    pstore.close()
    return stalled["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(alarm_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(f"http://127.0.0.1:{PORT}/alarms")
        page.screenshot(path=SHOTS / "alarms-feature-subject.png")

        page.goto(f"http://127.0.0.1:{PORT}/alarms/jarvis_os/{alarm_id}")
        page.screenshot(path=SHOTS / "alarm-feature-subject-detail.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    alarm_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(alarm_id)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("alarm*-feature-subject*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
