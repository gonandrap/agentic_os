"""Notification routing: project outboxes -> central inbox -> sinks.

Sinks are intentionally simple functions. `log` is always on; `telegram` activates when
its env vars are present; `desktop` uses notify-send when available. Projects emit via
`jarvis notify`, which writes their outbox — they never talk to sinks directly. That is
the unified pipeline existing per-project Telegram scripts migrate to.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from .catalog import Catalog
from .central_store import CentralStore
from .paths import logs_dir

LEVEL_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}

#: Anchor on the work-order page marking whatever is waiting on the user
#: (pending assumptions, the attention banner, otherwise the reply box).
PENDING_ANCHOR = "pending"


def ui_base_url(catalog: Catalog) -> str:
    """Root URL of the local dashboard, as a notification recipient should reach it."""
    return catalog.os.ui_base_url or f"http://127.0.0.1:{catalog.os.ui_port}"


def wo_url(catalog: Catalog, project: str, wo_id: str) -> str:
    """Deep link to a work order's history, scrolled to what needs the user."""
    quote = urllib.parse.quote
    return f"{ui_base_url(catalog)}/wo/{quote(project)}/{quote(wo_id)}#{PENDING_ANCHOR}"


def sink_log(item: dict[str, Any], catalog: Catalog) -> str:
    logs_dir().mkdir(parents=True, exist_ok=True)
    path = logs_dir() / "notifications.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["ts"]))
    with path.open("a") as f:
        f.write(
            f"{stamp} [{item['level'].upper()}] {item['project']}: {item['title']}"
            + (f" — {item['body']}" if item["body"] else "")
            + (f" (wo={item['wo_id']})" if item.get("wo_id") else "")
            + "\n"
        )
    return "ok"


def sink_telegram(item: dict[str, Any], catalog: Catalog) -> str:
    token = os.environ.get(catalog.os.telegram_token_env, "")
    chat_id = os.environ.get(catalog.os.telegram_chat_id_env, "")
    if not token or not chat_id:
        return f"skipped: {catalog.os.telegram_token_env}/{catalog.os.telegram_chat_id_env} not set"
    emoji = LEVEL_EMOJI.get(item["level"], "")
    esc = html.escape
    # HTML (not Markdown): work order ids become tappable links into the local UI,
    # and titles containing _ or * no longer break the parse.
    text = f"{emoji} <b>[{esc(item['project'])}]</b> {esc(item['title'])}"
    if item["body"]:
        text += f"\n{esc(item['body'])}"
    if item.get("wo_id"):
        url = wo_url(catalog, item["project"], item["wo_id"])
        text += f'\n<a href="{esc(url, quote=True)}">{esc(item["wo_id"])}</a>'
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                         "disable_web_page_preview": True}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return "ok" if resp.status == 200 else f"http {resp.status}"
    except Exception as e:  # noqa: BLE001 — sink failures must never crash the router
        return f"error: {e}"


def sink_desktop(item: dict[str, Any], catalog: Catalog) -> str:
    if not shutil.which("notify-send"):
        return "skipped: notify-send not available"
    try:
        subprocess.run(
            ["notify-send", f"Jarvis [{item['project']}]", f"{item['title']}\n{item['body']}"],
            timeout=10, check=False,
        )
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


SINKS: dict[str, Callable[[dict[str, Any], Catalog], str]] = {
    "log": sink_log,
    "telegram": sink_telegram,
    "desktop": sink_desktop,
}


def route_new_inbox(central: CentralStore, catalog: Catalog) -> int:
    """Send every 'new' inbox item through the configured sinks. Returns count."""
    sinks = list(dict.fromkeys(["log", *catalog.os.notification_sinks]))
    count = 0
    for item in central.new_inbox():
        results = {}
        for name in sinks:
            fn = SINKS.get(name)
            results[name] = fn(item, catalog) if fn else f"unknown sink {name!r}"
        central.mark_inbox(item["id"], "notified", sink_results=results)
        count += 1
    return count
