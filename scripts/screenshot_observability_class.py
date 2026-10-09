"""Screenshot the bill's fourth actor class — what LOOKING at an order cost (§10 of
docs/superpowers/specs/2026-09-24-order-observability.md).

Two shots, because the class has two renderings and a reviewer has to see both:

  1. `bill-observability-class.png` — the by-actor view of an order somebody looked at:
     the observability line at `0.00` beside a worker line that is not zero. The zero is
     the claim, so it has to be visible NEXT TO real money or it reads as a bug.
  2. `bill-observability-absent.png` — the "What is not on this bill" panel of an order
     nobody looked at: the sentence says "not recorded" and never `0.00`, because absent
     is not zero (Neo's ruling on question 766).

Everything lives in a temp `JARVIS_HOME`, so it never touches the live OS:

    uv run python scripts/screenshot_observability_class.py
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
LOOKED_AT = "bill-observability-class.png"
NOBODY_LOOKED = "bill-observability-absent.png"


def envelope(cost: float, *, read: int, write: int, out: int) -> str:
    return json.dumps({
        "usage_v": 3, "total_cost_usd": cost, "input": 2, "cache_write": write,
        "cache_read": read, "cache_1h": 0, "cache_5m": write, "output": out,
        "context_peak": read + write, "context_window": 1_000_000,
        "by_model": [{"model": "claude-opus-5", "input": 2, "cache_write": write,
                      "cache_read": read, "output": out, "cost_usd": cost}],
    })


def seed() -> tuple[str, str, str]:
    from jarvis import agent_usage, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"cold_prefix_floor": 5_000},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.set_state("catalog_path", str(catalog))
    central.conn.commit()
    central.close()

    looked = ops.create_work_order("jarvis_os", "an order somebody watched")
    unseen = ops.create_work_order("jarvis_os", "an order nobody has looked at")
    store = ProjectStore(project)
    try:
        # A real worker line on both orders: the point of shot 1 is the zero BESIDE
        # money, and an empty bill would show neither the class nor the contrast.
        for wo_id, turns in ((looked["id"], ((4.11, (612_000, 41_000, 6_300)),
                                             (1.86, (240_000, 12_500, 2_100)))),
                             (unseen["id"], ((2.40, (310_000, 18_000, 3_400)),))):
            for cost, tokens in turns:
                turn = store.create_turn(wo_id, kind="message", prompt="p")
                store.finish_turn(turn["id"], "done", result="r", cost_usd=cost,
                                  num_turns=4,
                                  usage_json=envelope(cost, read=tokens[0],
                                                      write=tokens[1], out=tokens[2]))
    finally:
        store.close()

    # The envelope the meter writes for a mechanical path: a wall clock, and zeros.
    for kind in ("observe_live", "observe_why", "observe_inspect", "observe_live"):
        agent_usage.record(kind, project="jarvis_os", wo_id=looked["id"],
                           usage={"wall_ms": 340, "total_cost_usd": 0.0, "input": 0,
                                  "cache_write": 0, "cache_read": 0, "output": 0})
    return "jarvis_os", looked["id"], unseen["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


#: Clip to the heading plus the panel under it. A full-page shot of a bill puts the point
#: of either image off screen, and a reviewer should not have to hunt for it.
SECTION_CLIP = """
(heading) => {
  const h = [...document.querySelectorAll('h2')]
    .find(el => el.textContent.trim().startsWith(heading));
  if (!h) return null;
  const top = h.getBoundingClientRect();
  let last = top;
  for (let el = h.nextElementSibling; el && el.tagName !== 'H2';
       el = el.nextElementSibling) last = el.getBoundingClientRect();
  return {x: Math.max(top.left - 12, 0), y: Math.max(top.top + window.scrollY - 12, 0),
          width: Math.min(top.right - top.left + 24, 1240),
          height: last.bottom + window.scrollY - top.top - window.scrollY + 24};
}
"""


def shoot(project: str, looked: str, unseen: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        for wo_id, heading, name in ((looked, "The bill", LOOKED_AT),
                                     (unseen, "What is not on this bill",
                                      NOBODY_LOOKED)):
            page.goto(f"http://127.0.0.1:{PORT}/cost/{project}/{wo_id}")
            page.wait_for_timeout(200)
            clip = page.evaluate(SECTION_CLIP, heading)
            if clip is None:
                raise SystemExit(f"no '{heading}' section on the bill for {wo_id}")
            # `full_page` with a clip: the section can sit below the viewport, and a clip
            # against the viewport-sized image is refused as outside it.
            page.screenshot(path=SHOTS / name, clip=clip, full_page=True)
        browser.close()


def main() -> int:
    home = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = home
    # `agent_usage.record` writes to $JARVIS_SPEND_HOME when it is set, and it IS set in
    # any process a work order spawned — without this the seeded rows land in the live
    # accounting DB instead of the temp one.
    os.environ["JARVIS_SPEND_HOME"] = home
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, looked, unseen = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(project, looked, unseen)
    for name in (LOOKED_AT, NOBODY_LOOKED):
        print(SHOTS / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
