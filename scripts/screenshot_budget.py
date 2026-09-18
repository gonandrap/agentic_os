"""Screenshot the per-order token budget, for a PR's UI evidence.

`scripts/screenshot_forced_round.py` is the shape this copies. Three shots, because the
feature makes three separate claims about rendered surfaces:

  budget-work-order.png   an order running under a ceiling: what it has spent, what is
                          left, and the box that raises it
  budget-exhausted.png    the same order once the money is gone: the new status word and
                          the attention line that says how to un-stick it
  budget-feature.png      a FAMILY budget: the rollup, and the part no live child has
                          already claimed — the number a reader needs to decide whether
                          to top up the child or the feature

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME` and
a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_budget.py
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
PORT = 8803


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def _bill(store, wo_id: str, usd: float) -> None:
    """Spend money the way a worker does: a finished turn carrying a cost.

    The turn is the real row the enforcement reads — `budget.spent` sums `wo_turns`
    — so a change that stopped recording it shows up in the picture.
    """
    turn = store.create_turn(wo_id, kind="message", prompt="work")
    store.finish_turn(turn["id"], "done", result="done", cost_usd=usd, num_turns=1)


def orders(store) -> tuple[str, str, str]:
    """One capped order still running, one that ran out, and a funded feature.

    The exhausted one is parked by `budget.escalate` ITSELF rather than by setting the
    status here: the status word, the attention line and the timeline event in the shot
    are then whatever the real escalation produces.
    """
    from jarvis import budget, ops
    from jarvis.central_store import CentralStore

    healthy = ops.create_work_order(
        "jarvis_os", "Teach the gate recogniser about negative slices",
        "`log[-20:]` ships nothing and should not gate.", budget_usd=5.0)
    _bill(store, healthy["id"], 1.85)
    store.set_status(healthy["id"], "running")

    broke = ops.create_work_order(
        "jarvis_os", "Trace every cache write back to its cause",
        "Label cold-start, ttl-expiry and prefix-miss apart.", budget_usd=3.0)
    _bill(store, broke["id"], 3.12)
    store.set_status(broke["id"], "running")
    central = CentralStore()
    spent = budget.exhaustion(store, central, store.get_work_order(broke["id"]))
    assert spent is not None, "the fixture did not actually run out of money"
    budget.escalate(store, store.get_work_order(broke["id"]), spent)
    central.close()

    feature = ops.create_feature_order(
        "jarvis_os", "A budget per order",
        description="Cap what one order may spend, and stop it at the cap.",
        budget_usd=50.0)
    # A family that has spent some and PROMISED some: one settled child, and one still
    # running with a live reservation. Without the live reservation `unreserved` and the
    # plain remainder would be the same number, and the shot would not show the one thing
    # a family budget adds over a work order's ceiling. The children are written straight
    # onto the store rather than planned, because a real planner is five model calls and
    # the picture is of the arithmetic, not of the plan.
    done = store.create_work_order("Read the CSV", parent_id=feature["id"],
                                   status="completed")
    live = store.create_work_order("Write the CSV", parent_id=feature["id"],
                                   status="running")
    _bill(store, done["id"], 4.40)
    _bill(store, live["id"], 2.10)
    central = CentralStore()
    budget.reserve(store, central, store.get_work_order(live["id"]))
    central.close()
    return healthy["id"], broke["id"], feature["id"]


def seed() -> tuple[Path, str, str, str]:
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
                      "description": "the OS itself"}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    ids = orders(pstore)
    pstore.close()
    return (catalog, *ids)


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(healthy: str, broke: str, feature: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for wo_id, name in ((healthy, "budget-work-order"), (broke, "budget-exhausted")):
            page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
            page.get_by_text("Budget:").first.scroll_into_view_if_needed()
            page.wait_for_timeout(200)
            page.screenshot(path=SHOTS / f"{name}.png")
        page.goto(f"http://127.0.0.1:{PORT}/fo/jarvis_os/{feature}")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "budget-feature.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, *ids = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(*ids)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("budget-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
