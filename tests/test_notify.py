"""Notification sinks: deep links back into the local UI."""

from __future__ import annotations

import json

import pytest

from jarvis import notify
from jarvis.catalog import Catalog, parse_catalog


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
def sent(monkeypatch):
    """Capture the Telegram payload instead of hitting the network."""
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
