"""jarvis — the agentic OS command line.

Grouped commands:
  jarvis start|stop|status|adopt          OS lifecycle
  jarvis wo create|list|show|send|ask|assume|finish|review|cancel|done|inject
  jarvis fo create|list|show|plan|approve|cancel        feature orders (planned sets)
  jarvis gate request|list|show|approve|deny|dismiss   privileged-action approvals
  jarvis neo list|show|review|answer|learnings|learn
  jarvis backlog add|list|promote|done
  jarvis learn add|list|search
  jarvis notify / jarvis inbox
  jarvis bug report                       file a Jarvis OS bug (GitHub issue + ping)
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
    "needs_review": "👀", "waiting_pr_merge": "🔀", "completed": "✅", "failed": "❌",
    "cancelled": "🚫",
}
# What the user is most likely to act on, listed first: work in flight, then the pull
# requests waiting for them to merge. Everything else keeps its recency order below.
# The dashboard groups by the same rule (see ui/app.py FEATURED_STATUSES).
LIST_PRIORITY = {"running": 0, "waiting_pr_merge": 1}
ORIGIN_BADGE = {"jarvis": "🤖 jarvis", "ui": "🖥 ui", "manual": "⚠ manual",
                "adhoc": "⚠ ad-hoc", "injected": "🔗 injected", "neo": "🧠 neo"}


class _VersionAction(argparse.Action):
    """`jarvis --version` — resolved lazily so hooks don't pay for dist metadata."""

    def __call__(self, parser, _namespace=None, _values=None, _option_string=None):  # noqa: ANN001
        from .bugreport import jarvis_version
        print(f"jarvis {jarvis_version()}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jarvis", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    # Which release am I on? install.sh checks this against the tag it installed.
    p.add_argument("-V", "--version", nargs=0, action=_VersionAction,
                   help="print the installed Jarvis OS version")
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

    sp = sub.add_parser("doctor", help="check the OS's own post-conditions (invariants)")
    sp.add_argument("project", nargs="?", help="one project (default: the whole fleet)")
    sp.add_argument("--repair", action="store_true",
                    help="apply the repairs instead of only reporting them")
    sp.add_argument("--catalog", help="catalog to read the fleet from")
    sp.add_argument("--json", action="store_true")

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
    c.add_argument("--depends-on", default="",
                   help="comma-separated work order ids that must COMPLETE before this "
                        "one is dispatched (same project only)")

    ub = wo.add_parser("unblock", help="cut the dependency edges holding a work order "
                                       "back (by default only the ones that can never "
                                       "clear)")
    ub.add_argument("wo_id")
    ub.add_argument("--project")
    ub.add_argument("--all", action="store_true", dest="drop_all",
                    help="cut every remaining edge, including dependencies still running")

    l = wo.add_parser("list", help="list work orders")
    l.add_argument("project", nargs="?", help="restrict to one project")
    l.add_argument("--all", action="store_true", help="include closed work orders")
    l.add_argument("--include-hidden", action="store_true",
                   help="include work orders you've hidden")

    s = wo.add_parser("show", help="show one work order with its timeline, messages "
                                   "and assumptions")
    s.add_argument("wo_id")
    s.add_argument("--project")
    s.add_argument("--debug", action="store_true",
                   help="include plumbing entries (message delivery, session hooks)")

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
    f.add_argument("--pr", default="", metavar="URL",
                   help="the pull request this work order opened — parks it in "
                        "'waiting for PR merge' instead of completing it, so it stays "
                        "on the open list until the user merges")

    r = wo.add_parser("review", help="accept/reject a work order's pending assumptions")
    r.add_argument("wo_id")
    r.add_argument("--reject", action="store_true")
    r.add_argument("--feedback", default="",
                   help="your reasoning — teaches Neo, and on --reject is delivered "
                        "to the worker as guidance")

    x = wo.add_parser("cancel", help="cancel a work order")
    x.add_argument("wo_id")

    dn = wo.add_parser("done", help="close a work order yourself — the work is "
                                    "finished; stops the worker if one is still running")
    dn.add_argument("wo_id")
    dn.add_argument("--project")

    ak = wo.add_parser("ack", help="acknowledge a work order's attention flag — it "
                                   "stops asking for you in status and on the dashboard")
    ak.add_argument("wo_id", nargs="?")
    ak.add_argument("--all", action="store_true",
                    help="acknowledge every flagged work order in the fleet")
    ak.add_argument("--project")

    h = wo.add_parser("hide", help="hide a work order from listings and the attention "
                                   "list (nothing is deleted)")
    h.add_argument("wo_id")
    h.add_argument("--project")

    uh = wo.add_parser("unhide", help="bring a hidden work order back")
    uh.add_argument("wo_id")
    uh.add_argument("--project")

    dl = wo.add_parser("delete", help="permanently delete a work order and its whole "
                                      "history — this cannot be undone")
    dl.add_argument("wo_id")
    dl.add_argument("--project")
    dl.add_argument("--yes", "-y", action="store_true",
                    help="confirm the deletion (required)")

    ij = wo.add_parser("inject", help="hand a Claude session you started over to "
                                      "Jarvis, as a work order")
    ij.add_argument("session_id", help="from `claude agents` (its session id or agent id)")
    ij.add_argument("--project", help="which project it belongs to (default: whichever "
                                      "one the session's directory is inside)")
    ij.add_argument("--title", help="work order title (default: the session's name)")

    ra = wo.add_parser("resume-auto",
                       help="unstick a worker blocked on a permission prompt: flip it "
                            "to auto mode and resume it")
    ra.add_argument("wo_id")
    ra.add_argument("--project")

    # feature orders -------------------------------------------------------------------
    # Parallel to `wo` on purpose: a user who knows the work-order surface should not
    # have to learn a second grammar to use the one above it.
    fo = sub.add_parser(
        "fo",
        help="feature orders: a coarse ask the project plans into work orders itself",
    ).add_subparsers(dest="fo_cmd", required=True)

    f = fo.add_parser("create", help="file a feature order — the OS plans it into work "
                                     "orders and dispatches them in dependency order")
    f.add_argument("project")
    f.add_argument("title")
    f.add_argument("--description", "-d", default="",
                   help="the whole ask. Required: the planner sees only this text")
    f.add_argument("--origin", default="jarvis", choices=["jarvis", "ui", "manual"])
    f.add_argument("--max-parallel", dest="max_parallel", type=int, metavar="N",
                   help="run at most N of this feature's children at once. Omit for no "
                        "cap beyond the project's own max_concurrent — which is the "
                        "right answer unless the children contend for something the OS "
                        "cannot see (a test database, a rate-limited API, review "
                        "attention)")

    f = fo.add_parser("list", help="feature orders and how far along they are")
    f.add_argument("project", nargs="?")
    f.add_argument("--all", action="store_true", help="include settled feature orders")

    f = fo.add_parser("show", help="one feature order: its plan and its children")
    f.add_argument("fo_id")
    f.add_argument("--project")

    f = fo.add_parser("plan", help="(planners) submit the plan — the planner's terminal "
                                   "action, and what settles its work order")
    f.add_argument("fo_id")
    f.add_argument("--from-file", required=True, dest="from_file", metavar="PATH",
                   help="the plan, as JSON. A file rather than an argument on purpose: "
                        "a plan is a long string full of repo paths, which is exactly "
                        "what trips the privileged-action classifier")
    f.add_argument("--project")

    f = fo.add_parser("approve", help="release a submitted plan (or send it back), when "
                                      "Neo escalated the decision to you")
    f.add_argument("fo_id")
    f.add_argument("--reject", action="store_true")
    f.add_argument("--feedback", default="",
                   help="your reasoning — on --reject it reaches the planner as guidance "
                        "and it revises in its existing session")
    f.add_argument("--project")

    f = fo.add_parser("cancel", help="stop a feature order and everything it has running")
    f.add_argument("fo_id")
    f.add_argument("--project")

    # gates (privileged-action approvals) ------------------------------------------------
    ga = sub.add_parser(
        "gate",
        help="privileged-action approvals: shipping actions a worker may attempt only "
             "with an independent review",
    ).add_subparsers(dest="ga_cmd", required=True)
    g = ga.add_parser("request",
                      help="(workers) ask permission to run a privileged command")
    g.add_argument("wo_id")
    g.add_argument("command", help="the EXACT command you will run if approved")
    g.add_argument("--why", default="", help="why this is ready to ship")
    g.add_argument("--evidence", default="",
                   help="PR number, test results, checks — the reviewer sees only this")
    g.add_argument("--project")
    g = ga.add_parser("list", help="approval requests, newest first")
    g.add_argument("--project")
    g.add_argument("--wo")
    g.add_argument("--pending", action="store_true", help="only requests awaiting a verdict")
    g = ga.add_parser("show", help="one request in full, as the reviewer saw it")
    g.add_argument("approval_id", type=int)
    g.add_argument("--project")
    g = ga.add_parser("approve", help="open a gate: the worker may run the command")
    g.add_argument("approval_id", type=int)
    g.add_argument("--reason", default="", help="recorded, and shown to the worker")
    g.add_argument("--project")
    g = ga.add_parser("deny", help="refuse a gate, with a reason the worker can act on")
    g.add_argument("approval_id", type=int)
    g.add_argument("--reason", required=True)
    g.add_argument("--project")
    g = ga.add_parser(
        "dismiss",
        help="not a gated action at all: the recogniser matched a command that performs "
             "no privileged action. Unblocks it without recording an authorisation",
    )
    g.add_argument("approval_id", type=int)
    g.add_argument("--reason", required=True,
                   help="what the recogniser got wrong — this is the defect report")
    g.add_argument("--project")

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
    b = bl.add_parser("promote", help="turn a backlog item into a work order (or, with "
                                      "--as feature, into a feature order the project "
                                      "plans for itself)")
    b.add_argument("item_id")
    b.add_argument("--as", dest="as_kind", default="work", choices=["work", "feature"],
                   help="`work` (default) dispatches one worker; `feature` opens a "
                        "planner that decomposes it first")
    b.add_argument("--force", action="store_true", help="ignore unfinished dependencies")
    b.add_argument("--max-parallel", dest="max_parallel", type=int, metavar="N",
                   help="with --as feature: cap how many of its children run at once")
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
    k = kn.add_parser("retract", help="retire a superseded entry (kept for the record, "
                                      "dropped from worker prompts)")
    k.add_argument("knowledge_id")
    k.add_argument("--reason", required=True,
                   help="what supersedes it — this is kept beside the entry for ever")

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
    n = ne.add_parser("retract", help="retire a superseded learning (kept for the "
                                      "record, dropped from Neo's prompt)")
    n.add_argument("learning_id", type=int)
    n.add_argument("--reason", required=True,
                   help="what supersedes it — this is kept beside the learning for ever")

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

    # bug reports ----------------------------------------------------------------
    bug = sub.add_parser(
        "bug", help="report a bug in Jarvis OS itself").add_subparsers(
        dest="bug_cmd", required=True)
    br = bug.add_parser("report", help="file a Jarvis OS bug as a GitHub issue")
    br.add_argument("title")
    br.add_argument("--description", "-d", required=True,
                    help="what happened, in Jarvis OS terms")
    br.add_argument("--expected", "-e", required=True, help="what you expected")
    br.add_argument("--actual", "-a", required=True, help="what you got instead")
    br.add_argument("--steps", default="", help="optional steps to reproduce")
    br.add_argument("--project", default="", help="reporting project (default: $JARVIS_PROJECT)")
    br.add_argument("--wo-id", default="", help="reporting work order (default: $JARVIS_WO_ID)")

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
            ident = a["wo_id"] or a.get("fo_id")
            print(f"  • [{a['project']}]{' ' + ident if ident else ''} {a['title']} "
                  f"— {a['reason']}")
            if a.get("attach"):
                print(f"      approve it: {a['attach']}  ·  or `jarvis wo resume-auto {a['wo_id']}`")
            if a.get("decide"):
                # A gate item carries the command to run AND the request to read; a
                # feature order's `decide` is already the whole instruction. Keyed on
                # `approval_id` rather than on the item's shape, because reading it off
                # every item is what made a feature order crash this line.
                see = (f"  ·  see it: `jarvis gate show {a['approval_id']}`"
                       if a.get("approval_id") else "")
                print(f"      {a['decide']}{see}")
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


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import ops
    res = ops.run_doctor(project=args.project, repair=args.repair,
                         catalog_path=args.catalog)
    if args.json:
        _print(res, True)
        return 1 if res["violations"] else 0
    orphans = res.get("orphaned_sessions") or []
    if not res["violations"]:
        print("✓ all OS invariants hold")
        _print_orphans(orphans)
        return 0
    verb = "repaired" if res["repair"] else "found (run with --repair to fix)"
    print(f"⚠ {res['violations']} invariant violation(s) {verb}:\n")
    for p in res["projects"]:
        if p.get("error"):
            print(f"• {p['project']}: {p['error']}")
        for v in p["violations"]:
            where = f" {v['wo_id']}" if v["wo_id"] else ""
            print(f"• [{p['project']}]{where} {v['invariant']}")
            print(f"    {v['detail']}")
            if v["repaired"]:
                print(f"    → {'fixed' if res['repair'] else 'would fix'}: {v['repair']}")
    _print_orphans(orphans)
    return 1


def _print_orphans(orphans: list[dict]) -> None:
    """Leftover background agents from the pre-headless-turn transport.

    Listed, never stopped for you: they live in your own agents view. Nothing creates
    them any more, so this list only shrinks.
    """
    if not orphans:
        return
    print(f"\nℹ {len(orphans)} leftover background session(s) with no open work order.")
    print("  These are debris from the old worker transport. Stop them when convenient:")
    for o in orphans:
        print(f"    {o['stop']}    # {o['name'][:60]} ({o['state']})")


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
    from . import invariants, ops
    from .project_store import OPEN_STATUSES, ProjectStore
    from .timeline import build_timeline

    if args.wo_cmd == "create":
        deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
        wo = ops.create_work_order(
            args.project, args.title, description=args.description, origin=args.origin,
            model=args.model, effort=args.effort, permission_mode=args.permission_mode,
            append_system_prompt=args.append_system_prompt, depends_on=deps,
        )
        _print({"created": wo["id"], "project": args.project, "status": wo["status"],
                "depends_on": deps,
                "note": (f"jarvisd will dispatch it once {', '.join(deps)} completes"
                         if deps else "jarvisd will dispatch it shortly")}, args.json)

    elif args.wo_cmd == "unblock":
        _print(ops.unblock_work_order(args.wo_id, drop_all=args.drop_all,
                                      project_name=args.project), args.json)

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
                wos = store.list_work_orders(
                    statuses=None if args.all else OPEN_STATUSES,
                    include_hidden=args.include_hidden,
                )
                # Derived inside the store's lifetime: the label reads the dependencies'
                # rows, so it cannot be computed after the connection is closed.
                labels = {wo["id"]: invariants.status_label(store, wo) for wo in wos}
            finally:
                store.close()
            for wo in wos:
                out.append({"project": name, **{k: wo[k] for k in (
                    "id", "title", "status", "origin", "needs_attention",
                    "attention_reason", "created_at", "hidden", "pr_url")},
                    "status_label": labels[wo["id"]]})
        # Running first, then the PRs waiting to be merged, then everything else as it
        # came (newest first, per project). `sorted` is stable, so the second key is
        # only a tie-break within a group.
        out.sort(key=lambda wo: LIST_PRIORITY.get(wo["status"], 9))
        if args.json:
            _print(out, True)
        else:
            for wo in out:
                icon = STATUS_ICON.get(wo["status"], "•")
                badge = ORIGIN_BADGE.get(wo["origin"], wo["origin"])
                att = " ⚠" if wo["needs_attention"] else ""
                hid = " 🙈" if wo["hidden"] else ""
                pr = f"\n    → merge {wo['pr_url']}" if wo["status"] == "waiting_pr_merge" \
                     and wo["pr_url"] else ""
                print(f"{icon} {wo['id']} [{wo['project']}] [{badge}] "
                      f"{wo['title']} ({wo['status_label']}, "
                      f"{_age(wo['created_at'])}){att}{hid}{pr}")
            if not out:
                print("no work orders")

    elif args.wo_cmd == "show":
        name, path, wo = ops.find_work_order(args.wo_id, args.project)
        store = ProjectStore(path)
        try:
            messages = store.list_messages(args.wo_id)
            detail = {
                "project": name, **wo,
                "status_label": invariants.status_label(store, wo),
                "blocked_by": store.unfinished_dependencies(args.wo_id),
                "timeline": build_timeline(wo, store.list_events(args.wo_id),
                                           messages, include_debug=args.debug,
                                           questions=ops.neo_question_texts(args.wo_id)),
                "messages": messages,
                "assumptions": store.pending_assumptions(args.wo_id),
                # What this work order was allowed (or refused) permission to ship.
                "gates": [
                    {k: a[k] for k in ("id", "kind", "command", "status", "escalated",
                                       "decided_by", "decision_reason")}
                    for a in store.list_approvals(args.wo_id)
                ],
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
        _print(ops.finish(args.wo_id, args.summary, pr_url=args.pr or None), args.json)
    elif args.wo_cmd == "review":
        _print(ops.review_work_order(args.wo_id, accept=not args.reject,
                                     feedback=args.feedback), args.json)
    elif args.wo_cmd == "cancel":
        _print(ops.cancel(args.wo_id), args.json)
    elif args.wo_cmd == "done":
        _print(ops.mark_done(args.wo_id, project_name=args.project), args.json)
    elif args.wo_cmd == "ack":
        _print(ops.ack_attention(args.wo_id, all_projects=args.all,
                                 project_name=args.project), args.json)
    elif args.wo_cmd in ("hide", "unhide"):
        _print(ops.hide_work_order(args.wo_id, hidden=args.wo_cmd == "hide",
                                   project_name=args.project), args.json)
    elif args.wo_cmd == "delete":
        if not args.yes:
            raise SystemExit(
                f"refusing to delete {args.wo_id}: this erases the work order, its "
                "timeline, messages and assumptions for good. Re-run with --yes "
                "(or use `jarvis wo hide` to just get it out of the way)."
            )
        _print(ops.delete_work_order(args.wo_id, project_name=args.project), args.json)
    elif args.wo_cmd == "inject":
        _print(ops.inject_session(args.session_id, project_name=args.project,
                                  title=args.title), args.json)
    elif args.wo_cmd == "resume-auto":
        _print(ops.resume_in_auto(args.wo_id, project_name=args.project), args.json)
    return 0


FO_ICON = {"pending": "⏳", "planning": "🧭", "plan_review": "👀", "executing": "🟢",
           "completed": "✅", "failed": "❌", "cancelled": "🚫"}


def cmd_fo(args: argparse.Namespace) -> int:
    import json as _json

    from . import ops

    if args.fo_cmd == "create":
        fo = ops.create_feature_order(args.project, args.title,
                                      description=args.description, origin=args.origin,
                                      max_parallel=args.max_parallel)
        _print({"created": fo["id"], "project": args.project, "status": fo["status"],
                **({"max_parallel": fo["max_parallel"]} if fo["max_parallel"] else {}),
                "note": "jarvisd will open a planner for it shortly; the plan comes "
                        "back for review before any work order is created"}, args.json)

    elif args.fo_cmd == "list":
        rows = ops.list_feature_orders(args.project, include_settled=args.all)
        if args.json:
            _print(rows, True)
        elif not rows:
            print("no feature orders")
        else:
            for fo in rows:
                icon = FO_ICON.get(fo["status"], "•")
                att = " ⚠" if fo["needs_attention"] else ""
                print(f"{icon} {fo['id']} [{fo['project']}] {fo['title']} "
                      f"({fo['status']}, {fo['progress']['label']}, "
                      f"{_age(fo['created_at'])}){att}")

    elif args.fo_cmd == "show":
        detail = ops.show_feature_order(args.fo_id, args.project)
        if args.json:
            _print(detail, True)
        else:
            print(f"{FO_ICON.get(detail['status'], '•')} {detail['id']} "
                  f"[{detail['project']}] {detail['title']} ({detail['status']})")
            print(f"\n{detail['description']}\n")
            if detail["attention_reason"]:
                print(f"⚠ {detail['attention_reason']}\n")
            if detail["planner"]:
                p = detail["planner"]
                print(f"planner: {p['id']} ({p['status']})")
            if detail["plan_text"]:
                print(f"\nplan:\n{detail['plan_text']}")
            if detail["max_parallel"]:
                print(f"slots: at most {detail['max_parallel']} children at once "
                      f"({detail['active_children']} running now)")
            if detail["children"]:
                print(f"\nchildren ({detail['progress']['label']}):")
                for c in detail["children"]:
                    icon = STATUS_ICON.get(c["status"], "•")
                    att = " ⚠" if c["needs_attention"] else ""
                    print(f"  {icon} {c['id']} {c['title']} "
                          f"({c['status_label']}){att}")

    elif args.fo_cmd == "plan":
        path = Path(args.from_file)
        if not path.is_file():
            raise ops.OpsError(f"no such plan file: {path}")
        try:
            doc = _json.loads(path.read_text())
        except _json.JSONDecodeError as e:
            raise ops.OpsError(f"{path} is not valid JSON: {e}") from e
        _print(ops.submit_plan(args.fo_id, doc, project_name=args.project), args.json)

    elif args.fo_cmd == "approve":
        _print(ops.review_plan(args.fo_id, accept=not args.reject,
                               feedback=args.feedback, decided_by="user",
                               project_name=args.project), args.json)

    elif args.fo_cmd == "cancel":
        _print(ops.cancel_feature_order(args.fo_id, args.project), args.json)
    return 0


GATE_ICON = {"pending": "⏸", "approved": "✅", "denied": "⛔", "dismissed": "⊘",
             "expired": "⌛"}


def cmd_gate(args: argparse.Namespace) -> int:
    from . import ops

    if args.ga_cmd == "request":
        _print(ops.request_gate_approval(
            args.wo_id, args.command, why=args.why, evidence=args.evidence,
            project_name=args.project), args.json)
    elif args.ga_cmd == "list":
        rows = ops.list_gates(project_name=args.project, wo_id=args.wo,
                              pending_only=args.pending)
        if args.json:
            _print(rows, True)
        elif not rows:
            print("no approval requests" + (" pending" if args.pending else ""))
        else:
            for r in rows:
                icon = GATE_ICON.get(r["status"], "•")
                where = "you" if r["escalated"] else "neo"
                if r["status"] == "pending":
                    state = f"pending (with {where})"
                elif r["status"] == "dismissed":
                    state = f"dismissed by {r['decided_by'] or '?'} — not a gated action"
                else:
                    state = f"{r['status']} by {r['decided_by'] or '?'}"
                print(f"{icon} {r['id']} [{r['project']}] {r['kind']} · {state} "
                      f"· {r['wo_id']} · {_age(r['ts'])} ago")
                print(f"    {r['command']}")
                if r["status"] == "pending" and r["escalated"]:
                    print(f"    ↳ Neo escalated: {r['escalation_reason']}")
                    print(f"    ↳ jarvis gate approve {r['id']} --reason \"...\"  |  "
                          f"jarvis gate deny {r['id']} --reason \"...\"  |  "
                          f"jarvis gate dismiss {r['id']} --reason \"...\"")
                elif r["decision_reason"]:
                    print(f"    ↳ {r['decision_reason']}")
            # The classifier's own error rate, kept in front of whoever reads this list.
            # It is the only place the cost of an over-broad recogniser shows up as a
            # number rather than as one worker's lost round trip.
            dismissed = [r for r in rows if r["status"] == "dismissed"]
            if dismissed:
                print(f"\n{len(dismissed)} of {len(rows)} shown were dismissed as "
                      f"classifier false positives — commands that tripped a gate but "
                      f"perform no privileged action.")
    elif args.ga_cmd == "show":
        data = ops.show_gate(args.approval_id, project_name=args.project)
        if args.json:
            _print(data, True)
        else:
            q = data.pop("neo_question", None)
            _pretty(data)
            if q:
                print("\n-- the request as the reviewer saw it "
                      "----------------------------------")
                print(q["question"])
                if q.get("answer"):
                    print(f"\nVerdict: {q['answer']} ({q.get('answered_by')})")
                    print(f"Reason: {q.get('answer_reason')}")
    elif args.ga_cmd in ("approve", "deny", "dismiss"):
        verdict = {"approve": "approved", "deny": "denied",
                   "dismiss": "dismissed"}[args.ga_cmd]
        _print(ops.decide_gate(args.approval_id, verdict=verdict,
                               reason=args.reason, project_name=args.project), args.json)
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
            _print(ops.promote_backlog(args.item_id, force=args.force,
                                       as_feature=args.as_kind == "feature",
                                       max_parallel=args.max_parallel), args.json)
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
            # An audit surface, so retired entries are listed too — `--project` would
            # otherwise hide them, `search_knowledge` already shows them, and the same
            # command answering two different questions is worse than either.
            rows = (central.relevant_knowledge(args.project, limit=100,
                                               include_retired=True)
                    if args.project else central.search_knowledge("", limit=100))
            _print(rows, args.json)
        elif args.kn_cmd == "search":
            _print(central.search_knowledge(args.term), args.json)
        elif args.kn_cmd == "retract":
            try:
                row = central.retract_knowledge(args.knowledge_id, args.reason)
            except (KeyError, ValueError) as exc:
                # `.args[0]`, not `str(exc)`: KeyError stringifies to its repr, so the
                # message would reach the user wrapped in quotes.
                print(f"error: {exc.args[0]}", file=sys.stderr)
                return 1
            _print({"retracted": row["id"], "reason": row["retired_reason"],
                    "content": row["content"]}, args.json)
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
            # An audit surface: retired learnings are listed, marked, and shown with the
            # reason they were retired. Neo's PROMPT gets the filtered default.
            rows = neo.learnings(args.project, limit=200, include_retired=True)
        finally:
            neo.close()
        if args.json:
            _print(rows, True)
        else:
            for r in rows:
                scope = r["project"] or "global"
                retired = (f" [retired: {r['retired_reason']}]"
                           if r["retired_at"] else "")
                bullet = "⊘" if r["retired_at"] else "•"
                print(f"{bullet} #{r['id']} [{scope}] ({r['source']}) "
                      f"{r['content']}{retired}")
            if not rows:
                print("Neo has no learnings yet — review its answers to teach it")
    elif args.neo_cmd == "learn":
        neo = NeoStore()
        try:
            row = neo.add_learning(args.content, project=args.project, source="manual")
        finally:
            neo.close()
        _print({"learned": row["id"], "project": args.project or "global"}, args.json)
    elif args.neo_cmd == "retract":
        neo = NeoStore()
        try:
            row = neo.retract_learning(args.learning_id, args.reason)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 1
        finally:
            neo.close()
        _print({"retracted": row["id"], "reason": row["retired_reason"],
                "content": row["content"]}, args.json)
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


def cmd_bug(args: argparse.Namespace) -> int:
    from .bugreport import report_bug
    result = report_bug(title=args.title, description=args.description,
                        expected=args.expected, actual=args.actual, steps=args.steps,
                        project=args.project, wo_id=args.wo_id)
    _print(result, args.json)
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
    from .bugreport import BugReportError
    from .catalog import CatalogError
    from .ops import OpsError
    try:
        if args.cmd == "start":
            return cmd_start(args)
        if args.cmd == "stop":
            return cmd_stop(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
        if args.cmd == "adopt":
            return cmd_adopt(args)
        if args.cmd == "wo":
            return cmd_wo(args)
        if args.cmd == "fo":
            return cmd_fo(args)
        if args.cmd == "gate":
            return cmd_gate(args)
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
        if args.cmd == "bug":
            return cmd_bug(args)
        if args.cmd == "ui":
            return cmd_ui(args)
        if args.cmd == "daemon" and args.d_cmd == "run":
            from .daemon import run_daemon
            run_daemon(args.catalog, poll_interval=args.poll_interval)
            return 0
    except (OpsError, CatalogError, BugReportError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
