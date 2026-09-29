"""Screenshot the "Time in state" panel telling the live truth under an open span.

Fix 4 of docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md: an open span
used to say only "still in it", naming the transition that opened it, while a subagent's
transcript kept growing underneath. `scripts/screenshot_held_gate.py` is the shape this
copies. Seed: a work order enters `running` on a `gate_abandoned` trigger 45 minutes ago,
every DB table (`wo_events`, `wo_turns`, `wo_messages`, `validation_rounds`, `approvals`)
is backdated to or before that moment, and a session transcript plus one
`subagents/*.jsonl` file sit on disk with mtime ~30 seconds old — the incident's own
shape, where the LEAD's transcript is quiet and its implementer's is not.

    uv run python scripts/screenshot_state_active_span.py
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
PORT = 8804

FORTY_FIVE_MIN = 45 * 60.0
THIRTY_SEC = 30.0


def _transcript(config_dir: Path, cwd: Path, session_id: str, *, subagent: str = "",
                mtime: float) -> Path:
    """Same shape as `tests/test_time_in_state.py::_transcript` — mtime is the fact."""
    munged = "".join(c if c.isalnum() else "-" for c in str(cwd))
    base = config_dir / "projects" / munged
    path = (base / f"{session_id}.jsonl" if not subagent
            else base / session_id / "subagents" / subagent)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "assistant"}\n')
    os.utime(path, (mtime, mtime))
    return path


def seed(config_dir: Path) -> str:
    """An order stuck `gate_abandoned` 45 minutes ago while a subagent still types."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
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
        "jarvis_os", "Land the heredoc-write fix",
        description="Fix the four defects behind issue 849's incident.")
    _, path, _ = ops.find_work_order(wo["id"], "jarvis_os")
    pstore = ProjectStore(path)

    session_id = "sess-active-span"
    pstore.update_work_order(wo["id"], session_id=session_id)
    now = time.time()
    entered = now - FORTY_FIVE_MIN

    # The transition an abandoned gate leaves behind — one span, one trigger.
    pstore.set_status(wo["id"], "running", trigger="gate_abandoned")

    # Every table backdated to or before `entered`: the DB genuinely went quiet there,
    # so anything newer must come from the transcript source, not from a DB race. Both
    # spans move — the creation span too, or its unmoved "now" timestamp would sort
    # AFTER the backdated running span and flip which one reads as open.
    pstore.conn.execute(
        "UPDATE wo_state_spans SET ts=? WHERE order_id=? AND to_status<>'running'",
        (entered - 600, wo["id"]))
    pstore.conn.execute(
        "UPDATE wo_state_spans SET ts=? WHERE order_id=? AND to_status='running'",
        (entered, wo["id"]))
    pstore.conn.execute("UPDATE wo_events SET ts=? WHERE wo_id=?", (entered, wo["id"]))
    pstore.conn.execute("UPDATE work_orders SET created_at=? WHERE id=?",
                        (entered - 600, wo["id"]))
    pstore.conn.commit()

    # The transcript: the lead's own file old, the subagent's beside it fresh.
    cwd = pstore.project_path
    _transcript(config_dir, cwd, session_id, mtime=entered)
    _transcript(config_dir, cwd, session_id, subagent="implementer.jsonl",
               mtime=now - THIRTY_SEC)

    pstore.close()
    return wo["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.get_by_role("tab", name="Time in state", exact=False).click()

        panel = page.locator("#tab-states")
        text = panel.inner_text()
        assert "gate_abandoned" in text, text
        assert "still in it" in text, text
        assert "active" in text and "ago" in text, text

        box = panel.bounding_box()
        page.screenshot(path=SHOTS / "state-active-span.png", full_page=True, clip={
            "x": box["x"] - 8, "y": box["y"] - 8,
            "width": box["width"] + 16, "height": box["height"] + 16})
        browser.close()


def main() -> int:
    home = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = home
    os.environ.pop("JARVIS_WO_ID", None)
    config_dir = Path(tempfile.mkdtemp()) / "claude-config"
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed(config_dir)
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    out = SHOTS / "state-active-span.png"
    assert out.exists() and out.stat().st_size > 5000, out
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
