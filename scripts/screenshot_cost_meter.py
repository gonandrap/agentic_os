"""Screenshot the `/cost` USAGE METER section, for a PR's UI evidence.

`scripts/screenshot_cost_window.py` is the shape this copies: a temp `JARVIS_HOME`, a
temp catalog, synthetic state written through the real schema, the app served in a
thread. The difference is WHAT the state has to prove, so every figure is seeded to make
one clause of `usage_meter.sentence` appear:

    a 5h span of readings a minute apart, WITH A RESET INSIDE IT (so the rise is summed
    per segment) · three gap rows (a failed read is an unknown, never 0.0) · three
    earlier clean calibration runs (so $/point reads `measured`, never the shipped seed)
    · worker turns and a Neo call in the span (the Jarvis share) · and ONE SESSION
    OUTSIDE every registered project (the "other sessions on this machine" clause)

Spec docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md §§4-9.

The shot is CLIPPED from the page heading to just past the meter panel, so the sentence,
the 5h/7d sparkline and the first heading of the listing BELOW it are legible in one
image rather than a full-page strip. The script fails rather than reporting success when
the rendered panel is not evidence: no sentence, no `<polyline>` with at least two
points, a sentence missing the reset or outside-session clause, or a $/point labelled
`seed`.

    uv run python scripts/screenshot_cost_meter.py
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
SHOT = "cost_meter"
PORT = 8811
PROJECT = "jarvis_os"
MINUTE = 60.0
HOUR = 3600.0

#: The clip: the heading down to a little past the meter panel, so what the meter sits
#: ABOVE is visible too.
CLIP = """() => {
  const panel = [...document.querySelectorAll('.panel')]
      .find(e => e.textContent.includes("The account's usage meter"));
  if (!panel) return null;
  const top = document.querySelector('h2').getBoundingClientRect().top - 14;
  const bottom = panel.getBoundingClientRect().bottom + 150;
  return {x: 0, y: Math.max(0, top), width: 1280, height: bottom - Math.max(0, top)};
}"""

#: What the panel actually says, read back out of the DOM rather than off the payload.
READ = """() => {
  const panel = [...document.querySelectorAll('.panel')]
      .find(e => e.textContent.includes("The account's usage meter"));
  if (!panel) return null;
  const line = [...panel.querySelectorAll('p')]
      .find(e => e.textContent.includes('the 5h meter rose'));
  const svg = panel.querySelector('svg polyline');
  return {
    sentence: line ? line.textContent.trim() : null,
    kv: [...panel.querySelectorAll('p.kv')].map(e => e.textContent.trim()),
    points: svg ? (svg.getAttribute('points') || '').trim().split(/\\s+/).length : 0,
  };
}"""


def samples(rows) -> None:
    """Write `usage_samples` through the real writer — `usage_meter.record`."""
    from jarvis import usage_meter
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        for row in rows:
            usage_meter.record(central, row)
        central.conn.commit()
    finally:
        central.close()


def run(start: float, *, n: int, first: float, last: float, resets: float,
        seven_resets: float, seven_first: float = 20.0, seven_last: float = 22.0,
        gaps: set[int] = frozenset()):
    """`n` readings a minute apart, utilisation rising first -> last.

    A member of `gaps` is a GAP ROW: `ok=0` with NULL percentages, which is what a failed
    read is written as. Never 0.0 — 0.0 is a real reading that means the window just
    reset (§4.2).
    """
    from jarvis.usage_meter import Sample

    step = (last - first) / (n - 1) if n > 1 else 0.0
    seven_step = (seven_last - seven_first) / (n - 1) if n > 1 else 0.0
    found = []
    for i in range(n):
        if i in gaps:
            found.append(Sample(ts=start + i * MINUTE, ok=False, reason="http 503",
                                http_status=503))
            continue
        found.append(Sample(ts=start + i * MINUTE, ok=True,
                            five_hour_pct=first + i * step,
                            five_hour_resets_at=resets,
                            seven_day_pct=seven_first + i * seven_step,
                            seven_day_resets_at=seven_resets))
    return found


def seed() -> None:
    """The scene, placed on the real clock so `?window=5h` re-anchors onto it."""
    from jarvis import db
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
    now = db.now()
    # `meter_window` anchors the span on the LATEST sample's `five_hour_resets_at`, so
    # the boundary chosen here IS the end of the span the page reports.
    reset_at = now
    since = reset_at - 5 * HOUR
    reset_mid = since + 180 * MINUTE

    # $/point MEASURED, not the shipped seed: three clean 40-minute runs of 10 points at
    # $8.20 each. Separated by more than `MAX_SPAN_GAP_SECONDS`, so each is its own run.
    for offset in (-30 * HOUR, -28 * HOUR, -26 * HOUR):
        at = since + offset
        samples(run(at, n=40, first=0.0, last=10.0, resets=at + 3 * HOUR,
                    seven_resets=at + 4 * 86_400))
        wo = fix.order("a calibration order")
        fix.turn(wo, started_at=at + MINUTE, ended_at=at + 2 * MINUTE, cost_usd=8.20)

    # Segment 1: 10.0 -> 42.0 against the OLD boundary, with three failed reads in it.
    # The 7d boundary is the SAME in both halves: only the 5h window resets here, which
    # is why the 7d line climbs straight through the 5h line's fall.
    seven_resets = reset_at + 4 * 86_400
    samples(run(since, n=180, first=10.0, last=42.0, resets=reset_mid,
                seven_resets=seven_resets, seven_first=20.0, seven_last=26.6,
                gaps={5, 6, 7}))
    # Segment 2: the window has reset, so utilisation starts from the floor again. Every
    # later segment starts at 0.0 BY DEFINITION (§4.4).
    samples(run(reset_mid, n=120, first=0.0, last=18.0, resets=reset_at,
                seven_resets=seven_resets, seven_first=26.6, seven_last=31.0))

    # Jarvis's own half: two orders' turns plus Neo answering between them (§7).
    # Each order gets a TRANSCRIPT as well as a turn: the headline above the meter is
    # token-sourced, so an order with only a turn row would put a ~$0.00 headline over a
    # meter sentence saying Jarvis spent $36.90.
    one = fix.order("sample the account's usage meter every minute", session_id="sess-1")
    fix.turn(one, started_at=since + 20 * MINUTE, ended_at=since + 95 * MINUTE,
             cost_usd=21.50)
    fix.transcript("sess-1", [
        fix.call_row(at=since + 25 * MINUTE, write=2_500_000, out=50_000, mid="m-1-a"),
        fix.call_row(at=since + 80 * MINUTE, read=5_000_000, out=50_000, mid="m-1-b"),
    ])
    fix.os_call("neo_answer", ts=since + 60 * MINUTE, wo_id=one, cost_usd=1.80,
                label="does --window 5h re-anchor on the sampled resets_at?")
    two = fix.order("reconcile the meter delta against what the records say",
                    session_id="sess-2")
    fix.turn(two, started_at=reset_mid + 15 * MINUTE, ended_at=reset_mid + 80 * MINUTE,
             cost_usd=12.70)
    fix.transcript("sess-2", [
        fix.call_row(at=reset_mid + 20 * MINUTE, write=1_500_000, out=30_000,
                     mid="m-2-a"),
        fix.call_row(at=reset_mid + 70 * MINUTE, read=3_000_000, out=30_000,
                     mid="m-2-b"),
    ])
    fix.os_call("validation_seat", ts=reset_mid + 40 * MINUTE, wo_id=two, cost_usd=0.90,
                label="panel seat")

    # THE OUTSIDE SESSION: a directory that is no registered project's slug, so no
    # ownership record can claim it. $2.00 of cache write + $0.48 of output at Opus list.
    fix.transcript("1a9cd19d-0000-4000-8000-000000000001", [
        {"type": "user",
         "timestamp": _iso(since + 150 * MINUTE),
         "message": {"content": "walk me through the invoice for last month"}},
        fix.call_row(at=since + 151 * MINUTE, write=320_000, out=19_200, mid="m-out"),
    ], directory="-home-someone-elsewhere")


def _iso(at: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> dict | None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1600})
        page.goto(f"http://127.0.0.1:{PORT}/cost?window=5h")
        page.wait_for_timeout(300)
        clip = page.evaluate(CLIP)
        seen = page.evaluate(READ)
        if clip is None:
            page.screenshot(path=SHOTS / f"{SHOT}.png", full_page=True)
        else:
            page.screenshot(path=SHOTS / f"{SHOT}.png", clip=clip)
        browser.close()
    return seen


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    seen = shoot()

    png = SHOTS / f"{SHOT}.png"
    print(f"{png}  {png.stat().st_size} bytes")
    if seen is None:
        print("FAIL: the page rendered no meter panel at all", file=sys.stderr)
        return 1
    print(f"sentence: {seen['sentence']}")
    for line in seen["kv"]:
        print(f"kv: {line}")
    said = seen["sentence"] or ""
    for clause, why in (
            ("the 5h meter rose", "no meter delta: the sentence was not built"),
            ("other sessions on this machine", "the outside session was not priced"),
            ("unexplained", "the residual is not labelled"),
            ("summed per segment", "no reset was detected inside the span"),
    ):
        if clause not in said:
            print(f"FAIL: {why} — {clause!r} is not in the sentence", file=sys.stderr)
            return 1
    if "not measured here" in said:
        print("FAIL: $/point fell back to the shipped seed — the calibration runs were "
              "not read", file=sys.stderr)
        return 1
    if seen["points"] < 2:
        print(f"FAIL: the sparkline has {seen['points']} point(s) — a polyline of one "
              f"point draws nothing", file=sys.stderr)
        return 1
    if png.stat().st_size < 10_000:
        print(f"FAIL: {png} is {png.stat().st_size} bytes — the page rendered empty",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
