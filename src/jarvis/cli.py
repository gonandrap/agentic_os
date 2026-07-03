"""jarvis — the agentic OS command line.

Grouped commands:
  jarvis start|stop|status|adopt          OS lifecycle
  jarvis wo create|list|show|send|ask|assume|finish|review|cancel
  jarvis neo list|show|review|answer|learnings|learn
  jarvis backlog add|list|promote|done
  jarvis learn add|list|search
  jarvis notify / jarvis inbox
  jarvis ui                               web dashboard
  jarvis daemon run                       (internal) foreground daemon
  jarvis _hook                            (internal) Claude Code hook handler
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        _pretty(data)


def _pretty(data: Any, indent: int = 0) -> None:
    pad = "  " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _pretty(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _pretty(item, indent)
                print()
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{data}")


def _age(ts: float | None) -> str:
    if not ts:
        return "-"
    delta = time.time() - ts
    if delta < 90:
        return f"{int(delta)}s"
    if delta < 5400:
        return f"{int(delta / 60)}m"
    if delta < 129600:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.1f}d"


STATUS_ICON = {
    "pending": "⏳", "dispatching": "🚀", "running": "🟢", "waiting_input": "🙋",
    "needs_review": "👀", "completed": "✅", "failed": "❌", "cancelled": "🚫",
}
ORIGIN_BADGE = {"jarvis": "🤖 jarvis", "ui": "🖥 ui", "manual": "⚠ manual", "adhoc": "⚠ ad-hoc"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    # start / stop / status ------------------------------------------------------
    sp = sub.add_parser("start", help="start the OS: bootstrap projects + run the daemon")
    sp.add_argument("--catalog", required=True, help="path to the project catalog JSON")
    sp.add_argument("--force-config", action="store_true",
                    help="overwrite manually-edited injected settings")
    sp.add_argument("--foreground", action="store_true", help="run the daemon in-process")
    sp.add_argument("--poll-interval", type=float, default=5.0)

    sub.add_parser("stop", help="stop the daemon")

    sp = sub.add_parser("status", help="whole-OS status; flags what needs your attention")
    sp.add_argument("--attention", action="store_true", help="only show attention items")

    sp = sub.add_parser("adopt", help="make a project OS-ready (README, OPERATION.md, settings)")
    sp.add_argument("path", help="project directory")
    sp.add_argument("--name", help="project name (default: directory name)")
    sp.add_argument("--catalog", help="catalog to take overrides/defaults from")
    sp.add_argument("--force-config", action="store_true")
    sp.add_argument("--dry-run", action="store_true")

    # work orders -------------------------------------------------------------------
    wo = sub.add_parser("wo", help="work orders").add_subparsers(dest="wo_cmd", required=True)

    c = wo.add_parser("create", help="create a work order")
    c.add_argument("project")
    c.add_argument("title")
    c.add_argument("--description", "-d", default="")
    c.add_argument("--model")
    c.add_argument("--effort")
    c.add_argument("--permission-mode")
    c.add_argument("--append-system-prompt")
    c.add_argument("--origin", default="jarvis", choices=["jarvis", "ui", "manual"])

    l = wo.add_parser("list", help="list work orders")
    l.add_argument("project", nargs="?", help="restrict to one project")
    l.add_argument("--all", action="store_true", help="include closed work orders")

    s = wo.add_parser("show", help="show one work order with events/messages/assumptions")
    s.add_argument("wo_id")
    s.add_argument("--project")

    m = wo.add_parser("send", help="send feedback to the worker handling a work order")
    m.add_argument("wo_id")
    m.add_argument("message")
    m.add_argument("--project")
    m.add_argument("--source", default="jarvis", choices=["jarvis", "ui", "direct"])

    a = wo.add_parser("assume", help="(workers) record an assumption for user review")
    a.add_argument("wo_id")
    a.add_argument("content")

    q = wo.add_parser("ask", help="(workers) ask a question — Neo answers as the user")
    q.add_argument("wo_id")
    q.add_argument("question")
    q.add_argument("--project")

    f = wo.add_parser("finish", help="(workers) mark a work order finished")
    f.add_argument("wo_id")
    f.add_argument("--summary", required=True)

    r = wo.add_parser("review", help="accept/reject a work order's pending assumptions")
    r.add_argument("wo_id")
    r.add_argument("--reject", action="store_true")

    x = wo.add_parser("cancel", help="cancel a work order")
    x.add_argument("wo_id")

    # backlog ---------------------------------------------------------------------------
    bl = sub.add_parser("backlog", help="unified deferred-work backlog").add_subparsers(
        dest="bl_cmd", required=True)
    b = bl.add_parser("add")
    b.add_argument("project")
    b.add_argument("title")
    b.add_argument("--description", "-d", default="")
    b.add_argument("--depends-on", default="", help="comma-separated backlog ids")
    b = bl.add_parser("list")
    b.add_argument("project", nargs="?")
    b.add_argument("--all", action="store_true", help="include non-open items")
    b = bl.add_parser("promote", help="turn a backlog item into a work order")
    b.add_argument("item_id")
    b.add_argument("--force", action="store_true", help="ignore unfinished dependencies")
    b = bl.add_parser("done", help="mark a backlog item done without a work order")
    b.add_argument("item_id")

    # knowledge -----------------------------------------------------------------------------
    kn = sub.add_parser("learn", help="central knowledge base").add_subparsers(
        dest="kn_cmd", required=True)
    k = kn.add_parser("add")
    k.add_argument("content")
    k.add_argument("--project", default="", help="omit for a global learning")
    k.add_argument("--topic", default="")
    k.add_argument("--tags", default="")
    k = kn.add_parser("list")
    k.add_argument("--project")
    k = kn.add_parser("search")
    k.add_argument("term")

    # neo -----------------------------------------------------------------------------------
    ne = sub.add_parser("neo", help="Neo: the OS answerer agent (answers workers as you)"
                        ).add_subparsers(dest="neo_cmd", required=True)
    n = ne.add_parser("list", help="questions Neo has handled or is handling")
    n.add_argument("--all", action="store_true", help="include reviewed items")
    n = ne.add_parser("show", help="one question with Neo's full answer")
    n.add_argument("question_id", type=int)
    n = ne.add_parser("review", help="approve or correct one of Neo's answers")
    n.add_argument("question_id", type=int)
    n.add_argument("--correct", metavar="FEEDBACK",
                   help="reject the answer and teach Neo what you would have said")
    n = ne.add_parser("answer", help="answer a question Neo escalated to you")
    n.add_argument("question_id", type=int)
    n.add_argument("text")
    n = ne.add_parser("learnings", help="what Neo has learned from your reviews")
    n.add_argument("--project", default="")
    n = ne.add_parser("learn", help="teach Neo directly (no question needed)")
    n.add_argument("content")
    n.add_argument("--project", default="")

    # notifications ----------------------------------------------------------------------------
    n = sub.add_parser("notify", help="emit a notification into the OS pipeline")
    n.add_argument("title")
    n.add_argument("body", nargs="?", default="")
    n.add_argument("--project", help="project name (auto-detected from cwd when omitted)")
    n.add_argument("--level", default="info", choices=["info", "warning", "critical"])
    n.add_argument("--wo-id")

    ib = sub.add_parser("inbox", help="central notification inbox").add_subparsers(
        dest="ib_cmd", required=False)
    ib.add_parser("list")
    i = ib.add_parser("ack")
    i.add_argument("inbox_id", nargs="?", type=int, help="omit to ack everything")

    # ui / daemon / hook ---------------------------------------------------------------------------
    u = sub.add_parser("ui", help="run the web dashboard")
    u.add_argument("--port", type=int)
    u.add_argument("--host", default="127.0.0.1")

    d = sub.add_parser("daemon", help="daemon control (internal)").add_subparsers(
        dest="d_cmd", required=True)
    dr = d.add_parser("run")
    dr.add_argument("--catalog", required=True)
    dr.add_argument("--poll-interval", type=float, default=5.0)

    sub.add_parser("_hook", help=argparse.SUPPRESS)
    return p


# -- command implementations ----------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    from . import ops
    result = ops.start_os(args.catalog, force_config=args.force_config,
                          foreground=args.foreground, poll_interval=args.poll_interval)
    if args.json:
        _print(result, True)
    else:
        for pr in result["projects"]:
            print(f"• {pr['name']}")
            for a in pr["actions"]:
                print(f"    {a}")
            for w in pr["warnings"]:
                print(f"    ⚠ {w}")
        d = result["daemon"]
        print(f"daemon: {d['status']}" + (f" (pid {d.get('pid')})" if d.get("pid") else ""))
    if args.foreground:
        from .daemon import run_daemon
        run_daemon(args.catalog, poll_interval=args.poll_interval, log_to_file=True)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    from . import ops
    _print(ops.stop_os(), args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import ops
    st = ops.os_status()
    if args.json:
        _print(st, True)
        return 0
    d = st["daemon"]
    print(f"Jarvis {'🟢 running' if d['running'] else '🔴 daemon stopped'}"
          + (f" (pid {d['pid']})" if d["pid"] else ""))
    if st["attention"]:
        print(f"\n⚠ NEEDS YOUR ATTENTION ({len(st['attention'])}):")
        for a in st["attention"]:
            wo = f" {a['wo_id']}" if a["wo_id"] else ""
            print(f"  • [{a['project']}]{wo} {a['title']} — {a['reason']}")
    if st["inbox"]["unacked"]:
        print(f"\n📥 inbox: {st['inbox']['unacked']} unacked"
              f" ({st['inbox']['critical']} critical) — `jarvis inbox list`")
    neo = st.get("neo", {})
    if neo.get("queued") or neo.get("unreviewed") or neo.get("escalated"):
        print(f"\n🕶 neo: {neo.get('queued', 0)} queued, "
              f"{neo.get('escalated', 0)} escalated to you, "
              f"{neo.get('unreviewed', 0)} answers awaiting your review — `jarvis neo list`")
    if args.attention:
        return 0
    print()
    for p in st["projects"]:
        counts = ", ".join(f"{k}:{v}" for k, v in p.get("summary", {}).get("by_status", {}).items())
        drift = " ⚠ settings drift" if p.get("settings_drift") else ""
        print(f"• {p['name']} — {counts or 'no work orders'}{drift}")
        for wo in p.get("open_work_orders", []):
            icon = STATUS_ICON.get(wo["status"], "•")
            badge = ORIGIN_BADGE.get(wo["origin"], wo["origin"])
            att = f"  ⚠ {wo['attention_reason']}" if wo["needs_attention"] else ""
            print(f"    {icon} {wo['id']} [{badge}] {wo['title']} ({wo['status']}){att}")
    if st["backlog"]["open"]:
        print(f"\n🗂 backlog: {st['backlog']['open']} open items — `jarvis backlog list`")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    from .bootstrap import bootstrap_project
    from .catalog import ProjectSpec, WorkerDefaults, load_catalog
    path = Path(args.path).expanduser().resolve()
    name = args.name or path.name
    spec = None
    if args.catalog:
        catalog = load_catalog(args.catalog)
        for pr in catalog.projects:
            if pr.name == name or pr.path == path:
                spec = pr
                break
    if spec is None:
        spec = ProjectSpec(name=name, path=path, worker=WorkerDefaults())
    report = bootstrap_project(spec, force_config=args.force_config, dry_run=args.dry_run)
    _print({"project": report.project, "actions": report.actions,
            "warnings": report.warnings}, args.json)
    return 0 if not report.warnings else 1


def cmd_wo(args: argparse.Namespace) -> int:
    from . import ops
    from .project_store import OPEN_STATUSES, ProjectStore

    if args.wo_cmd == "create":
        wo = ops.create_work_order(
            args.project, args.title, description=args.description, origin=args.origin,
            model=args.model, effort=args.effort, permission_mode=args.permission_mode,
            append_system_prompt=args.append_system_prompt,
        )
        _print({"created": wo["id"], "project": args.project, "status": wo["status"],
                "note": "jarvisd will dispatch it shortly"}, args.json)

    elif args.wo_cmd == "list":
        paths = ops.registered_project_paths()
        if args.project:
            if args.project not in paths:
                raise ops.OpsError(f"project {args.project!r} not registered")
            paths = {args.project: paths[args.project]}
        out = []
        for name, path in sorted(paths.items()):
            if not path.is_dir():
                continue
            store = ProjectStore(path)
            try:
                wos = store.list_work_orders(statuses=None if args.all else OPEN_STATUSES)
            finally:
                store.close()
            for wo in wos:
                out.append({"project": name, **{k: wo[k] for k in (
                    "id", "title", "status", "origin", "needs_attention",
                    "attention_reason", "created_at")}})
        if args.json:
            _print(out, True)
        else:
            for wo in out:
                icon = STATUS_ICON.get(wo["status"], "•")
                badge = ORIGIN_BADGE.get(wo["origin"], wo["origin"])
                att = " ⚠" if wo["needs_attention"] else ""
                print(f"{icon} {wo['id']} [{wo['project']}] [{badge}] "
                      f"{wo['title']} ({wo['status']}, {_age(wo['created_at'])}){att}")
            if not out:
                print("no work orders")

    elif args.wo_cmd == "show":
        name, path, wo = ops.find_work_order(args.wo_id, args.project)
        store = ProjectStore(path)
        try:
            detail = {
                "project": name, **wo,
                "events": store.list_events(args.wo_id),
                "messages": store.list_messages(args.wo_id),
                "assumptions": store.pending_assumptions(args.wo_id),
            }
        finally:
            store.close()
        _print(detail, args.json)

    elif args.wo_cmd == "send":
        _print(ops.send_message(args.wo_id, args.message, source=args.source,
                                project_name=args.project), args.json)
    elif args.wo_cmd == "assume":
        _print(ops.assume(args.wo_id, args.content), args.json)
    elif args.wo_cmd == "ask":
        _print(ops.ask_question(args.wo_id, args.question,
                                project_name=args.project), args.json)
    elif args.wo_cmd == "finish":
        _print(ops.finish(args.wo_id, args.summary), args.json)
    elif args.wo_cmd == "review":
        _print(ops.review_work_order(args.wo_id, accept=not args.reject), args.json)
    elif args.wo_cmd == "cancel":
        _print(ops.cancel(args.wo_id), args.json)
    return 0


def cmd_backlog(args: argparse.Namespace) -> int:
    from . import ops
    from .central_store import CentralStore
    central = CentralStore()
    try:
        if args.bl_cmd == "add":
            deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
            item = central.add_backlog(args.project, args.title,
                                       description=args.description, depends_on=deps)
            _print(item, args.json)
        elif args.bl_cmd == "list":
            items = central.list_backlog(project=args.project,
                                         status=None if args.all else "open")
            if args.json:
                _print(items, True)
            else:
                for it in items:
                    deps = f" (deps: {', '.join(it['depends_on'])})" if it["depends_on"] else ""
                    print(f"• {it['id']} [{it['project']}] {it['title']} "
                          f"({it['status']}){deps}")
                if not items:
                    print("backlog empty")
        elif args.bl_cmd == "promote":
            _print(ops.promote_backlog(args.item_id, force=args.force), args.json)
        elif args.bl_cmd == "done":
            central.mark_backlog(args.item_id, "done")
            _print({"item": args.item_id, "status": "done"}, args.json)
    finally:
        central.close()
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    from .central_store import CentralStore
    central = CentralStore()
    try:
        if args.kn_cmd == "add":
            _print(central.add_knowledge(args.content, project=args.project,
                                         topic=args.topic, tags=args.tags), args.json)
        elif args.kn_cmd == "list":
            rows = (central.relevant_knowledge(args.project, limit=100)
                    if args.project else central.search_knowledge("", limit=100))
            _print(rows, args.json)
        elif args.kn_cmd == "search":
            _print(central.search_knowledge(args.term), args.json)
    finally:
        central.close()
    return 0


def cmd_neo(args: argparse.Namespace) -> int:
    from . import ops
    from .neo_store import NeoStore

    if args.neo_cmd == "list":
        neo = NeoStore()
        try:
            qs = neo.list_questions()
        finally:
            neo.close()
        if not args.all:
            qs = [q for q in qs
                  if q["status"] in ("queued", "answering", "escalated", "failed")
                  or (q["status"] == "answered" and q["review_status"] == "unreviewed")]
        if args.json:
            _print(qs, True)
        else:
            icon = {"queued": "⏳", "answering": "🤔", "answered": "💬",
                    "escalated": "🙋", "failed": "❌"}
            for q in qs:
                review = f" [{q['review_status']}]" if q["status"] == "answered" else ""
                print(f"{icon.get(q['status'], '•')} #{q['id']} [{q['project']}] "
                      f"{q['wo_id']} ({q['status']}{review}, {_age(q['ts'])}) "
                      f"{q['question'][:80]}")
            if not qs:
                print("nothing pending for Neo ✨")
    elif args.neo_cmd == "show":
        neo = NeoStore()
        try:
            q = neo.get(args.question_id)
        finally:
            neo.close()
        if q is None:
            print(f"error: neo question {args.question_id} not found", file=sys.stderr)
            return 1
        _print(q, args.json)
    elif args.neo_cmd == "review":
        _print(ops.neo_review(args.question_id, approved=args.correct is None,
                              feedback=args.correct or ""), args.json)
    elif args.neo_cmd == "answer":
        _print(ops.neo_answer_escalated(args.question_id, args.text), args.json)
    elif args.neo_cmd == "learnings":
        neo = NeoStore()
        try:
            rows = neo.learnings(args.project, limit=200)
        finally:
            neo.close()
        if args.json:
            _print(rows, True)
        else:
            for r in rows:
                scope = r["project"] or "global"
                print(f"• [{scope}] ({r['source']}) {r['content']}")
            if not rows:
                print("Neo has no learnings yet — review its answers to teach it")
    elif args.neo_cmd == "learn":
        neo = NeoStore()
        try:
            row = neo.add_learning(args.content, project=args.project, source="manual")
        finally:
            neo.close()
        _print({"learned": row["id"], "project": args.project or "global"}, args.json)
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    """Write to the project outbox (daemon routes it), falling back to the central
    inbox when the project isn't identifiable."""
    import os
    from .central_store import CentralStore
    from .hooks import find_project_root
    from .project_store import ProjectStore

    project = args.project or os.environ.get("JARVIS_PROJECT")
    root = None
    if project:
        from .ops import registered_project_paths
        root = registered_project_paths().get(project)
    if root is None:
        root = find_project_root(Path.cwd())
        if root is not None and not project:
            project = root.name
    if root is not None and (root / ".jarvis").is_dir():
        store = ProjectStore(root)
        try:
            store.add_notification(args.title, args.body, level=args.level,
                                   wo_id=args.wo_id,
                                   source=os.environ.get("JARVIS_WO_ID", "cli"))
        finally:
            store.close()
        _print({"queued": True, "project": project, "via": "project outbox"}, args.json)
    else:
        central = CentralStore()
        try:
            central.add_inbox(project or "unknown", args.title, body=args.body,
                              level=args.level, wo_id=args.wo_id)
        finally:
            central.close()
        _print({"queued": True, "project": project or "unknown",
                "via": "central inbox"}, args.json)
    return 0


def cmd_inbox(args: argparse.Namespace) -> int:
    from .central_store import CentralStore
    central = CentralStore()
    try:
        if args.ib_cmd == "ack":
            n = central.ack_inbox(args.inbox_id)
            _print({"acked": n}, args.json)
        else:
            items = central.unacked_inbox()
            if args.json:
                _print(items, True)
            else:
                for it in items:
                    print(f"[{it['id']}] {it['level'].upper()} [{it['project']}] "
                          f"{it['title']} ({_age(it['ts'])})"
                          + (f" — {it['body']}" if it["body"] else ""))
                if not items:
                    print("inbox empty ✨")
    finally:
        central.close()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("the web UI needs extras: pip install 'jarvis-os[ui]'", file=sys.stderr)
        return 1
    from . import ops
    from .ui.app import create_app
    port = args.port
    if port is None:
        try:
            port = ops.resolve_catalog().os.ui_port
        except Exception:  # noqa: BLE001
            port = 8787
    uvicorn.run(create_app(), host=args.host, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # accept --json anywhere, not only before the subcommand
    as_json = "--json" in argv
    args = build_parser().parse_args([a for a in argv if a != "--json"])
    args.json = as_json
    if args.cmd == "_hook":
        from .hooks import main_hook
        return main_hook()
    from .catalog import CatalogError
    from .ops import OpsError
    try:
        if args.cmd == "start":
            return cmd_start(args)
        if args.cmd == "stop":
            return cmd_stop(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "adopt":
            return cmd_adopt(args)
        if args.cmd == "wo":
            return cmd_wo(args)
        if args.cmd == "backlog":
            return cmd_backlog(args)
        if args.cmd == "learn":
            return cmd_learn(args)
        if args.cmd == "neo":
            return cmd_neo(args)
        if args.cmd == "notify":
            return cmd_notify(args)
        if args.cmd == "inbox":
            return cmd_inbox(args)
        if args.cmd == "ui":
            return cmd_ui(args)
        if args.cmd == "daemon" and args.d_cmd == "run":
            from .daemon import run_daemon
            run_daemon(args.catalog, poll_interval=args.poll_interval)
            return 0
    except (OpsError, CatalogError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
