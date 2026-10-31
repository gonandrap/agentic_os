"""Notification routing: project outboxes -> central inbox -> sinks"""

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
from .project_store import ProjectStore, is_feature_order_id, order_path

LEVEL_EMOJI = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}

#: Kill switch for every sink whose effect leaves this process
DISABLE_EXTERNAL_SINKS_ENV = "JARVIS_DISABLE_EXTERNAL_SINKS"


def external_sinks_disabled() -> bool:
    return bool(os.environ.get(DISABLE_EXTERNAL_SINKS_ENV))

#: Anchor on the work-order page marking whatever is waiting on the user (pending assumptions
PENDING_ANCHOR = "pending"


def ui_base_url(catalog: Catalog) -> str:
    """Root URL of the local dashboard, as a notification recipient should reach it"""
    return catalog.os.ui_base_url or f"http://127.0.0.1:{catalog.os.ui_port}"


def wo_url(catalog: Catalog, project: str, wo_id: str) -> str:
    """Deep link to a work order's history, scrolled to what needs the user"""
    quote = urllib.parse.quote
    return f"{ui_base_url(catalog)}/wo/{quote(project)}/{quote(wo_id)}#{PENDING_ANCHOR}"


def neo_question_url(catalog: Catalog, question_id: Any) -> str:
    """Deep link to one Neo question — its verdict, its reasoning, its answer box"""
    return f"{ui_base_url(catalog)}/neo/question/{urllib.parse.quote(str(question_id))}"


def check_wo_link(central: CentralStore, catalog: Catalog, project: str,
                  wo_id: str) -> tuple[str | None, str]:
    """`(url, problem)` — the deep link, or `None` and why it would dead-end"""
    paths = {p["name"]: Path(p["path"]) for p in central.list_projects()
             if p["status"] == "active"}
    if project not in paths:
        return None, (f"project {project!r} is not registered with this Jarvis "
                      f"(known: {', '.join(sorted(paths)) or 'none'})")
    path = paths[project]
    if not path.is_dir():
        return None, f"project {project!r} has no directory at {path}"
    # A decision order's notification names the order itself (decision-orders spec §7)
    is_fo = is_feature_order_id(wo_id)
    store = ProjectStore(path)
    try:
        if is_fo:
            store.get_feature_order(wo_id)
        else:
            store.get_work_order(wo_id)
    except KeyError:
        return None, f"{'order' if is_fo else 'work order'} {wo_id} does not exist in {project!r}"
    finally:
        store.close()
    if is_fo:
        return ui_base_url(catalog) + urllib.parse.quote(order_path(project, wo_id)), ""
    return wo_url(catalog, project, wo_id), ""


def _link_for(item: dict[str, Any], catalog: Catalog) -> tuple[str | None, str]:
    """The link a sink should use for `item`"""
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
            # On disk too, so a dead link is diagnosable after the fact rather than only visible to whoever...
            + (f" [no deep link: {problem}]" if problem else "")
            + "\n"
        )
    return "ok"


def render_telegram(item: dict[str, Any], catalog: Catalog) -> str:
    """The message body exactly as Telegram will receive it"""
    emoji = LEVEL_EMOJI.get(item["level"], "")
    esc = html.escape
    # HTML (not Markdown): work order ids become tappable links into the local UI
    text = f"{emoji} <b>[{esc(item['project'])}]</b> {esc(item['title'])}"
    if item["body"]:
        text += f"\n{esc(item['body'])}"
    if item.get("wo_id"):
        url, problem = _link_for(item, catalog)
        if url:
            text += f'\n<a href="{esc(url, quote=True)}">{esc(item["wo_id"])}</a>'
        else:
            # No link rather than a link that 404s: the user cannot tell a stale link from a broken dashboard
            text += f"\n{esc(item['wo_id'])} — <i>{esc(problem)}</i>"
    return text


def sink_telegram(item: dict[str, Any], catalog: Catalog) -> str:
    if external_sinks_disabled():
        return f"skipped: {DISABLE_EXTERNAL_SINKS_ENV} set"
    token = os.environ.get(catalog.os.telegram_token_env, "")
    chat_id = os.environ.get(catalog.os.telegram_chat_id_env, "")
    if not token or not chat_id:
        return f"skipped: {catalog.os.telegram_token_env}/{catalog.os.telegram_chat_id_env} not set"
    text = render_telegram(item, catalog)
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


#: AppleScript reading title and body from argv, so neither needs quoting into the script
_OSASCRIPT = ["-e", "on run argv", "-e",
              "display notification (item 2 of argv) with title (item 1 of argv)",
              "-e", "end run"]


def desktop_argv(title: str, body: str) -> list[str] | None:
    """`notify-send` on Linux, `osascript` on a Mac, `None` when neither is installed"""
    if shutil.which("notify-send"):
        return ["notify-send", title, body]
    if shutil.which("osascript"):
        return ["osascript", *_OSASCRIPT, title, body]
    return None


def sink_desktop(item: dict[str, Any], catalog: Catalog) -> str:
    if external_sinks_disabled():
        return f"skipped: {DISABLE_EXTERNAL_SINKS_ENV} set"
    argv = desktop_argv(f"Jarvis [{item['project']}]", f"{item['title']}\n{item['body']}")
    if argv is None:
        return "skipped: neither notify-send nor osascript is available"
    try:
        subprocess.run(argv, timeout=10, check=False)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


SINKS: dict[str, Callable[[dict[str, Any], Catalog], str]] = {
    "log": sink_log,
    "telegram": sink_telegram,
    "desktop": sink_desktop,
}


def route_new_inbox(central: CentralStore, catalog: Catalog) -> int:
    """Send every 'new' inbox item through the configured sinks"""
    sinks = list(dict.fromkeys(["log", *catalog.os.notification_sinks]))
    count = 0
    for item in central.new_inbox():
        # Validate the deep link once, here
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
