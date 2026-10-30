"""Screenshot the debug page's context-ingredient modal, seeded by `screenshot_debug_page`"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshot_debug_page as base  # noqa: E402

# Its own port, so it never collides with a running `screenshot_debug_page`
base.PORT = 8813

TEXT = ("You are the worker agent for Jarvis work order `wo-demo` in project `jarvis_os`.\n\n"
        "# Work order: the debugging view\n\nRead the section spec first.\n")


def store_text(project: str, wo_id: str) -> None:
    """Give turn 1's `worker_prompt` row a stored text, as `context.record` now does"""
    from jarvis import context
    from jarvis.project_store import ProjectStore

    store = ProjectStore(Path(os.environ["JARVIS_HOME"]) / project)
    try:
        turn = store.list_turns(wo_id)[0]
        payload = json.loads(turn["context_json"])
        for row in payload["ingredients"]:
            if row["name"] == "worker_prompt":
                row["sha"] = context._sha(TEXT)
        store.conn.execute("INSERT OR IGNORE INTO ctx_blobs VALUES (?, ?)",
                           (context._sha(TEXT), TEXT))
        store.conn.execute("UPDATE wo_turns SET context_json=? WHERE id=?",
                           (json.dumps(payload), turn["id"]))
        store.conn.commit()
    finally:
        store.close()


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = tmp
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(tmp) / "claude-projects")
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(base.REPO / "src"))
    project, wo_id, _empty, _sealed = base.seed()
    store_text(project, wo_id)
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", base.PORT)) == 0:
            raise SystemExit(f"port {base.PORT} is already answering; nothing was captured")
    threading.Thread(target=base.serve, daemon=True).start()
    base.wait_for_server()

    from playwright.sync_api import sync_playwright

    base.SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{base.PORT}/wo/{project}/{wo_id}/debug#ctx-1")
        for name, seq, shot in (("worker_prompt", 1, "debug_context_modal_text.png"),
                                ("agent_persona", 1, "debug_context_modal_not_captured.png")):
            page.locator(f'.ctx-open[data-seq="{seq}"][data-name="{name}"]').click()
            page.wait_for_function(
                "document.getElementById('ctx-modal-note').textContent !== 'loading…'")
            page.screenshot(path=base.SHOTS / shot)
            print(base.SHOTS / shot)
            page.locator("#ctx-modal-close").click()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
