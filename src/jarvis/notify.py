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
from pathlib import Path
from typing import Any, Callable

from .catalog import Catalog
from .central_store import CentralStore
from .paths import logs_dir
from .project_store import ProjectStore

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


def check_wo_link(central: CentralStore, catalog: Catalog, project: str,
                  wo_id: str) -> tuple[str | None, str]:
    """`(url, problem)` — the deep link, or `None` and why it would dead-end.

    A notification is often the user's only entry point into a work order, and until
    this the link was built from whatever project name the emitter happened to pass:
    no check that the project was registered or that the work order existed. Shipping
    a link that is *guaranteed* to 404 is worse than shipping no link, because the user
    cannot tell a stale link from a broken OS — the observed case was a test-fixture
    project name reaching the real Telegram sink and the user following it into an
    HTTP 500.

    Checked against the same source `ops.find_work_order` uses (the registry, then the
    project's own store), so agreeing with it is not a coincidence. `central` is passed
    in rather than opened here to keep this honest under an isolated `$JARVIS_HOME`.
    """
    paths = {p["name"]: Path(p["path"]) for p in central.list_projects()
             if p["status"] == "active"}
    if project not in paths:
        return None, (f"project {project!r} is not registered with this Jarvis "
                      f"(known: {', '.join(sorted(paths)) or 'none'})")
    path = paths[project]
    if not path.is_dir():
        return None, f"project {project!r} has no directory at {path}"
    store = ProjectStore(path)
    try:
        store.get_work_order(wo_id)
    except KeyError:
        return None, f"work order {wo_id} does not exist in {project!r}"
    finally:
        store.close()
    return wo_url(catalog, project, wo_id), ""


def _link_for(item: dict[str, Any], catalog: Catalog) -> tuple[str | None, str]:
    """The link a sink should use for `item`.

    `route_new_inbox` validates once per item and stashes the verdict; a sink called
    directly (tests, one-off scripts) has no verdict to read and falls back to the
    unvalidated URL — validation needs a `CentralStore`, and a sink must not go opening
    one behind its caller's back.
    """
    if not item.get("wo_id"):
        return None, ""
    if "wo_link" in item:
        return item["wo_link"], item.get("wo_link_problem", "")
    return wo_url(catalog, item["project"], item["wo_id"]), ""


def sink_log(item: dict[str, Any], catalog: Catalog) -> str:
    logs_dir().mkdir(parents=True, exist_ok=True)
    path = logs_dir() / "notifications.log"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["ts"]))
    _, problem = _link_for(item, catalog)
    with path.open("a") as f:
        f.write(
            f"{stamp} [{item['level'].upper()}] {item['project']}: {item['title']}"
            + (f" — {item['body']}" if item["body"] else "")
            + (f" (wo={item['wo_id']})" if item.get("wo_id") else "")
            # On disk too, so a dead link is diagnosable after the fact rather than
            # only visible to whoever tapped it in Telegram.
            + (f" [no deep link: {problem}]" if problem else "")
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
        url, problem = _link_for(item, catalog)
        if url:
            text += f'\n<a href="{esc(url, quote=True)}">{esc(item["wo_id"])}</a>'
        else:
            # No link rather than a link that 404s: the user cannot tell a stale link
            # from a broken dashboard, and guessing wrong costs them a debugging session.
            text += f"\n{esc(item['wo_id'])} — <i>{esc(problem)}</i>"
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
        # Validate the deep link once, here, rather than in each sink: this is the last
        # point at which the OS still owns the notification, and every sink downstream
        # then agrees on whether there is a page to link to.
        if item.get("wo_id"):
            item["wo_link"], item["wo_link_problem"] = check_wo_link(
                central, catalog, item["project"], item["wo_id"])
        results = {}
        for name in sinks:
            fn = SINKS.get(name)
            results[name] = fn(item, catalog) if fn else f"unknown sink {name!r}"
        central.mark_inbox(item["id"], "notified", sink_results=results)
        count += 1
    return count
