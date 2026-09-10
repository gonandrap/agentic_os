"""Screenshot the three halves of `/alarms` and one health finding's own page.

§6 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
`scripts/screenshot_feature_finding.py` is the shape this copies, and NOTHING HERE TURNS
THE SUPERVISOR ON: every column a sweep, a proposal or an applied remedy would fill is
written directly, because a picture is not worth a real model call.

    uv run python scripts/screenshot_health_surfaces.py
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
PORT = 8801

#: One picture per half, plus the anchor page. The halves are three different asks and
#: the whole argument of §6.1 is that they stay three, so they are shot separately.
SHOWN = ("alarms-health-asking", "alarms-health-feedback", "alarms-health-record")


def seed() -> tuple[str, str]:
    """A proposal awaiting permission, an applied remedy awaiting feedback, and a
    settled cost alarm on the record — the three halves, each with a row."""
    from jarvis import ops, remedies
    from jarvis.central_store import CentralStore
    from jarvis.project_store import NO_TURN, ProjectStore
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

    seeded = ops.create_work_order(
        "jarvis_os", "Backfill the citation index",
        description="Re-run the importer over everything filed before March.")
    _, path, _ = ops.find_work_order(seeded["id"], "jarvis_os")
    pstore = ProjectStore(path)

    # -- the top half: a feature finding whose remedy is waiting on the user ---------
    fo = pstore.create_feature_order("Ship the reference importer",
                                     "OpenAlex, Crossref and a BibTeX fallback.")
    pstore.set_feature_status(fo["id"], "executing")
    manager = pstore.create_work_order("Coordinate the reference importer",
                                       parent_id=fo["id"], kind="manager",
                                       status="running")
    stalled = pstore.add_finding(
        manager["id"], kind="no-progress", seq=NO_TURN, source="health",
        probe="no-progress", subject_kind="feature_order", fo_id=fo["id"],
        reason="nothing has moved on the plan or on any of the six children for "
               "nineteen hours, and the newest event is the plan being approved")
    argument = ("Ask the manager whether it is waiting on the BibTeX child or has "
                "stopped dispatching.")
    approval = pstore.add_approval(
        manager["id"], remedies.GATE_KIND,
        remedies.INTENT.format(alarm_id=stalled["id"], remedy="nudge",
                               subject_id=fo["id"], argument=argument),
        justification="nineteen hours is past any turn this feature could still be in",
        evidence="# The unit\nfeature order, executing, 6 children (4 pending, 2 "
                 "failed)\n# The newest turn\nturn 3 ended 19h ago, 0 tool calls "
                 "since\n# What the record says\nno message in either direction",
        max_uses=1)
    pstore.update_alarm(
        stalled["id"], status="proposed", verdict="propose",
        verdict_reason="nineteen hours with no event of any kind is not a long turn",
        remedy="nudge", remedy_argument=argument,
        remedy_approval_id=approval["id"], decided_at=stalled["ts"])
    pstore.add_event(manager["id"], "remedy_proposed",
                     {"alarm_id": stalled["id"], "remedy": "nudge",
                      "approval_id": approval["id"], "argument": argument})
    pstore.flag_attention(manager["id"],
                          f"{fo['id']}: nothing has moved for nineteen hours")

    # -- the middle half: a remedy that was granted, applied, and not yet reviewed ---
    circling = pstore.add_finding(
        seeded["id"], kind="going-in-circles", seq=NO_TURN, source="health",
        probe="going-in-circles",
        reason="the same three tests have failed in each of the last four turns and "
               "the error text has not changed")
    granted = pstore.add_approval(
        seeded["id"], remedies.GATE_KIND,
        remedies.INTENT.format(alarm_id=circling["id"], remedy="nudge",
                               subject_id=seeded["id"], argument="…"),
        max_uses=1)
    pstore.decide_approval(granted["id"], "approved", "the ask is one message and the "
                           "loop is real", decided_by="neo")
    pstore.update_alarm(
        circling["id"], status="acked", verdict="propose",
        verdict_reason="four turns at the same failure is re-spent effort, not a fix",
        note="It has re-run the same failing tests four times. I asked it where it is.",
        remedy="nudge", remedy_argument="Ask it what it has ruled out so far.",
        remedy_approval_id=granted["id"], decided_at=circling["ts"])
    pstore.add_event(seeded["id"], "remedy_applied",
                     {"alarm_id": circling["id"], "remedy": "nudge",
                      "approval_id": granted["id"], "use": 1,
                      "result": f"queued one message on {seeded['id']} asking it to "
                                f"say where it is (delivered on its next turn)"})

    # -- the bottom half: the record, including an ordinary cost alarm ---------------
    old = pstore.add_alarm(seeded["id"], "big-rewrite", 4,
                           "turn 4 re-wrote 312k tokens of cache (prefix-miss)")
    pstore.update_alarm(old["id"], status="acked", verdict="ack",
                        verdict_reason="a prefix miss after a subagent join",
                        note="Known cause, already fixed on main.",
                        review_status="approved", decided_at=old["ts"])
    pstore.close()
    return stalled["id"], fo["id"]


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
        for index, name in enumerate(SHOWN):
            # Heading AND panel: the heading is what says which of the three asks this
            # is, and a panel on its own is unreadable out of context.
            head = page.locator("h2").nth(index).bounding_box()
            panel = page.locator("div.panel").nth(index).bounding_box()
            # `full_page` even though this is a clip: without it the third half is
            # below the fold and the clip comes back as a strip of heading.
            page.screenshot(path=SHOTS / f"{name}.png", full_page=True, clip={
                "x": head["x"] - 8, "y": head["y"] - 8,
                "width": max(head["width"], panel["width"]) + 16,
                "height": panel["y"] + panel["height"] - head["y"] + 16})

        page.goto(f"http://127.0.0.1:{PORT}/alarms/jarvis_os/{alarm_id}")
        page.screenshot(path=SHOTS / "alarm-health-proposal.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    alarm_id, _ = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(alarm_id)
    print("\n".join(str(SHOTS / f"{n}.png") for n in
                    (*SHOWN, "alarm-health-proposal")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
