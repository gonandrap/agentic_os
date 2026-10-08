"""Screenshot the `/cost` WINDOW SELECTOR, for a PR's UI evidence.

`scripts/screenshot_bill_reading.py` is the shape this copies: a temp `JARVIS_HOME`, a
temp catalog, synthetic state written through the real schema, the app served in a
thread. The difference is that every figure here is a question about WHEN, so the state
is placed on a CHOSEN clock — four orders, one per window:

    the current usage week · the previous week · a 5h slice · and one six weeks back
    that must appear in NO shot

so the shots differ in the only way that makes them evidence. Seven shots, because the
claim has seven parts: the default is still the usage week, a past week moves every
number, a 5h slice is reportable at all, a custom range round-trips through the form,
A BAD PARAMETER IS REFUSED rather than silently reported as the week (§3 of
docs/superpowers/specs/2026-10-07-cost-window-selector.md), the DISPLAY ZONE can be
picked and labels the page in it, and a bad zone is refused the same way (§11).

Each shot is CLIPPED from the page heading to the bottom of the headline panel, so the
selector row, both window labels and the totals are legible in one image rather than a
full-page strip. The default/previous-week pair is compared before the script exits: an
identical pair is worthless as evidence, so it fails rather than reporting success. The
Europe/Berlin shot is compared against the default week the other way round — §11's
display-only rule says changing the zone must move NO number, so an unequal headline
total fails.

    uv run python scripts/screenshot_cost_window.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8809
PROJECT = "jarvis_os"

#: The clip: the heading, the selector row and the headline totals, nothing below.
CLIP = """() => {
  const p = [...document.querySelectorAll('p')]
      .find(e => e.textContent.trim().startsWith('~$'));
  if (!p) {
    // The refusal renders `error.html`: there is no headline, so the clip runs from
    // the top of the page to the end of its one message.
    const last = document.querySelectorAll('body *');
    const end = [...last].map(e => e.getBoundingClientRect().bottom);
    return {x: 0, y: 0, width: 1280, height: Math.max(...end) + 14};
  }
  const top = document.querySelector('h2').getBoundingClientRect().top - 14;
  const bottom = p.closest('.panel').getBoundingClientRect().bottom + 14;
  return {x: 0, y: Math.max(0, top), width: 1280, height: bottom - Math.max(0, top)};
}"""

HEADLINE = """() => {
  const p = [...document.querySelectorAll('p')]
      .find(e => e.textContent.trim().startsWith('~$'));
  return p ? p.textContent.trim() : null;
}"""


def os_call(wo_id: str, at: float, *, read: int, out: int) -> None:
    """One `agent_calls` row WITH tokens — Neo answering between a worker's turns.

    Written here rather than through `FleetCostFixture.os_call`, which stamps zero
    tokens: every figure on the page is list-priced from tokens, so a zero-token row
    would put the jarvis half of the headline at $0.00 in every shot and the evidence
    would show only the worker half moving with the window.
    """
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.conn.execute(
            """INSERT INTO agent_calls (ts, project, wo_id, kind, label, model, ok,
                                        cost_usd, input, cache_write, cache_read, output)
               VALUES (?, ?, ?, 'neo_answer', 'question', 'claude-opus-5', 1,
                       0, 400, 0, ?, ?)""", (at, PROJECT, wo_id, read, out))
        central.conn.commit()
    finally:
        central.close()


def stamp(ts: float, zone: str) -> str:
    """The seconds-less shape `<input type="datetime-local">` submits.

    Rendered in the page's DISPLAY zone, because that is the clock the form is now
    parsed in (§11). Stamping UTC here put the custom shot's range hours off its seeded
    orders and the shot read $0.00.
    """
    return datetime.fromtimestamp(ts, tz=ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")


def seed() -> dict[str, str]:
    """Four orders in four windows; returns the query strings the shots use."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.testing import FleetCostFixture, make_git_project

    home = Path(os.environ["JARVIS_HOME"])
    project = make_git_project(home, PROJECT)
    root = home / "transcripts"
    (root / "-proj").mkdir(parents=True)
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(root)

    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"cold_prefix_floor": 5_000},
        "projects": [{"name": PROJECT, "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    try:
        central.upsert_project(PROJECT, str(project), "the OS itself")
        central.set_state("catalog_path", str(catalog))
        central.conn.commit()
    finally:
        central.close()

    fix = FleetCostFixture(home, project, root, catalog, name=PROJECT)

    # The windows themselves come from the resolver under test, so the state is placed
    # by the same arithmetic the page reports with.
    week = ops.cost_window(window="week", offset=0)
    prev = ops.cost_window(window="week", offset=-1)
    sess = ops.cost_window(window="5h", offset=-1)

    inside_week = week["since"] + 1_800
    if sess["since"] <= inside_week < sess["until"]:   # keep the two populations apart
        inside_week = sess["until"] + 60
    custom_since, custom_until = prev["since"], prev["since"] + 2 * 86_400

    plan = [
        ("the dashboard re-reads itself every 15 seconds", inside_week,
         (400_000, 1_000_000, 20_000), 3.50),
        ("a window selector for /cost and jarvis cost --fleet",
         custom_since + 3_600, (1_200_000, 4_000_000, 60_000), 11.25),
        ("teach the gate recogniser that a negative slice ships nothing",
         prev["since"] + 5 * 86_400, (700_000, 2_000_000, 30_000), 6.40),
        ("read the 5h grid off the weekly reset", sess["since"] + 1_200,
         (150_000, 300_000, 8_000), 1.10),
        ("an order six weeks back that must appear in no shot",
         prev["since"] - 5 * 7 * 86_400, (9_000_000, 20_000_000, 200_000), 40.00),
    ]
    for i, (title, at, (write, read, out), cost) in enumerate(plan):
        wo_id = fix.order(title, session_id=f"sess-{i}")
        fix.turn(wo_id, started_at=at, ended_at=at + 600, cost_usd=cost)
        fix.transcript(f"sess-{i}", [
            fix.call_row(at=at, write=write, out=out // 2, mid=f"m-{i}-a"),
            fix.call_row(at=at + 300, read=read, out=out - out // 2, mid=f"m-{i}-b"),
        ])
        os_call(wo_id, at + 120, read=read // 4, out=out // 4)

    # The zone read the way the page reads it, never a constant imported from catalog.
    zone = ops.cost_zone()
    return {
        "wo-cost-window-week": "",
        "wo-cost-window-prev-week": "?window=week&offset=-1",
        "wo-cost-window-5h": "?window=5h&offset=-1",
        "wo-cost-window-custom": (f"?since={stamp(custom_since, zone)}"
                                  f"&until={stamp(custom_until, zone)}"),
        "wo-cost-window-refusal": "?window=5x",
        "wo-cost-window-tz": "?tz=Europe/Berlin",
        "wo-cost-window-tz-refusal": "?tz=Mars/Olympus",
    }


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(shots: dict[str, str]) -> dict[str, str | None]:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str | None] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        for name, query in shots.items():
            page.goto(f"http://127.0.0.1:{PORT}/cost{query}")
            page.wait_for_timeout(200)
            clip = page.evaluate(CLIP)
            seen[name] = page.evaluate(HEADLINE)
            page.screenshot(path=SHOTS / f"{name}.png", clip=clip)
        browser.close()
    return seen


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    shots = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    seen = shoot(shots)

    for name in shots:
        png = SHOTS / f"{name}.png"
        print(f"{png}  {png.stat().st_size} bytes  headline {seen[name]}")
    default, prev = seen["wo-cost-window-week"], seen["wo-cost-window-prev-week"]
    if default is None or default == prev:
        print(f"FAIL: the default and previous-week shots both read {default} — "
              f"an identical pair proves nothing", file=sys.stderr)
        return 1
    for name, query in (("wo-cost-window-refusal", "?window=5x"),
                        ("wo-cost-window-tz-refusal", "?tz=Mars/Olympus")):
        if seen[name] is not None:
            print(f"FAIL: {query} rendered a cost headline instead of refusing",
                  file=sys.stderr)
            return 1
    custom = seen["wo-cost-window-custom"]
    if custom in (None, "~$0.00") or custom == default:
        print(f"FAIL: the custom-range shot reads {custom} — a range that reports "
              f"nothing, or reports the default week, is not evidence of a range",
              file=sys.stderr)
        return 1
    # §11: the display zone is display only, so the total under it may not move.
    berlin = seen["wo-cost-window-tz"]
    if berlin != default:
        print(f"FAIL: ?tz=Europe/Berlin reads {berlin} and the default week reads "
              f"{default} — changing the display zone must move no number",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
