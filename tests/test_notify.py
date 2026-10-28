"""Notification sinks: deep links back into the local UI"""

from __future__ import annotations

import json

import pytest

from jarvis import notify
from jarvis.catalog import Catalog, load_catalog, parse_catalog


def make_catalog(ui: dict | None = None) -> Catalog:
    return parse_catalog({
        "os": {
            "notifications": {"sinks": ["telegram"]},
            **({"ui": ui} if ui else {}),
        },
        "projects": [],
    })


def item(**over):
    base = {
        "id": "1", "ts": 0, "project": "shared_schedule", "level": "warning",
        "title": "Work order waiting on you", "body": "", "wo_id": "wo-42",
    }
    base.update(over)
    return base


def test_wo_url_defaults_to_local_ui_port():
    cat = make_catalog()
    assert notify.wo_url(cat, "shared_schedule", "wo-42") == (
        "http://127.0.0.1:8787/wo/shared_schedule/wo-42#pending"
    )


def test_wo_url_honours_configured_port_and_base_url():
    assert notify.wo_url(make_catalog({"port": 9000}), "p", "wo-1").startswith(
        "http://127.0.0.1:9000/wo/p/wo-1"
    )
    cat = make_catalog({"base_url": "https://jarvis.example.com/"})
    assert notify.wo_url(cat, "p", "wo-1") == "https://jarvis.example.com/wo/p/wo-1#pending"


def test_wo_url_quotes_path_segments():
    assert "my%20proj" in notify.wo_url(make_catalog(), "my proj", "wo-1")


@pytest.fixture
def sent(monkeypatch, allow_external_sinks):
    """Capture the Telegram payload instead of hitting the network"""
    calls: list[dict] = []

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=0):
        calls.append(json.loads(req.data.decode()))
        return Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("JARVIS_TELEGRAM_CHAT_ID", "c")
    return calls


def test_telegram_links_the_wo_id_to_the_ui(sent):
    assert notify.sink_telegram(item(), make_catalog()) == "ok"
    text = sent[0]["text"]
    assert sent[0]["parse_mode"] == "HTML"
    assert '<a href="http://127.0.0.1:8787/wo/shared_schedule/wo-42#pending">wo-42</a>' in text


def test_telegram_escapes_html_in_user_text(sent):
    notify.sink_telegram(item(title="fix <b>bug</b> & co", wo_id=None), make_catalog())
    text = sent[0]["text"]
    assert "&lt;b&gt;bug&lt;/b&gt; &amp; co" in text
    assert "<b>bug</b>" not in text


def test_telegram_without_wo_id_has_no_link(sent):
    notify.sink_telegram(item(wo_id=None), make_catalog())
    assert "<a href" not in sent[0]["text"]


# -- deep links that would dead-end

def test_route_validates_the_deep_link_before_it_ships(jarvis_home, fake_claude,
                                                       catalog_file, sent):
    """A notification is often the user's only way into a work order"""
    from jarvis import ops
    from jarvis.central_store import CentralStore

    ops.start_os(str(catalog_file), foreground=True)
    catalog = load_catalog(catalog_file)
    catalog.os.notification_sinks = ["telegram"]  # the fixture catalog only logs
    real = ops.create_work_order("proj_a", "a real one")

    central = CentralStore()
    try:
        central.add_inbox(project="proj_a", title="live", wo_id=real["id"])
        central.add_inbox(project="proj_gone", title="stale project", wo_id="wo-x")
        central.add_inbox(project="proj_a", title="stale wo", wo_id="wo-deleted")
        notify.route_new_inbox(central, catalog)
    finally:
        central.close()

    live, stale_project, stale_wo = (c["text"] for c in sent)
    assert f'<a href="http://127.0.0.1:8787/wo/proj_a/{real["id"]}#pending">' in live
    # No link at all rather than one that 404s: the user cannot tell a stale link from a broken...
    assert "<a href" not in stale_project
    assert "not registered with this Jarvis" in stale_project
    assert "<a href" not in stale_wo
    assert "does not exist in &#x27;proj_a&#x27;" in stale_wo


def test_the_log_sink_records_why_a_link_was_withheld(jarvis_home, fake_claude,
                                                      catalog_file):
    """On disk too — otherwise a dead link is only ever visible to whoever tapped it"""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.paths import logs_dir

    ops.start_os(str(catalog_file), foreground=True)
    central = CentralStore()
    try:
        central.add_inbox(project="proj_gone", title="stale", wo_id="wo-x")
        notify.route_new_inbox(central, load_catalog(catalog_file))
    finally:
        central.close()

    log = (logs_dir() / "notifications.log").read_text()
    assert "[no deep link: project 'proj_gone' is not registered" in log


def test_a_sink_called_directly_still_links(sent):
    """Validation needs a CentralStore"""
    notify.sink_telegram(item(), make_catalog())
    assert "<a href" in sent[0]["text"]


# -- the desktop sink's binary


def test_desktop_prefers_notify_send(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert notify.desktop_argv("T", "B") == ["notify-send", "T", "B"]


def test_desktop_falls_back_to_osascript_on_a_mac(monkeypatch):
    """Title and body ride in argv, so a quote in either cannot break the AppleScript"""
    monkeypatch.setattr(notify.shutil, "which",
                        lambda name: "/usr/bin/osascript" if name == "osascript" else None)
    argv = notify.desktop_argv("Jarvis [p]", 'say "hi"')
    assert argv[0] == "osascript"
    assert argv[-2:] == ["Jarvis [p]", 'say "hi"']
    assert all('"hi"' not in a for a in argv[1:-2])


def test_desktop_with_neither_binary_says_so(monkeypatch):
    monkeypatch.setattr(notify.shutil, "which", lambda name: None)
    assert notify.desktop_argv("T", "B") is None
