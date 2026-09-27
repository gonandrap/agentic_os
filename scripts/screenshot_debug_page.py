"""Screenshot `/wo/{project}/{wo_id}/debug` — the PR's UI evidence for spec §7 of
docs/specs/2026-09-24-order-observability.md.

The page is four independent readings of one order, so the fixture has to carry all four:
a diagnosis with holds and a blocker, a live frame with a tool call in flight, an anatomy
with three turns, tool parameters, a subagent and all three write causes, and a context
ledger with a delta, a residual and a prefix break. Two more shots cover the cases the
tests assert in strings and nobody can see: ONE block failing while the other three stand,
and an order with no session at all (absent is never zero, issue #227).

Everything lives in a temp `JARVIS_HOME` with its own transcript root, so it never touches
the live OS or the developer's real `~/.claude/projects`:

    uv run python scripts/screenshot_debug_page.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8799
SLUG = "-proj"
SESSION = "sess-debug-demo"
TASK = "a7b62083-1111-2222-3333-444455556666"

#: The clock every window and every transcript row is hung off, so the page shows recent
#: ages ("4m ago") rather than 1970.
NOW = time.time()


# -- the transcript ---------------------------------------------------------------------
#
# Row builders in the shape Claude Code writes, as `tests/test_inspection.py` describes
# them. Copied here rather than imported from the tests: a script that reaches into a test
# module breaks when that module is rewritten, which is the churn §4 is already doing.


def stamp(at: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str, *, sdk: bool = True) -> dict:
    row = {"type": "user", "timestamp": stamp(at), "message": {"content": text}}
    if sdk:
        row["promptSource"] = "sdk"
    return row


def assistant_row(at: float, mid: str, *, write: int = 0, read: int = 0,
                  content: list | None = None, out: int = 120) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 12,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read,
                                  "cache_creation": {"ephemeral_5m_input_tokens": write,
                                                     "ephemeral_1h_input_tokens": 0},
                                  "output_tokens": out},
                        "content": content or [{"type": "text", "text": "ok"}]}}


def tool_use_row(at: float, tool_id: str, name: str, payload: dict, *,
                 read: int = 0) -> dict:
    # `read` matters on the OPEN span: the live frame's `context` is the LATEST call's
    # context, and a call with no counts would put a bare 0 on the page.
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": read, "output_tokens": 8},
                        "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                     "input": payload}]}}


def tool_rows(start: float, end: float, tool_id: str, name: str,
              payload: dict) -> list[dict]:
    return [tool_use_row(start, tool_id, name, payload),
            {"type": "user", "timestamp": stamp(end),
             "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}}]


#: The three turn windows, and the gaps between them are load-bearing: turn 2's write lands
#: 130s after turn 1's last call (inside the 5-minute TTL, so `prefix-miss` — a defect) and
#: turn 3's lands 890s after turn 2's (outside it, so `ttl-expiry`). All three causes on one
#: page is the point of the classification.
T1 = (NOW - 1800, NOW - 1500)
T2 = (NOW - 1400, NOW - 1100)
T3 = (NOW - 240, None)


def transcript_rows() -> list[dict]:
    return [
        prompt_row(T1[0] + 2, "You are the worker agent for this work order. "
                              "Read the section spec first."),
        assistant_row(T1[0] + 8, "m1", write=124_000),
        *tool_rows(T1[0] + 20, T1[0] + 46, "t-read", "Read",
                   {"file_path": "/home/you/workspace/agentic_os/src/jarvis/ops.py",
                    "offset": 9600, "limit": 240}),
        *tool_rows(T1[0] + 50, T1[0] + 96, "t-bash", "Bash",
                   {"command": "uv run pytest tests/test_ui_debug.py -q",
                    "description": "run the targeted suite"}),
        # The join: 190 seconds of the parent turn spent waiting on a subagent, which is
        # `blocked` in the partition and never `generating`.
        *tool_rows(T1[0] + 100, T1[0] + 290, "t-task", "TaskOutput", {"task_id": TASK}),
        assistant_row(T1[1] - 20, "m2", read=124_000, write=1_200),

        prompt_row(T2[0] + 2, "[Neo, answering for the user] yes, proceed"),
        assistant_row(T2[0] + 10, "m3", read=90_000, write=96_000),
        *tool_rows(T2[0] + 30, T2[0] + 58, "t-edit", "Edit",
                   {"file_path": "src/jarvis/ui/app.py",
                    "old_string": "QUIET_PATHS = (\"/api/status\",)",
                    "new_string": "QUIET_SUFFIXES = (\"/live\",)"}),
        assistant_row(T2[1] - 30, "m4", read=186_000, write=2_400),

        prompt_row(T3[0] + 2, "<task-notification>a subagent finished"),
        assistant_row(T3[0] + 9, "m5", read=190_000, write=48_000),
        *tool_rows(T3[0] + 20, T3[0] + 41, "t-grep", "Grep",
                   {"pattern": "inspect_report", "path": "src/jarvis"}),
        # OPEN on purpose: a `tool_use` with no `tool_result` while the record says a turn
        # is running is the one state `live` calls `working`.
        tool_use_row(NOW - 26, "t-open", "Bash",
                     {"command": "uv run pytest tests/ -q",
                      "description": "the whole suite"}, read=238_000),
    ]


def subagent_rows() -> list[dict]:
    base = T1[0] + 105
    return [
        prompt_row(base, "find every caller of ops.inspect_report", sdk=False),
        assistant_row(base + 4, "s-m1", write=31_000),
        *tool_rows(base + 10, base + 38, "s-t1", "Grep", {"pattern": "inspect_report"}),
        assistant_row(base + 60, "s-m2", read=31_000, write=9_000),
    ]


# -- the record -------------------------------------------------------------------------


def ingredients(context, *, prompt_bytes: int, dirs_bytes: int,
                knowledge: int | None) -> list[dict]:
    """One turn's measured ingredients. `context._row` rather than hand-built dicts: the
    shape belongs to that module and a second copy here would drift out of the payload
    the page renders."""
    rows = [context._row("worker_prompt", prompt_bytes),
            context._row("agent_persona", 1_100),
            context._row("add_dirs", dirs_bytes)]
    rows.append(context._row("knowledge_index", knowledge) if knowledge is not None
                else context._row("knowledge_index", None,
                                  note="no knowledge block reached this turn's window — "
                                       "absent, not empty"))
    return rows


def seed() -> tuple[str, str, str]:
    from jarvis import context, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.set_state("catalog_path", str(catalog))
    central.conn.commit()
    central.close()

    wo = ops.create_work_order(
        "jarvis_os", "the debugging view: the dashboard page and the JSON behind it")
    empty = ops.create_work_order("jarvis_os", "filed, never dispatched")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], session_id=SESSION, status="running")
        for seq, (window, payload) in enumerate((
                (T1, ingredients(context, prompt_bytes=18_400, dirs_bytes=800,
                                 knowledge=4_200)),
                (T2, ingredients(context, prompt_bytes=19_100, dirs_bytes=5_400,
                                 knowledge=None)),
                (T3, None)), start=1):
            turn = store.create_turn(wo["id"], kind="dispatch" if seq == 1 else "message",
                                     prompt="p")
            if window[1] is not None:
                store.finish_turn(turn["id"], "done", result="ok", cost_usd=1.4,
                                  num_turns=6)
            # The windows are rewritten directly because `create_turn` stamps `now` and the
            # whole fixture is a session that ran half an hour ago. `context_report` joins
            # cache writes to turns BY these timestamps, so they have to match the rows.
            store.conn.execute(
                "UPDATE wo_turns SET started_at=?, ended_at=?, context_json=? WHERE id=?",
                (window[0], window[1],
                 json.dumps({"schema": context.SCHEMA, "ingredients": payload,
                             "caps": context.caps(),
                             "token_bytes": context.TOKEN_BYTES}) if payload else None,
                 turn["id"]))
        # Two holds the OS's own record explains, one of each shape the page renders: a
        # Neo question that was answered, and a gate still open right now.
        for kind, payload, at in (
                ("question_asked", {"neo_question_id": "q772"}, T1[1] + 30),
                ("neo_answered", {"neo_question_id": "q772"}, T2[0] - 40),
                ("gate_requested", {"approval_id": "ap-4f1c"}, NOW - 120)):
            store.add_event(wo["id"], kind, payload)
            store.conn.execute(
                "UPDATE wo_events SET ts=? WHERE rowid=(SELECT MAX(rowid) FROM wo_events)",
                (at,))
        store.add_assumption(wo["id"],
                             "I took the `?debug=1` query parameter to mean debug-level "
                             "timeline events and left it alone.")
        store.conn.commit()
    finally:
        store.close()

    root = Path(os.environ["JARVIS_TRANSCRIPT_ROOT"])
    (root / SLUG).mkdir(parents=True, exist_ok=True)
    (root / SLUG / f"{SESSION}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in transcript_rows()))
    subs = root / SLUG / SESSION / "subagents"
    subs.mkdir(parents=True, exist_ok=True)
    (subs / f"agent-{TASK}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in subagent_rows()))
    (subs / f"agent-{TASK}.meta.json").write_text(
        json.dumps({"agentType": "explorer", "description": "find the callers"}))
    return "jarvis_os", wo["id"], empty["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


# -- the shots --------------------------------------------------------------------------


def shoot(project: str, wo_id: str, empty_id: str) -> list[Path]:
    from jarvis import ops
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    url = f"http://127.0.0.1:{PORT}/wo/{project}/{wo_id}/debug"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1500})
        page.goto(url)
        page.wait_for_selector("#block-anatomy")

        def clip(*ids: str) -> dict:
            boxes = [page.locator(f"#{i}").bounding_box() for i in ids]
            top = min(b["y"] for b in boxes)
            # From the top of the page for the first shot, so the reviewer sees which
            # order they are looking at and the links back to it.
            start = 0.0 if "block-diagnosis" in ids else max(0.0, top - 70)
            end = max(b["y"] + b["height"] for b in boxes) + 40
            # Clamped to the document: a clip that runs past it is not in the image.
            end = min(end, page.evaluate("document.documentElement.scrollHeight"))
            return {"x": 0, "y": start, "width": 1280, "height": end - start}

        def shot(name: str, **kw) -> None:
            path = SHOTS / name
            # `full_page` with a clip: a clip taller than the viewport is outside the
            # rendered image otherwise, and these blocks are taller than any viewport.
            page.screenshot(path=path, full_page=True, **kw)
            out.append(path)

        # Above the fold: the diagnosis first, always — "why is nothing happening" is the
        # largest bug class and must not be below it.
        shot("debug_page_top.png", clip=clip("block-diagnosis", "block-live"))
        page.locator("#block-anatomy").screenshot(path=SHOTS / "debug_page_anatomy.png")
        out.append(SHOTS / "debug_page_anatomy.png")
        page.locator("#block-context").screenshot(path=SHOTS / "debug_page_context.png")
        out.append(SHOTS / "debug_page_context.png")

        # The poll, verified rather than claimed: the frame's silence clock is re-read from
        # `/api/wo/…/live` every two seconds, so this text has to move on its own.
        before = page.locator("#live-stale").inner_text()
        page.wait_for_timeout(5_000)
        after = page.locator("#live-stale").inner_text()
        assert after != before, f"the live block did not repaint ({before!r} stayed)"
        # An ELEMENT screenshot, not a clip: after the element shots above, a page clip
        # taller than the viewport is refused ("clipped area … outside the resulting
        # image") on this Playwright, and the live block is one element anyway.
        page.locator("#block-live").screenshot(path=SHOTS / "debug_page_poll.png")
        out.append(SHOTS / "debug_page_poll.png")
        print(f"poll: silence clock moved {before} -> {after}")

        # ONE block failing. Patched in this process because the server runs in a thread of
        # it: three blocks intact, the fourth carrying its note, and NOT an error page.
        real = ops.inspect_report
        ops.inspect_report = lambda *a, **k: 1 / 0
        try:
            page.goto(url)
            page.wait_for_selector("#block-anatomy")
            shot("debug_page_degraded.png")
        finally:
            ops.inspect_report = real

        # No session at all: three honest notes and not one zero.
        page.goto(f"http://127.0.0.1:{PORT}/wo/{project}/{empty_id}/debug")
        page.wait_for_selector("#block-context")
        shot("debug_page_no_transcript.png")
        browser.close()
    return out


def wait_for_server(timeout: float = 30.0) -> None:
    """Block until the dashboard answers, and RAISE if it never does.

    A fixed `sleep(2)` is a race: uvicorn binds when it is ready and not when the clock
    says so, and losing it gives `ERR_CONNECTION_REFUSED` partway through — which leaves
    some PNGs rewritten and some stale from the previous run, the one failure mode a
    screenshot script must not have.
    """
    import urllib.error
    import urllib.request

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


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = tmp
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(tmp) / "claude-projects")
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, wo_id, empty_id = seed()
    # Refuse a port already in use rather than screenshotting somebody else's dashboard:
    # two copies of this script at once is how a run ends up half against one fixture and
    # half against another, and the PNGs would not say so.
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"port {PORT} is already answering — another copy of this "
                             f"script is running; nothing was captured")
    threading.Thread(target=serve, daemon=True).start()
    wait_for_server()
    for path in shoot(project, wo_id, empty_id):
        print(f"{path}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
