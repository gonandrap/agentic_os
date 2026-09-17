"""Screenshot /config's wiring section against a throwaway fleet, for a PR's UI evidence.

Same shape as `screenshot_config_console.py`, with one difference that matters: the
inventory is INSTALLED rather than discovered. `wiring.discover` reads the machine this
runs on, so a real read would put whoever's Claude configuration into a committed PNG and
would look different on every developer's box. The rows below are the shapes the page has
to render — a block with a measurement, a plugin, a server that follows its plugin, a
server with no lever at all, a user skill.

    uv run python scripts/screenshot_wiring.py
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
PORT = 8798
SERENA = "serena@claude-plugins-official"


def seed() -> None:
    from jarvis import ops, wiring
    from jarvis.central_store import CentralStore

    home = Path(tempfile.mkdtemp())
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}},
        "projects": [
            {"name": "jarvis_os", "path": str(home / "jarvis_os"),
             "description": "the OS itself"},
            {"name": "shared_schedule", "path": str(home / "shared_schedule"),
             "description": "the household calendar"},
        ],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.adopt_config(reason="recording the catalog as it shipped")

    wiring._STATE["inventory"] = wiring.Inventory(ts=time.time(), items=[
        wiring.Item(kind="mcp", name="claude.ai Google Drive",
                    source="claude.ai connector", detail="https://drivemcp.googleapis.com/mcp/v1",
                    lever=wiring.LEVER_CONNECTORS,
                    note="the claude.ai connectors wire as one block"),
        wiring.Item(kind="mcp", name="claude.ai Gmail", source="claude.ai connector",
                    detail="https://gmailmcp.googleapis.com/mcp/v1",
                    lever=wiring.LEVER_CONNECTORS,
                    note="the claude.ai connectors wire as one block"),
        wiring.Item(kind="mcp", name="plugin:serena:serena",
                    source=f"plugin {SERENA}",
                    detail="uvx --from git+https://github.com/oraios/serena serena "
                           "start-mcp-server",
                    lever=f"{wiring.LEVER_PLUGIN}{SERENA}",
                    note="unwiring it unwires the whole plugin"),
        wiring.Item(kind="mcp", name="local-postgres", source="your Claude config",
                    detail="npx @modelcontextprotocol/server-postgres",
                    note="only /mcp disable unwires this one, and that writes to "
                         "your own Claude configuration"),
        wiring.Item(kind="skill", name="heycrypto-pr",
                    source="your skills directory",
                    detail="Use when creating a pull request in the auto_heycrypto repo.",
                    lever=f"{wiring.LEVER_SKILL}heycrypto-pr"),
        wiring.Item(kind="skill", name="Claude Code's bundled skills",
                    source="Claude Code", lever=wiring.LEVER_BUNDLED,
                    detail="the skills and workflows the CLI ships (code review, "
                           "dataviz, …)",
                    note="they wire as one block"),
        wiring.Item(kind="plugin", name=SERENA, source="user scope",
                    detail="Semantic code analysis MCP server · 0 skills",
                    lever=f"{wiring.LEVER_PLUGIN}{SERENA}",
                    note="its MCP servers, skills and agents go with it"),
        wiring.Item(kind="plugin", name="superpowers@claude-plugins-official",
                    source="user scope", detail="Process skills · 14 skills",
                    lever=f"{wiring.LEVER_PLUGIN}superpowers@claude-plugins-official",
                    note="its MCP servers, skills and agents go with it"),
    ])


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})

        # Everything wired, which is what every project looks like until someone acts.
        page.goto(f"{base}/config?scope=projects.shared_schedule#wiring")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "config-wiring-default.png")

        # ...and the same page after two deselections, one of each kind.
        page.click("#wiring form:has(input[value='connectors']) button")
        page.wait_for_load_state()
        page.goto(f"{base}/config?scope=projects.shared_schedule#wiring")
        page.click(f"#wiring form:has(input[value='plugin:{SERENA}']) button")
        page.wait_for_load_state()
        page.goto(f"{base}/config?scope=projects.shared_schedule#wiring")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "config-wiring-deselected.png")

        # The history below it: a deselection is an ordinary config version, and the
        # page says which project it moved and when.
        page.locator("h2", has_text="History").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "config-wiring-history.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)  # `ops.set_config` refuses a worker session
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("config-wiring-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
