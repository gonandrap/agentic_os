"""Screenshot the debug page's per-subagent cache anatomy — the PR's UI evidence for
spec 2026-10-02 §1.1-§1.4.

The block used to say `no large writes` about a subagent that wrote 333,500 tokens in
writes none of which reached the floor. It now has four states, and a server-rendered
test that greps for the strings cannot show that a reader can tell them apart. So one
order carries THREE of the four at once, in one turn, with the boundary census beside
them:

  * an UNDER-FLOOR subagent — 23 writes, largest 14,500, nothing at the 20,000 floor
  * a ZERO-WRITE subagent — read the whole time, wrote nothing
  * a subagent rehydrated FROM A SEAL whose four threshold-free keys are ABSENT, which
    is every order sealed before they existed (`total_written is None`)

The fourth state — `writes` non-empty — is unchanged by this branch and already
shot in `docs/screenshots/debug_page_anatomy.png`.

The absent state is built the way it occurs: the order is sealed, then the four keys are
DELETED from one subagent of the stored payload and the transcript pruned, so the page
reads a seal that predates the fields. Everything lives in a temp `JARVIS_HOME` with its
own transcript root, so it never touches the live OS:

    uv run python scripts/screenshot_subagent_cache_anatomy.py
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
OUT = SHOTS / "subagent-cache-anatomy.png"
PORT = 8801
SLUG = "-proj"
SESSION = "sess-subagent-anatomy"

#: The three joins, in the order they are rendered under the turn.
UNDER_FLOOR = "11111111-aaaa-bbbb-cccc-000000000001"
NO_WRITES = "22222222-aaaa-bbbb-cccc-000000000002"
ABSENT = "33333333-aaaa-bbbb-cccc-000000000003"
#: The four keys a seal written before spec 2026-10-02 does not carry.
THRESHOLD_FREE = ("total_written", "max_write", "write_floor", "api_call_count")

NOW = time.time()
TURN = (NOW - 1800, NOW - 900)

#: Asserted on the rendered page before anything is captured: a template that stopped
#: wording one of these must fail here rather than ship a pretty picture of three states
#: where the reviewer was promised four readings.
EXPECTED = (
    "wrote 333,500 in 23 calls — no single write reached the 20,000 floor "
    "(largest 14,500).",
    "wrote nothing to the cache.",
    "cache anatomy not in this seal — sealed before it was recorded.",
    "no boundary — one continuous conversation, so no re-write tax. STRUCTURAL, not "
    "small: 333,500 was still written.",
)


# -- the transcript ---------------------------------------------------------------------


def stamp(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def prompt_row(at: float, text: str, *, sdk: bool = True) -> dict:
    row = {"type": "user", "timestamp": stamp(at), "message": {"content": text}}
    if sdk:
        row["promptSource"] = "sdk"
    return row


def assistant_row(at: float, mid: str, *, write: int = 0, read: int = 0,
                  out: int = 120) -> dict:
    return {"type": "assistant", "timestamp": stamp(at),
            "message": {"id": mid, "model": "claude-opus-5",
                        "usage": {"input_tokens": 12,
                                  "cache_creation_input_tokens": write,
                                  "cache_read_input_tokens": read,
                                  "cache_creation": {"ephemeral_5m_input_tokens": write,
                                                     "ephemeral_1h_input_tokens": 0},
                                  "output_tokens": out},
                        "content": [{"type": "text", "text": "ok"}]}}


def tool_rows(start: float, end: float, tool_id: str, name: str,
              payload: dict) -> list[dict]:
    return [{"type": "assistant", "timestamp": stamp(start),
             "message": {"id": f"m-{tool_id}", "model": "claude-opus-5",
                         "usage": {"input_tokens": 0,
                                   "cache_creation_input_tokens": 0,
                                   "cache_read_input_tokens": 120_000,
                                   "output_tokens": 8},
                         "content": [{"type": "tool_use", "id": tool_id, "name": name,
                                      "input": payload}]}},
            {"type": "user", "timestamp": stamp(end),
             "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]}}]


def parent_rows() -> list[dict]:
    """One turn that joined three subagents — so all three render side by side."""
    return [
        prompt_row(TURN[0] + 2, "You are the worker agent for this work order. "
                                "Read the spec, then fan out."),
        assistant_row(TURN[0] + 8, "p-m1", write=120_000),
        *tool_rows(TURN[0] + 20, TURN[0] + 300, "t-a", "TaskOutput",
                   {"task_id": UNDER_FLOOR}),
        *tool_rows(TURN[0] + 310, TURN[0] + 420, "t-b", "TaskOutput",
                   {"task_id": NO_WRITES}),
        *tool_rows(TURN[0] + 430, TURN[0] + 700, "t-c", "TaskOutput",
                   {"task_id": ABSENT}),
        assistant_row(TURN[1] - 20, "p-m2", read=120_000, write=2_400),
    ]


def under_floor_rows(base: float) -> list[dict]:
    """23 writes of 14,500 — 333,500 tokens, nothing at the 20,000 floor.

    Reads rise monotonically, so the conversation is CONTINUOUS: no cache read goes
    backwards, hence no boundary and a structurally zero re-write tax, which is the
    census line beneath the write state.
    """
    rows = [prompt_row(base, "find every caller of ops.inspect_report", sdk=False)]
    rows += [assistant_row(base + 2 + i * 4, f"a-m{i}", write=14_500,
                           read=100_000 + 20_000 * i, out=40)
             for i in range(23)]
    return rows


def no_write_rows(base: float) -> list[dict]:
    """A subagent that only READ: a measured zero, and a different answer."""
    return [prompt_row(base, "read the spec section and quote §1.4", sdk=False),
            assistant_row(base + 3, "b-m1", read=88_000, out=30),
            assistant_row(base + 9, "b-m2", read=92_000, out=30)]


def absent_rows(base: float) -> list[dict]:
    """A perfectly ordinary subagent. Its ANATOMY is what goes missing, in the seal."""
    return [prompt_row(base, "run the targeted suite and report", sdk=False),
            assistant_row(base + 4, "c-m1", write=64_000, read=40_000, out=60),
            assistant_row(base + 40, "c-m2", write=8_000, read=104_000, out=60)]


# -- the record -------------------------------------------------------------------------


def seed() -> tuple[str, str]:
    from jarvis import autopsy, db, ops
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

    wo = ops.create_work_order(
        "jarvis_os", "a turn that fanned out to three subagents, read back from its seal")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], session_id=SESSION, status="completed")
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="p")
        store.finish_turn(turn["id"], "done", result="ok", cost_usd=2.1, num_turns=8)
        # The window is rewritten directly: `create_turn` stamps now, and the whole
        # fixture is a session that ran half an hour ago. The anatomy joins calls to
        # turns BY these timestamps, so they have to match the transcript rows.
        store.conn.execute(
            "UPDATE wo_turns SET started_at=?, ended_at=? WHERE id=?",
            (TURN[0], TURN[1], turn["id"]))
        store.conn.commit()
    finally:
        store.close()

    root = Path(os.environ["JARVIS_TRANSCRIPT_ROOT"])
    (root / SLUG).mkdir(parents=True, exist_ok=True)
    main = root / SLUG / f"{SESSION}.jsonl"
    main.write_text("".join(json.dumps(r) + "\n" for r in parent_rows()))
    subs = root / SLUG / SESSION / "subagents"
    subs.mkdir(parents=True, exist_ok=True)
    for task, rows, label in ((UNDER_FLOOR, under_floor_rows(TURN[0] + 25),
                               "find the callers"),
                              (NO_WRITES, no_write_rows(TURN[0] + 315),
                               "quote the spec section"),
                              (ABSENT, absent_rows(TURN[0] + 435),
                               "run the targeted suite")):
        (subs / f"agent-{task}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        (subs / f"agent-{task}.meta.json").write_text(
            json.dumps({"agentType": "explorer", "description": label}))

    # The seal, then the FOURTH STATE built the only way it occurs: the keys deleted
    # from one subagent of the stored payload, as a seal written before they existed
    # carries no key at all. The transcript is pruned after, so the seal is what the
    # page can read — which is the case the seal exists for.
    store = ProjectStore(project)
    try:
        payload = autopsy.seal("jarvis_os", project, store.get_work_order(wo["id"]))
        stripped = 0
        for turn_payload in payload["turns"]:
            for sub in turn_payload.get("subagents") or []:
                if sub["task_id"] == ABSENT:
                    for key in THRESHOLD_FREE:
                        sub.pop(key, None)
                    stripped += 1
        if stripped != 1:
            raise SystemExit(f"the seal held {stripped} copies of the subagent whose "
                             f"anatomy must be absent — nothing was captured")
        store.seal_autopsy(wo["id"], db.to_json(payload))
    finally:
        store.close()
    main.unlink()
    return "jarvis_os", wo["id"]


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


def shoot(project: str, wo_id: str) -> Path:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        # Narrow and tall: an ELEMENT shot of the anatomy block, so the text is at its
        # rendered size rather than a full page scaled down to a thumbnail.
        page = browser.new_page(viewport={"width": 1100, "height": 1200},
                                device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{PORT}/wo/{project}/{wo_id}/debug")
        page.wait_for_selector("#block-anatomy")
        page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
        page.wait_for_timeout(200)
        # Whitespace-normalised: the template wraps these sentences across source lines,
        # and HTML collapses that — the SENTENCE is asserted, not its indentation.
        text = " ".join(page.locator("#block-anatomy").inner_text().split())
        for expected in EXPECTED:
            if expected not in text:
                raise SystemExit(f"not on the page: {expected!r} — nothing "
                                 f"was captured")
        page.locator("#block-anatomy").screenshot(path=OUT)
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
