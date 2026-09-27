"""Screenshot the three surfaces an improvement order renders on, for a PR's evidence.

§6.1 and §6.3 of docs/superpowers/specs/2026-09-23-improvement-orders.md.
`scripts/screenshot_bill_reading.py` is the shape it copies. Two orders are seeded: one
parked in `plan_review` with a pending, an accepted and a rejected finding, and one with
spend on both halves of its bill — the analyst's transcript and the order's own calls.
Everything lives in a throwaway JARVIS_HOME, so it never touches the live OS:

    uv run python scripts/screenshot_improvement_order.py
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
PORT = 8797
PROJECT = "jarvis_os"


def _analyst(store, io_id: str, session: str) -> dict:
    """The improvement order's analyst — its planner-shaped child (§3.1)."""
    analyst = store.create_work_order("Analyse the first-turn reads",
                                      description="the observation", kind="analyst",
                                      parent_id=io_id, status="running")
    store.update_feature_order(io_id, plan_wo_id=analyst["id"])
    store.set_feature_status(io_id, "planning")
    store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                       (session, analyst["id"]))
    store.conn.commit()
    return analyst


def _report() -> dict:
    """Three findings, one per decision the page has to render."""
    from jarvis.testing import a_finding, a_report

    def orders(title: str, description: str) -> list[dict]:
        return [{"type": "work", "project": PROJECT, "title": title,
                 "description": description}]

    return a_report(findings=[
        a_finding("first-turn-reads", proposed_orders=orders(
            "name the entry point in every dispatch brief",
            "Add the owning module and entry-point symbol to the dispatch brief a "
            "worker is spawned with, read from the committed code map. A worker sees "
            "this brief and nothing else, so the names have to be spelled out in it.")),
        a_finding(
            "stale-code-map",
            symptom=("The committed code map still names three modules that were split "
                     "apart two releases ago, so it misdirects every reader."),
            root_cause=("Nothing regenerates the map when a module moves; it is written "
                        "by hand whenever somebody remembers to."),
            why_insufficient=("Editing the three stale entries fixes today's map and "
                              "leaves the next rename to rot it the same way."),
            recommendation=("Regenerate the map from the symbol index on every release "
                            "and fail the release when it drifts."),
            evidence=[{"source": "git log --stat src/jarvis/",
                       "quote": "src/jarvis/gates.py split into gates/ 2 releases ago"}],
            proposed_orders=orders(
                "regenerate the code map on every release",
                "Rebuild the committed code map from the symbol index as a release "
                "step, and fail the release when the regenerated map differs from the "
                "committed one. The map is read by every worker's first turn.")),
        a_finding(
            "retry-storm",
            symptom=("A rejected pull request is re-pushed within seconds, three times "
                     "in a row, before the first review comment is even read."),
            root_cause=("The repair nudge carries no backoff, so a worker answers a "
                        "rejection with the same turn it just took."),
            why_insufficient=("Capping the retries hides the storm and still spends "
                              "three full turns answering one unread review."),
            recommendation=("Hold the nudge until the review body has been fetched, and "
                            "quote it into the worker's prompt."),
            evidence=[{"source": "jarvis inspect wo-22222222",
                       "quote": "turns 4, 5 and 6 pushed the same commit range"}],
            proposed_orders=[]),
    ])


def seed() -> tuple[str, str]:
    """A parked improvement order with three decided-differently findings, and a second
    one whose bill has spend on both halves."""
    from jarvis import agent_usage, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore
    from jarvis.testing import make_git_project

    home = Path(os.environ["JARVIS_HOME"])
    project = make_git_project(home, PROJECT)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "cold_prefix_floor": 5_000, "notifications": {"sinks": ["log"]}},
        "projects": [{"name": PROJECT, "path": str(project),
                      "description": "the OS itself"}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.close()
    ops.start_os(str(catalog), foreground=True)

    reviewed = ops.create_improvement_order(
        PROJECT, "first turns re-read the same module",
        description=("Three work orders in a row spent their first turn re-reading the "
                     "dispatch path because nothing told them where it lives."),
        refs=["wo-11111111", "#42", "https://example.invalid/x"])
    billed = ops.create_improvement_order(
        PROJECT, "the panel re-reads the whole diff every round",
        description=("Every validation round re-sends the full diff to all four seats, "
                     "including the three that only judge the tests."),
        refs=["wo-22222222", "#57"])

    store = ProjectStore(project)
    try:
        _analyst(store, reviewed["id"], "sess-reviewed")
        _analyst(store, billed["id"], "sess-billed")
        # A feature order beside them: the project block is only readable as "beside the
        # feature-order block" if there is one.
        fo = store.create_feature_order("Ship the reference importer",
                                        "OpenAlex, Crossref and a BibTeX fallback.")
        store.set_feature_status(fo["id"], "executing")
    finally:
        store.close()

    ops.submit_findings(reviewed["id"], _report())
    ops.review_findings(reviewed["id"], accept=("first-turn-reads",),
                        reject={"retry-storm": "the nudge already waits for the review "
                                               "body; these three pushes are one worker "
                                               "retrying a failed push, not a storm"},
                        project_name=PROJECT)

    # Both halves of the bill non-zero: the analyst's own session, and what Jarvis spent
    # on the order itself (the row §6.3 asks the page to name).
    root = Path(os.environ["JARVIS_TRANSCRIPT_ROOT"]) / "-proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / "sess-billed.jsonl").write_text(json.dumps({
        "type": "assistant",
        "message": {"id": "m1", "model": "claude-opus-5",
                    "usage": {"input_tokens": 0, "cache_creation_input_tokens": 900_000,
                              "cache_read_input_tokens": 120_000,
                              "output_tokens": 5_000}},
    }) + "\n")
    for label in ("is this observation worth an analyst?", "does the report hold up?"):
        agent_usage.record("neo_answer", project=PROJECT, wo_id=billed["id"],
                           label=label, model="claude-opus-5",
                           usage={"total_cost_usd": 0.02, "input": 10,
                                  "cache_write": 100_000, "cache_read": 0, "output": 0})
    return reviewed["id"], billed["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(reviewed: str, billed: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})

        page.goto(f"{base}/io/{PROJECT}/{reviewed}")
        page.screenshot(path=SHOTS / "improvement_order_page.png", full_page=True)

        page.goto(f"{base}/project/{PROJECT}")
        # Cropped to the block itself: the whole project page is mostly work orders.
        box = page.locator("h3", has_text="Improvement orders").bounding_box()
        page.screenshot(path=SHOTS / "improvement_order_project_block.png",
                        full_page=True,
                        clip={"x": 0, "y": max(box["y"] - 260, 0), "width": 1280,
                              "height": 560})

        page.goto(f"{base}/cost/{PROJECT}/{billed}")
        # The dashboard has no JavaScript, so every <details> is opened here rather than
        # clicked one at a time.
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "improvement_order_bill.png", full_page=True)
        browser.close()


def main() -> int:
    home = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = home
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(home) / "transcripts")
    # Inside a worker's process tree `JARVIS_SPEND_HOME` names the LIVE os.db, and
    # `agent_usage.record` writes there rather than to the temp home — seeded spend would
    # land on the running fleet's bill and never reach this screenshot.
    os.environ["JARVIS_SPEND_HOME"] = home
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    reviewed, billed = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(reviewed, billed)
    print("\n".join(str(SHOTS / name) for name in (
        "improvement_order_page.png", "improvement_order_project_block.png",
        "improvement_order_bill.png")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
