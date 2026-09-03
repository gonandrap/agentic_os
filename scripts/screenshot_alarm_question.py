"""Screenshot `/neo/question/<id>` for an alarm question, for a PR's UI evidence.

§3 of docs/superpowers/specs/2026-08-31-the-supervisor.md — the one-line safety branch
in `_question.html`: an alarm question shows a link to the alarms tab where every other
kind shows a reply box, because a reply here would message a worker mid-turn.
`scripts/screenshot_wo_alarm_record.py` is the shape it copies.

The `question` question beside it is the CONTROL: the point of the picture is that the
two render differently, and one page showing no reply box proves nothing on its own.

    uv run python scripts/screenshot_alarm_question.py
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


def seed() -> tuple[int, int]:
    """An escalated alarm question and an escalated ordinary question, side by side."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore
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

    wo = ops.create_work_order(
        "jarvis_os", "Rebuild the citation export",
        description="The CSV writer drops the DOI column on multi-author rows.")
    _, path, _ = ops.find_work_order(wo["id"], "jarvis_os")
    pstore = ProjectStore(path)
    alarm = pstore.add_alarm(wo["id"], "cache_write", 4,
                             "turn 4 re-wrote 312k tokens of cache (prefix-miss)")

    neo = NeoStore()
    q = neo.ask("jarvis_os", wo["id"],
                "Turn 4 re-sent 312k tokens of conversation and the turn is still "
                "running. Does this spend need the user?",
                context="# The alarm\nkind: cache_write\n…", kind="alarm")
    neo.mark(q["id"], "escalated",
             reason="I cannot tell a stuck turn from a large refactor from this packet")
    control = neo.ask("jarvis_os", wo["id"],
                      "Should the exporter emit one row per author, or one row per "
                      "paper with the authors joined?")
    neo.mark(control["id"], "escalated", reason="this is what the user meant, not what "
                                                "the code should do")
    neo.close()

    pstore.update_alarm(alarm["id"], status="escalated", verdict="escalate",
                        neo_question_id=q["id"],
                        verdict_reason="cannot account for the re-write from the "
                                       "evidence I was shown")
    pstore.close()
    return q["id"], control["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(alarm_qid: int, control_qid: int) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/neo/question/{alarm_qid}")
        page.screenshot(path=SHOTS / "neo-alarm-question.png")
        page.goto(f"http://127.0.0.1:{PORT}/neo/question/{control_qid}")
        page.screenshot(path=SHOTS / "neo-ordinary-question.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    alarm_qid, control_qid = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(alarm_qid, control_qid)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("neo-*-question.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
