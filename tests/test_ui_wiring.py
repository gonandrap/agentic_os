"""The /config page's wiring section (issue #164 item 4).

Its own file rather than a block in tests/test_ui_config.py: this section reads the
USER'S machine, so every test here has to install a fixed inventory first, and mixing
that fixture into the settings-table suite would make a page that shells out to `claude`
one forgotten monkeypatch away.

The write path underneath is proved in tests/test_wiring.py. What is proved here is the
PAGE: that it shows what the user has, that it never offers a control it cannot honour,
and that it distinguishes "still reading" from "you have nothing".
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops, wiring  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.set_config` refuses one."""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture()
def inventory(monkeypatch):
    """A fixed inventory in the cache, so the page never shells out to `claude`.

    Installed into `wiring._STATE` rather than over `discover`, because what the page
    calls is `cached()` — and the states it has to render (populated, still reading) are
    that function's answers, not discovery's.
    """
    def install(items):
        inv = wiring.Inventory(items=list(items), ts=time.time())
        monkeypatch.setattr(wiring, "_STATE", {"inventory": inv, "thread": None})
        return inv

    return install


def section(page: str) -> str:
    return page.split('id="wiring">')[1].split("<h2>History")[0]


def row(page: str, name: str) -> str:
    for tr in section(page).split("<tr>"):
        if f">{name}</span>" in tr:
            return tr
    raise AssertionError(f"no wiring row for {name!r}")


def test_the_section_lists_what_the_user_has_and_wires_all_of_it(client, inventory):
    inventory([
        wiring.Item(kind="mcp", name="claude.ai Gmail", source="claude.ai connector",
                    lever=wiring.LEVER_CONNECTORS),
        wiring.Item(kind="plugin", name="serena@m", source="user scope",
                    lever=f"{wiring.LEVER_PLUGIN}serena@m"),
    ])
    page = client.get("/config?scope=projects.proj_a").text
    assert "● wired" in row(page, "claude.ai Gmail")
    assert "unwire" in row(page, "serena@m")
    # The constraint the user made load-bearing, said on the page that could break it.
    assert "written back to it" in page


def test_the_one_row_with_a_measurement_behind_it_carries_it(client, inventory):
    """The claude.ai block is the only row this feature has evidence about, so it is
    the only row that claims any — and the claim is the finding's, to its own limit."""
    inventory([wiring.Item(kind="mcp", name="claude.ai Gmail", source="claude.ai",
                           lever=wiring.LEVER_CONNECTORS)])
    assert "83.7%" in row(client.get("/config").text, "claude.ai Gmail")


def test_a_server_with_no_lever_is_not_offered_a_control_it_cannot_honour(client,
                                                                         inventory):
    """`/mcp disable` writes the user's own Claude configuration, which this feature may
    not — so a hand-added server has no lever, and a button that silently did nothing
    would be worse than no button."""
    inventory([wiring.Item(kind="mcp", name="hand-added", source="your Claude config",
                           note="only `/mcp disable` unwires this one")])
    cell = row(client.get("/config").text, "hand-added")
    assert "no lever" in cell
    assert "unwire</button>" not in cell


def test_unwiring_from_the_page_is_the_same_write_as_the_terminal(client, inventory):
    lever = f"{wiring.LEVER_PLUGIN}caveman@caveman"
    inventory([wiring.Item(kind="plugin", name="caveman@caveman", source="user scope",
                           lever=lever)])
    r = client.post("/config/wiring",
                    data={"lever": lever, "wired": "", "scope": "projects.proj_a"})
    assert r.status_code == 303
    assert ops.wiring_config("proj_a").disabled_plugins == ("caveman@caveman",)
    page = client.get("/config?scope=projects.proj_a").text
    assert "○ not wired" in row(page, "caveman@caveman")
    assert "wire</button>" in row(page, "caveman@caveman")


def test_the_fleet_scope_writes_os_wiring(client, inventory):
    inventory([])
    client.post("/config/wiring", data={"lever": wiring.LEVER_CONNECTORS,
                                        "wired": "", "scope": "os"})
    assert ops.wiring_config().claude_ai_connectors is False
    paths = [c["path"] for v in ops.config_history(limit=5) for c in v["changes"]]
    assert "os.wiring.claude_ai_connectors" in paths


def test_an_unknown_lever_is_a_flash_not_a_500(client, inventory):
    inventory([])
    r = client.post("/config/wiring", data={"lever": "nonsense:x", "wired": "",
                                            "scope": "os"})
    assert r.status_code == 303 and "error=" in r.headers["location"]


def test_a_read_that_has_not_finished_says_so_rather_than_showing_an_empty_list(
        client, monkeypatch):
    """An empty list would claim the user has no MCP servers. `claude mcp list`
    health-checks every server over the network, so the first read runs BEHIND the
    page — and the page has to say which of the two states it is showing."""
    monkeypatch.setattr(wiring, "cached", lambda **_k: None)
    assert "reading your Claude configuration" in client.get("/config").text


def test_a_machine_whose_claude_cli_cannot_be_read_still_renders_the_settings(
        client, monkeypatch):
    monkeypatch.setattr(wiring, "_STATE",
                        {"inventory": wiring.Inventory(errors=["claude: not found"],
                                                       ts=time.time()),
                         "thread": None})
    page = client.get("/config").text
    assert "claude: not found" in page
    assert "os.defaults.model" in page


def test_nothing_is_behind_a_tab_here_either(client, inventory):
    """§8's rule for this page: Playwright's inner_text() skips display:none, so text
    inside a shut tab panel is unassertable from a browser test."""
    inventory([wiring.Item(kind="skill", name="mine", source="your skills directory",
                           lever=f"{wiring.LEVER_SKILL}mine")])
    page = client.get("/config").text
    assert 'class="tabbed' not in page and 'class="tabpanel' not in page
