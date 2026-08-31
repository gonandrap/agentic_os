"""The /config tab: a form over `jarvis config` (spec §8; wo-516126ce for the rest).

The write path itself is proved in tests/test_config_console.py. What is proved here is
the PAGE — what it shows about where a setting came from, which settings a scope, node
or search puts on screen, and what its one POST refuses.

WHAT THIS FILE CANNOT PROVE, and the reason tests_browser/test_ui_browser.py has a
`/config` block: these are assertions about an HTML string. A rendered width, a rule
drawn past the panel it belongs to, a control that is on the page but two characters
wide — none of them are visible from here, and all of them were shipped.
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


def labels(page: str) -> list[str]:
    """Every setting the page is currently showing, in order."""
    out = []
    for tr in page.split("<tr>")[1:]:
        cell = tr.partition('<td class="mono">')[2].partition("<")[0].strip()
        if cell:
            out.append(cell)
    return out


def head_id() -> str | None:
    store = CentralStore()
    try:
        version = store.head_config_version()
    finally:
        store.close()
    return version["id"] if version else None


def test_the_page_is_in_the_nav_and_opens_on_the_fleet(client):
    page = client.get("/config").text
    assert '<a href="/config" class="here">config</a>' in page
    assert "os — the fleet" in page
    # The label is the path with its scope prefix taken off.
    assert ">neo.panel.fast_path</td>" in page
    # ...and a project's settings are NOT on the fleet's page: they are a scope away.
    assert ">worker.model</td>" not in page


def test_a_project_is_a_scope_in_the_picker_not_a_scroll(client):
    """Item 4: the projects used to be one long column below the fleet's settings."""
    page = client.get("/config").text
    assert '<select name="scope"' in page
    assert '<option value="projects.proj_a"' in page

    proj = client.get("/config?scope=projects.proj_a").text
    assert ">worker.model</td>" in proj
    assert ">neo.panel.fast_path</td>" not in proj


def test_an_unknown_scope_falls_back_to_the_fleet(client):
    page = client.get("/config?scope=projects.nope").text
    assert "os — the fleet" in page and ">neo.panel.fast_path</td>" in page


def test_the_tree_lists_the_nodes_of_the_selected_scope(client):
    page = client.get("/config").text
    assert 'href="/config?scope=os&amp;node=neo"' in page
    assert 'href="/config?scope=os&amp;node=validation"' in page
    # A project's nodes belong to the project's own tree, not the fleet's.
    assert 'href="/config?scope=os&amp;node=worker"' not in page
    assert 'href="/config?scope=projects.proj_a&amp;node=worker"' \
        in client.get("/config?scope=projects.proj_a").text


def test_a_node_shows_its_subtree_and_nothing_else(client):
    """Item 6: clicking a node narrows the pane to what lives under it."""
    shown = labels(client.get("/config?scope=os&node=neo").text)
    assert shown and all(x.startswith("neo.") for x in shown)
    assert "neo.panel.fast_path" in shown          # a grandchild, not just a child
    assert "validation.enabled" not in shown


def test_a_deeper_node_narrows_further_and_unfolds_its_ancestors(client):
    page = client.get("/config?scope=os&node=neo.panel").text
    shown = labels(page)
    assert shown and all(x.startswith("neo.panel.") for x in shown)
    # `neo` is the selected node's parent, so the tree has it open with the child under it
    assert 'href="/config?scope=os&amp;node=neo.panel"' in page


def test_an_unknown_node_shows_the_whole_scope_rather_than_nothing(client):
    shown = labels(client.get("/config?scope=os&node=nope").text)
    assert "neo.panel.fast_path" in shown and "validation.enabled" in shown


def test_search_finds_a_setting_without_knowing_its_node(client):
    """Item 5, and the reason a search OVERRIDES the node rather than narrowing it:
    someone typing `autocompact` is searching because they do not know where it lives."""
    shown = labels(client.get("/config?scope=os&q=autocompact").text)
    assert shown == ["defaults.autocompact_window"]

    # ...even with a node selected that the match is not under.
    shown = labels(client.get("/config?scope=os&node=neo&q=autocompact").text)
    assert shown == ["defaults.autocompact_window"]


def test_search_results_are_grouped_by_the_node_they_came_out_of(client):
    """Neo, q194: the tree is NOT narrowed by a search, so each result has to say where
    it lives — `alarm` matches under `inspect` on this catalog and nowhere else, `model`
    matches under three different nodes."""
    page = client.get("/config?scope=os&q=model").text
    assert 'class="cfg-group"' in page
    assert 'node=neo">neo</a>' in page
    assert 'node=validation">validation</a>' in page


def test_a_search_that_matches_nothing_says_so(client):
    page = client.get("/config?scope=os&q=zzzznope").text
    assert labels(page) == []
    assert "no setting under" in page


def test_every_setting_is_editable_not_just_the_booleans(client):
    """Item 1/2: the boolean toggle was the only control on the page."""
    page = client.get("/config").text
    assert ">turn off</button>" in row(page, "neo.panel.fast_path")
    assert ">Save</button>" in row(page, "defaults.model")
    assert ">Save</button>" in row(page, "defaults.autocompact_window")
    assert 'type="number"' in row(page, "defaults.autocompact_window")
    assert 'type="text"' in row(page, "defaults.model")
    assert "<textarea" in row(page, "notifications.sinks")


def test_an_editor_starts_from_the_value_that_is_already_set(client):
    page = client.get("/config").text
    assert 'value="sonnet"' in row(page, "defaults.model")
    assert 'value="400000"' in row(page, "defaults.autocompact_window")
    # Jinja escapes the quotes inside a textarea; the JSON is what reaches the box.
    assert "[&#34;log&#34;]" in row(page, "notifications.sinks")


def test_a_safety_setting_is_marked_and_asks_for_its_reason(client):
    """`*.validation.*` changes what a worker is ALLOWED to do, and `ops` will refuse the
    write without a reason — so the row says so before the click, not after."""
    safety = row(client.get("/config").text, "validation.enabled")
    assert "⚠" in safety and 'placeholder="reason — required"' in safety
    plain = row(client.get("/config").text, "neo.panel.fast_path")
    assert "⚠" not in plain and 'placeholder="reason — optional"' in plain


def test_the_marker_has_a_legend_on_the_page(client):
    """Item 9: `title=` is a tooltip nobody hovers over a symbol to find."""
    page = client.get("/config").text
    assert "a safety setting: it changes what a worker is" in page
    assert "how many settings are under it" in page


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


def test_a_number_is_written_as_a_number(client, catalog_file):
    r = client.post("/config/set", data={"path": "os.defaults.autocompact_window",
                                         "value": "250000"})
    assert r.headers["location"] == "/config"
    assert json.loads(catalog_file.read_text())["os"]["defaults"]["autocompact_window"] \
        == 250000


def test_a_list_is_written_as_a_list(client, catalog_file):
    r = client.post("/config/set", data={"path": "os.notifications.sinks",
                                         "value": '["log", "telegram"]'})
    assert r.headers["location"] == "/config"
    assert json.loads(catalog_file.read_text())["os"]["notifications"]["sinks"] \
        == ["log", "telegram"]


def test_a_text_setting_takes_its_text_verbatim(client, catalog_file):
    """`parse_config_value` would read `123` as a number, and a model named `123` is a
    string. The value already there is what says which of the two this is."""
    r = client.post("/config/set", data={"path": "os.defaults.model", "value": "123"})
    assert r.headers["location"] == "/config"
    assert json.loads(catalog_file.read_text())["os"]["defaults"]["model"] == "123"


def test_a_value_of_the_wrong_type_is_refused_by_the_page(client, catalog_file):
    """Neo, q193: `parse_catalog` takes `true` for a whole number without a word, so the
    page is what has to notice."""
    r = client.post("/config/set", data={"path": "os.defaults.autocompact_window",
                                         "value": "true"})
    assert r.status_code == 303
    assert "takes a whole number" in unquote(r.headers["location"])
    assert json.loads(catalog_file.read_text())["os"]["defaults"] \
        .get("autocompact_window") is None


def test_a_bad_number_is_a_flash_and_not_a_500(client):
    """`parse_catalog` coerces with a bare `int()`, whose `ValueError` is not something
    `ops` converts — unguarded it reaches the user as a traceback."""
    r = client.post("/config/set", data={"path": "os.ui.port", "value": "eight"})
    assert r.status_code == 303
    assert "takes a whole number" in unquote(r.headers["location"])


def test_a_setting_cannot_be_nulled_from_the_page(client):
    """`jarvis config unset` is what clears a key; a key present and null is a third
    thing, and every reader of the catalog treats it as the value."""
    r = client.post("/config/set", data={"path": "os.defaults.model", "value": "null"})
    assert r.headers["location"] == "/config"
    assert ops.config_show()["resolved"]["os.defaults.model"] == "null"  # the string


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


def test_a_save_comes_back_to_the_view_it_was_made_from(client):
    """Without this the reader is dropped at the top of the fleet's settings after every
    save, and has to find their project and their node again."""
    back = "/config?scope=projects.proj_a&node=worker"
    r = client.post("/config/set", data={"path": "projects.proj_a.worker.model",
                                         "value": "opus", "back": back})
    assert r.headers["location"] == back

    r = client.post("/config/set", data={"path": "projects.proj_a.max_concurrent",
                                         "value": "lots", "back": back})
    assert r.headers["location"].startswith(f"{back}&error=")


def test_the_page_will_not_be_used_as_an_open_redirect(client):
    r = client.post("/config/set", data={"path": "os.neo.panel.fast_path",
                                         "value": "false",
                                         "back": "https://evil.example/"})
    assert r.headers["location"] == "/config"


def test_the_editor_form_carries_the_view_it_was_rendered_in(client):
    page = client.get("/config?scope=projects.proj_a&node=worker").text
    assert 'name="back" value="/config?scope=projects.proj_a&amp;node=worker"' in page


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


def test_an_empty_ledger_is_not_reported_as_a_hand_edit(client):
    """Item 10: `drift` is also true when NOTHING has been recorded, and the drift
    sentence is the wrong one for a fleet nobody has adopted yet — it made every fresh
    install open on a warning about an edit that never happened."""
    page = client.get("/config").text
    assert "no version recorded yet" in page
    assert "edited outside Jarvis" not in page


def test_a_hand_edit_is_explained_rather_than_alarmed_about(client, catalog_file):
    ops.adopt_config(reason="the file as it shipped")
    data = json.loads(catalog_file.read_text())
    data["os"]["ui"] = {"port": 9191}
    catalog_file.write_text(json.dumps(data))
    page = client.get("/config").text
    assert "edited outside Jarvis" in page
    assert "nothing is broken and nothing is waiting on" in page
    assert "jarvis config adopt" in page


def test_the_long_parts_are_disclosures_and_nothing_is_behind_a_tab(client):
    """One column of content needs no tabs, and text inside a shut tab panel cannot be
    asserted on by a browser test."""
    ops.adopt_config(reason="the file as it shipped")
    page = client.get("/config").text
    # `.tabbed` is the container the shared script hides panels inside; the CSS for it
    # is in base.html on every page, so the class ATTRIBUTE is what says a page uses it.
    assert 'class="tabbed' not in page and 'class="tabpanel' not in page
    assert "the whole document, as it was written" in page
