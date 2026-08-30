"""The /config tab: a form over `jarvis config` and nothing more (spec §8).

The write path itself is proved in tests/test_config_console.py. What is proved here is
the PAGE — what it shows about where a setting came from, what it refuses, and that its
one POST is the function the CLI calls.
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.set_config` refuses one — see
    the same fixture in tests/test_config_console.py."""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(create_app(), follow_redirects=False)


def row(page: str, label: str) -> str:
    """The one setting row whose left column is `label` — read off the cell's text, since
    a safety setting carries a marker after its name."""
    for tr in page.split("<tr>"):
        cell = tr.partition('<td class="mono">')[2].partition("<")[0].strip()
        if cell == label:
            return tr
    raise AssertionError(f"no row for {label!r} on the page")


def head_id() -> str | None:
    store = CentralStore()
    try:
        version = store.head_config_version()
    finally:
        store.close()
    return version["id"] if version else None


def test_the_page_is_in_the_nav_and_groups_the_key_space(client):
    page = client.get("/config").text
    assert '<a href="/config" class="here">config</a>' in page
    assert "os — the fleet" in page and "project · proj_a" in page
    # The label is the path with its group prefix taken off, in both groups.
    assert ">neo.panel.fast_path</td>" in page
    assert ">worker.model</td>" in page


def test_only_booleans_get_a_toggle(client):
    page = client.get("/config").text
    assert ">turn off</button>" in row(page, "neo.panel.fast_path")
    assert "<button" not in row(page, "defaults.model")


def test_a_safety_setting_is_marked_and_asks_for_its_reason(client):
    """`*.validation.*` changes what a worker is ALLOWED to do, and `ops` will refuse the
    toggle without a reason — so the row says so before the click, not after."""
    safety = row(client.get("/config").text, "validation.enabled")
    assert "⚠" in safety and 'placeholder="reason — required"' in safety
    plain = row(client.get("/config").text, "neo.panel.fast_path")
    assert "⚠" not in plain and 'placeholder="reason — optional"' in plain


def test_provenance_names_the_version_that_set_a_key(client):
    ops.set_config("os.neo.panel.fast_path", False)
    page = client.get("/config").text
    assert f'#{head_id()}"' in row(page, "neo.panel.fast_path")
    assert f'id="{head_id()}"' in page  # …and the link lands on it in the history
    assert "a default of this build" in row(page, "knowledge_digest_chars")


def test_an_adopted_catalog_does_not_make_every_default_look_chosen(client):
    """`adopt` diffs against nothing, so its change list names EVERY resolved path.
    Provenance read off the ledger alone would call all of them deliberate (Neo, q180).
    """
    ops.adopt_config(reason="recording the file")
    page = client.get("/config").text
    assert f'#{head_id()}"' in row(page, "defaults.model")  # the file does set it
    assert "a default of this build" in row(page, "knowledge_digest_chars")


def test_a_toggle_writes_through_ops_set_config(client, catalog_file):
    r = client.post("/config/set", data={"path": "os.neo.panel.fast_path",
                                         "value": "false"})
    assert r.status_code == 303 and r.headers["location"] == "/config"
    assert ops.config_show()["resolved"]["os.neo.panel.fast_path"] is False
    assert json.loads(catalog_file.read_text())["os"]["neo"]["panel"]["fast_path"] is False
    assert head_id() is not None  # the same version row the CLI would have written


def test_a_non_boolean_key_is_refused_and_named_back_to_the_cli(client, catalog_file):
    before = head_id()
    r = client.post("/config/set", data={"path": "os.defaults.model", "value": "true"})
    assert r.status_code == 303
    assert "jarvis config set os.defaults.model" in unquote(r.headers["location"])
    assert head_id() == before
    assert json.loads(catalog_file.read_text())["os"]["defaults"]["model"] == "sonnet"


def test_an_unknown_path_is_refused_by_ops_not_by_the_page(client):
    r = client.post("/config/set", data={"path": "os.nope", "value": "true"})
    assert "not a known setting" in unquote(r.headers["location"])


def test_a_safety_toggle_carries_the_reason_ops_demands(client):
    """`*.validation.*` is a safety key: the page has no rule of its own about it, it
    just shows the refusal `ops` raises."""
    r = client.post("/config/set", data={"path": "os.validation.enabled",
                                         "value": "true"})
    assert "--reason" in unquote(r.headers["location"])
    assert ops.config_show()["resolved"]["os.validation.enabled"] is False

    r = client.post("/config/set", data={"path": "os.validation.enabled",
                                         "value": "true", "reason": "trying it out"})
    assert r.headers["location"] == "/config"
    assert ops.config_show()["resolved"]["os.validation.enabled"] is True


def test_the_diff_defaults_to_what_changed_last(client):
    ops.adopt_config(reason="the file as it shipped")
    ops.set_config("os.ui.port", 9999)
    body = client.get("/config").text.split("<h2>Diff</h2>")[1]
    assert "os.ui.port: 8787 → 9999" in body


def test_any_two_versions_can_be_compared(client):
    ops.adopt_config(reason="the file as it shipped")
    first = head_id()
    ops.set_config("os.ui.port", 9999)
    ops.set_config("os.ui.base_url", "http://x")
    body = client.get(f"/config?a={first}&b={head_id()}").text.split("<h2>Diff</h2>")[1]
    assert "os.ui.port: 8787 → 9999" in body
    assert 'os.ui.base_url: "" → "http://x"' in body


def test_an_unknown_version_is_a_message_not_a_500(client):
    r = client.get("/config?a=cfg-nope&b=cfg-alsonope")
    assert r.status_code == 200
    assert "no config version" in r.text


def test_the_long_parts_are_disclosures_and_nothing_is_behind_a_tab(client):
    """One column of content needs no tabs, and text inside a shut tab panel cannot be
    asserted on by a browser test."""
    ops.adopt_config(reason="the file as it shipped")
    page = client.get("/config").text
    # `.tabbed` is the container the shared script hides panels inside; the CSS for it
    # is in base.html on every page, so the class ATTRIBUTE is what says a page uses it.
    assert 'class="tabbed' not in page and 'class="tabpanel' not in page
    assert "the whole document, as it was written" in page
