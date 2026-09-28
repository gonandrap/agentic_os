"""Screenshot the REVIEW control an escalated round owes, for a PR's UI evidence.

`scripts/screenshot_rejudge_control.py` is the shape this copies. Issue #816: a work
order the panel gave up on named the ask and offered no way to answer it, because the
only review form was gated on a pending assumption.

Two shots, because the change has two claims and one shot shows neither on its own: the
control existing at all on an order with NO assumption rows (and the `#pending` deep link
landing on it), and the same order shape with ONE pending assumption, where the control
renders ONCE and in the assumptions block.

The escalated state is built through `ops.finish` + `ops.escalate_validation_round`, never
`store.set_status` (kn-303839ee): a hand-built state leaves no attention flag and is a
picture of a surface nobody has.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_review_control.py
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

PR = "https://github.com/gonandrap/agentic_os/pull/817"
REASON = ("the panel asked for a regression test the worker had already argued is "
          "covered by tests/test_daemon.py, and neither moved — the panel and the "
          "worker cannot settle this between them")


def _repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def _escalate(store, title: str, description: str, summary: str) -> str:
    """A work order in the live shape of wo-659be188: `needs_review`, latest round
    `escalated`. Same shape as `tests/test_ui.py::_escalate`, through the real path."""
    from jarvis import ops

    wo = ops.create_work_order("jarvis_os", title, description)
    ops.finish(wo["id"], summary, pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 3841 passed, 2 skipped")
    rnd = store.latest_validation_round(wo_id=wo["id"])
    store.record_validation_opinion(
        rnd["id"], "tester", verdict="reject",
        reply="The fix is right, but I want a regression test that fails without it.",
        model="claude-opus-5", latency_ms=21300)
    store.record_validation_opinion(
        rnd["id"], "reviewer", verdict="pass",
        reply="Reads correctly and the existing daemon tests cover the path.",
        model="claude-sonnet-4-5", latency_ms=9100)
    ops.escalate_validation_round(store, store.get_work_order(wo["id"]),
                                  rnd["id"], rnd["round"], REASON)
    return wo["id"]


def seed() -> tuple[Path, str, str]:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    _repo(project)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself",
                      "validation": {"enabled": True, "auto_merge": True}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    try:
        bare = _escalate(
            pstore,
            "A parked work order never asks the user to resolve its own merge conflict",
            "The merge-conflict poll should ask the worker, up to three times, before "
            "the order reaches the user.",
            "taught the poll to retry the worker before escalating")
        held = _escalate(
            pstore,
            "The attention list drops a work order whose only blocker is a stale flag",
            "Re-derive attention from the invariants every tick instead of trusting the "
            "stored flag.",
            "attention is now derived, with the stored flag as a cache")
    finally:
        pstore.close()
    ops.assume(held, "Assumed a flag whose blocker no longer holds should be cleared "
                     "silently rather than reported in the inbox.")
    return catalog, bare, held


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(bare: str, held: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # The deep link, not a scroll: the shot proves `#pending` lands on the control.
        page.goto(f"{base}/wo/jarvis_os/{bare}#pending")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / "review-escalated-no-assumptions.png")

        page.goto(f"{base}/wo/jarvis_os/{held}#pending")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / "review-one-control-with-assumption.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, bare, held = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(bare, held)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("review-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
