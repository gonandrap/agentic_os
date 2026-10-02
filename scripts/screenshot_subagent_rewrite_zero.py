"""Screenshot the bill's re-write tax with the subagent side's STRUCTURAL zero named —
the PR's UI evidence for spec 2026-10-02 §1.5.

The unlabelled rollup read as "subagent cache spend is negligible". It is not negligible;
it was unattributable from that number. So the order here has a main side that pays a real
tax (three big writes against a smaller peak, one cache read going backwards) and a
subagent side that CANNOT pay one: monotonic reads far above what it wrote, so its
`rewrite_excess` and its `resume_boundaries` are 0 by arithmetic while its `cache_write`
is 10,000.

Everything lives in a temp `JARVIS_HOME` with its own transcript root, so it never touches
the live OS:

    uv run python scripts/screenshot_subagent_rewrite_zero.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
OUT = SHOTS / "subagent-rewrite-zero.png"
PORT = 8802
SLUG = "-proj"
SESSION = "sess-rewrite-zero"

NOW = time.time()
TURN = (NOW - 1800, NOW - 900)

#: Asserted on the rendered page before anything is captured —
#: `bill.SUBAGENT_REWRITE_ZERO` as the Jinja global `subagent_rewrite_zero` renders it,
#: with this fixture's figures.
#: A template that dropped the global would fail here rather than ship a picture of the
#: paragraph it was meant to prove.
#: The heading is checked apart from them: `h2` is rendered through a CSS
#: `text-transform`, so what the reader sees is upper case and the source string would
#: never match `inner_text`.
EXPECTED_HEADING = "The re-write tax"
EXPECTED = (
    "Not an extra charge — a part of the cache-write line, named.",
    "of that, subagents contributed 0 tokens across 0 boundaries — a STRUCTURAL zero "
    "and not a small one",
    "Those 1 subagent(s) still wrote 10,000 cache-creation tokens, itemised under the "
    "turn each ran in.",
)


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def assistant_row(at: float, mid: str, *, write: int = 0, read: int = 0,
                  out: int = 10) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 0,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read,
                                  "cache_creation": {"ephemeral_5m_input_tokens": write,
                                                     "ephemeral_1h_input_tokens": 0},
                                  "output_tokens": out}}}


def main_rows() -> list[dict]:
    """Three writes of 60,000 against a peak smaller than their sum, and one cache read
    going backwards — one boundary, and a real tax on the main side."""
    return [assistant_row(TURN[0] + 10, "m1", write=60_000, read=50_000),
            assistant_row(TURN[0] + 400, "m2", write=60_000, read=1_000),
            assistant_row(TURN[0] + 800, "m3", write=60_000, read=2_000)]


def subagent_rows() -> list[dict]:
    """Every real subagent: it writes a little and reads a lot, monotonically."""
    return [assistant_row(TURN[0] + 100, "s1", write=5_000, read=100_000, out=5),
            assistant_row(TURN[0] + 160, "s2", write=5_000, read=120_000, out=5)]


def envelope(cost: float) -> str:
    return json.dumps({
        # The same tokens the main transcript holds, and a CONTEXT PEAK smaller than
        # their sum: the peak is what the excess is measured against, so a peak above
        # the written total is a session with no tax to show.
        "usage_v": 3, "total_cost_usd": cost, "input": 0, "cache_write": 180_000,
        "cache_read": 53_000, "cache_1h": 0, "cache_5m": 180_000, "output": 30,
        "context_peak": 110_000, "context_window": 1_000_000, "api_calls": 3,
        "by_model": [{"model": "claude-opus-5", "input": 0, "cache_write": 180_000,
                      "cache_read": 53_000, "output": 30, "cost_usd": cost}],
    })


def seed() -> tuple[str, str]:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    # `cold_prefix_floor` is not decoration: a bill classifies its cold boundaries
    # against it and raises rather than guessing a threshold.
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

    wo = ops.create_work_order(
        "jarvis_os", "an order whose subagent side cannot pay a re-write tax")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], session_id=SESSION, status="running")
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="p")
        store.finish_turn(turn["id"], "done", result="ok", cost_usd=3.4, num_turns=6,
                          usage_json=envelope(3.4))
        store.conn.execute("UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                           (TURN[0], TURN[1], turn["id"]))
        store.conn.commit()
    finally:
        store.close()

    root = Path(os.environ["JARVIS_TRANSCRIPT_ROOT"])
    (root / SLUG).mkdir(parents=True, exist_ok=True)
    (root / SLUG / f"{SESSION}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in main_rows()))
    subs = root / SLUG / SESSION / "subagents"
    subs.mkdir(parents=True, exist_ok=True)
    (subs / "agent-0.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in subagent_rows()))
    (subs / "agent-0.meta.json").write_text(
        json.dumps({"agentType": "explorer", "description": "read the spec"}))
    return "jarvis_os", wo["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def wait_for_server(timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise SystemExit(f"the dashboard never answered on port {PORT} within {timeout:.0f}s "
                     f"— nothing was captured, so no screenshot is stale")


def shoot(project: str, wo_id: str) -> Path:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1200},
                                device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{PORT}/cost/{project}/{wo_id}")
        heading = page.locator("h2", has_text="The re-write tax")
        heading.wait_for()
        text = " ".join(page.inner_text("body").split())
        if EXPECTED_HEADING.lower() not in " ".join(heading.inner_text().split()).lower():
            raise SystemExit(f"not on the page: {EXPECTED_HEADING!r} — nothing was "
                             f"captured")
        for expected in EXPECTED:
            if expected not in text:
                raise SystemExit(f"not on the page: {expected!r} — nothing was "
                                 f"captured. The page read:\n{text[:3000]}")
        # CLIPPED to the heading and the panel under it, at the rendered size: a
        # full-page shot of a bill puts this paragraph in unreadable type.
        panel = page.locator("div.panel", has_text="Not an extra charge").first
        top, box = heading.bounding_box(), panel.bounding_box()
        # `full_page` with the clip: the block sits well below the fold, and a clip
        # outside the viewport is refused on a viewport-sized image.
        page.screenshot(path=OUT, full_page=True, clip={"x": max(0.0, top["x"] - 16),
                                        "y": max(0.0, top["y"] - 16),
                                        "width": min(1100.0, box["width"] + 32),
                                        "height": (box["y"] + box["height"]
                                                   - top["y"] + 32)})
        browser.close()
    return OUT


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = tmp
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(tmp) / "claude-projects")
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, wo_id = seed()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"port {PORT} is already answering — another copy of this "
                             f"script is running; nothing was captured")
    threading.Thread(target=serve, daemon=True).start()
    wait_for_server()
    path = shoot(project, wo_id)
    for expected in EXPECTED:
        print(f"on the page: {expected}")
    print(f"{path}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
