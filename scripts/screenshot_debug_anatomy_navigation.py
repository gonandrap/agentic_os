"""Screenshot the debug page's navigation reading — the PR's UI evidence for spec §3.

`_debug_anatomy.html` renders `u.nav_line` beside the `Tool profile` heading, and
`inspection.nav_line` OMITS the `unclassified` clause when the count is zero: a reading
that could not be taken must never print as a measured `0` (issue #227). A server-side
test that greps for the string cannot show that the pair reads as a pair, so both states
are captured side by side, from two REAL order pages whose readings differ in nothing
else:

  * WITH an unclassified count — `navigation — 2 symbol, 3 source nav, 1 unclassified`,
    the three separate numbers all visible
  * WITHOUT one — `navigation — 2 symbol, 3 source nav`, the clause ABSENT rather than
    printed as zero

Same 2 and same 3 on both sides deliberately: the only difference in the sentence is the
clause, which is the claim. Everything lives in a temp `JARVIS_HOME` with its own
transcript root, so it never touches the live OS:

    uv run python scripts/screenshot_debug_anatomy_navigation.py
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
OUT = SHOTS / "debug-anatomy-navigation.png"
PORT = 8803
SLUG = "-proj"

NOW = time.time()
TURN = (NOW - 1800, NOW - 900)

#: The two states, each as (session id, caption, the sentence asserted on its page).
WITH_UNCLASSIFIED = "sess-nav-three-counts"
WITHOUT_UNCLASSIFIED = "sess-nav-clause-omitted"
EXPECTED = {
    WITH_UNCLASSIFIED: "navigation — 2 symbol, 3 source nav, 1 unclassified",
    WITHOUT_UNCLASSIFIED: "navigation — 2 symbol, 3 source nav",
}
CAPTIONS = {
    WITH_UNCLASSIFIED: "a reading WITH an unclassified count — three separate numbers",
    WITHOUT_UNCLASSIFIED: "the same reading with NONE — the clause is absent, "
                          "not printed as 0",
}


# -- the transcripts --------------------------------------------------------------------


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str) -> dict:
    return {"type": "user", "timestamp": stamp(at), "promptSource": "sdk",
            "message": {"content": text}}


def assistant_row(at: float, mid: str) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 12,
                                  "cache_creation_input_tokens": 40_000,
                                  "cache_read_input_tokens": 80_000,
                                  "cache_creation": {"ephemeral_5m_input_tokens": 40_000,
                                                     "ephemeral_1h_input_tokens": 0},
                                  "output_tokens": 120},
                        "content": [{"type": "text", "text": "ok"}]}}


def tool_rows(start: float, end: float, tool_id: str, name: str,
              payload: dict) -> list[dict]:
    return [{"type": "assistant", "timestamp": stamp(start),
             "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                         "usage": {"input_tokens": 0,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 90_000,
                                   "output_tokens": 8},
                         "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                      "input": payload}]}},
            {"type": "user", "timestamp": stamp(end),
             "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}}]


def classified_rows(base: float) -> list[dict]:
    """2 symbol calls and 3 source-navigation `Bash` calls, every one classifiable.

    The symbol names carry the `mcp__…__` prefix `navigation.is_symbol_call` strips, and
    each `Bash` command names a `.py` path — which is what `navigates_source` reads.
    """
    return [
        *tool_rows(base, base + 3, "t1", "mcp__plugin_serena_serena__find_symbol",
                   {"name_path_pattern": "Anatomy/nav_profile"}),
        *tool_rows(base + 4, base + 9, "t2",
                   "mcp__plugin_serena_serena__find_referencing_symbols",
                   {"name_path": "nav_line"}),
        *tool_rows(base + 10, base + 12, "t3", "Bash",
                   {"command": "grep -rn nav_line src/jarvis/inspection.py"}),
        *tool_rows(base + 13, base + 15, "t4", "Bash",
                   {"command": "cat src/jarvis/navigation.py"}),
        *tool_rows(base + 16, base + 19, "t5", "Bash",
                   {"command": "head -60 src/jarvis/ops.py"}),
    ]


def session_rows(base: float, *, unclassified: bool) -> list[dict]:
    """One turn of navigation, with or without a `Bash` span that carries no command.

    A `Bash` call recorded with only a `description` is UNCLASSIFIED rather than False:
    the command text was never available, so nothing can be said about it (issue #227).
    """
    rows = [prompt_row(base, "You are the worker agent for this work order. "
                             "Find every caller of inspection.nav_line."),
            assistant_row(base + 1, "m1"),
            *classified_rows(base + 2)]
    if unclassified:
        rows += tool_rows(base + 24, base + 26, "t6", "Bash",
                          {"description": "run the targeted suite"})
    rows.append(assistant_row(base + 30, "m2"))
    return rows


# -- the records ------------------------------------------------------------------------


def seed() -> tuple[str, list[tuple[str, str]]]:
    """One project, two completed orders — one per state. Returns (project, [(sid, wo)])."""
    from jarvis import ops
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

    root = Path(os.environ["JARVIS_TRANSCRIPT_ROOT"])
    (root / SLUG).mkdir(parents=True, exist_ok=True)

    made: list[tuple[str, str]] = []
    for session, title, unclassified in (
            (WITH_UNCLASSIFIED,
             "a turn whose navigation reading has an unclassified Bash call", True),
            (WITHOUT_UNCLASSIFIED,
             "the same turn with every Bash call classified", False)):
        wo = ops.create_work_order("jarvis_os", title)
        store = ProjectStore(project)
        try:
            store.update_work_order(wo["id"], session_id=session, status="completed")
            turn = store.create_turn(wo["id"], kind="dispatch", prompt="p")
            store.finish_turn(turn["id"], "done", result="ok", cost_usd=1.4, num_turns=6)
            # The window is rewritten directly: `create_turn` stamps now, and the anatomy
            # joins calls to turns BY these timestamps, so they must match the rows.
            store.conn.execute(
                "UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
                (TURN[0], TURN[1], turn["id"]))
            store.conn.commit()
        finally:
            store.close()
        (root / SLUG / f"{session}.jsonl").write_text(
            "".join(json.dumps(r) + "\n"
                    for r in session_rows(TURN[0] + 5, unclassified=unclassified)))
        made.append((session, wo["id"]))
    return "jarvis_os", made


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def wait_for_server(timeout: float = 30.0) -> None:
    """Block until the dashboard answers, and RAISE if it never does — a fixed sleep is
    a race that leaves the PNG stale from the previous run."""
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


# -- the shot ---------------------------------------------------------------------------


# Two real pages side by side in one shot, iframes rather than two files because the PAIR
# is the claim: the clause present, and the clause gone (`screenshot_feature_spec.PAIR`).
PAIR = """
<html><body style="margin:0;font:13px system-ui;background:#0e1420;color:#c9d4e8">
<div style="display:flex;gap:10px;padding:10px">
  <div style="flex:1">
    <div style="padding:6px 2px">{left_caption}</div>
    <iframe src="{left}" style="width:100%;height:310px;border:1px solid #253049"></iframe>
  </div>
  <div style="flex:1">
    <div style="padding:6px 2px">{right_caption}</div>
    <iframe src="{right}" style="width:100%;height:310px;border:1px solid #253049"></iframe>
  </div>
</div>
</body></html>
"""

#: Puts the `Tool profile` heading at the top of its frame: the nav line renders directly
#: beneath it, and a long page left unscrolled lands the sentence under the fold.
SCROLL_TO_TOOLS = """
() => {
  const h = [...document.querySelectorAll('h2')]
    .find(n => n.textContent.trim() === 'Tool profile');
  if (!h) { return false; }
  window.scrollTo(0, h.getBoundingClientRect().top + window.scrollY - 10);
  return true;
}
"""


def assert_on_page(page, project: str, wo_id: str, session: str) -> None:
    """The sentence asserted on the REAL page before anything is captured.

    A template that stopped rendering `nav_line`, or started printing `0 unclassified`,
    must fail here rather than ship a blank picture of a reading nobody can read.
    """
    page.goto(f"http://127.0.0.1:{PORT}/wo/{project}/{wo_id}/debug")
    page.wait_for_selector("#block-anatomy")
    html = page.content()
    expected = EXPECTED[session]
    if expected not in html:
        raise SystemExit(f"not on the page for {session}: {expected!r} — nothing was "
                         f"captured")
    # Scoped to the rendered panel and never to the whole page: the order's own title and
    # session id carry the word too, and the claim is about the SENTENCE.
    panel = " ".join(page.locator("#block-anatomy .panel")
                     .filter(has_text="navigation —").inner_text().split())
    if panel != expected:
        raise SystemExit(f"the panel for {session} reads {panel!r}, not {expected!r} "
                         f"— nothing was captured")


def shoot(project: str, made: list[tuple[str, str]]) -> Path:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    (left_session, left_id), (right_session, right_id) = made
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1500, "height": 395},
                                device_scale_factor=2)
        for session, wo_id in made:
            assert_on_page(page, project, wo_id, session)

        page.set_content(PAIR.format(
            left=f"{base}/wo/{project}/{left_id}/debug",
            right=f"{base}/wo/{project}/{right_id}/debug",
            left_caption=CAPTIONS[left_session],
            right_caption=CAPTIONS[right_session]))
        page.wait_for_timeout(1200)
        for frame in page.frames[1:]:
            if not frame.evaluate(SCROLL_TO_TOOLS):
                raise SystemExit("no `Tool profile` heading in the framed page — the nav "
                                 "line renders inside that block; nothing was captured")
        page.wait_for_timeout(300)
        page.screenshot(path=OUT)
        browser.close()
    return OUT


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = tmp
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(tmp) / "claude-projects")
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, made = seed()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"port {PORT} is already answering — another copy of this "
                             f"script is running; nothing was captured")
    threading.Thread(target=serve, daemon=True).start()
    wait_for_server()
    path = shoot(project, made)
    for session, _wo_id in made:
        print(f"on the page: {EXPECTED[session]}  ({session})")
    print("on the page: each panel reads EXACTLY the sentence above, so the omitted "
          "clause is absent rather than a `0`")
    print(f"{path}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
