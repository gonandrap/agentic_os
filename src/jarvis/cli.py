"""jarvis — the agentic OS command line.

Grouped commands:
  jarvis start|stop|status|adopt          OS lifecycle
  jarvis cost [project|wo-id|fo-id]       what the work has cost in tokens
  jarvis wo create|list|show|send|ask|assume|finish|review|cancel|done|inject
  jarvis fo create|list|show|plan|submit|approve|cancel feature orders (planned sets)
  jarvis gate request|list|show|approve|deny|dismiss   privileged-action approvals
  jarvis gate rules|rule-retract|explain  what counts as privileged, and what the OS
                                          has LEARNED does not
  jarvis neo list|show|review|answer|learnings|learn|export
  jarvis backlog add|list|show|promote|done
  jarvis learn add|list|search|show|topics|pin|unpin
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


def _readable_rounds(detail: dict[str, Any]) -> dict[str, Any]:
    """A unit's document with its validation rounds collapsed to one line each.

    HUMAN output only, and the same trick `_with_readable_digest` plays: `--json` gets
    the rows as they are stored, because other tooling reads them, while a person gets
    "round 1 · a1b2 · rejected — no test touches the new branch" instead of a nested
    block per round. A unit that has never been validated loses the key altogether, so
    its document reads exactly as it did before rounds existed — which is every unit in
    a fleet that has not turned validation on.
    """
    from . import ops

    row = dict(detail)
    rounds = row.pop("validation_rounds", None) or []
    if rounds:
        row["validation_rounds"] = [ops.round_line(r) for r in rounds]
    return row


def _with_readable_digest(q: dict[str, Any]) -> dict[str, Any]:
    """A Neo question row with its dashboard digest decoded, for HUMAN output only.

    `questions.digest` holds JSON, and printing it as one long line is the opposite of
    what a digest is for. Decoded into a nested block when there is one, replaced by the
    recorded reason when the attempt failed, dropped entirely when it was never
    attempted. `--json` never comes through here: a row dump is the row as stored.
    """
    from . import digest as digest_mod

    row = dict(q)
    view = digest_mod.decode(row.get("digest"))
    failed = digest_mod.failure_reason(row.get("digest"))
    row.pop("digest", None)
    if view:
        row["digest (dashboard only — Neo read the question above in full)"] = view
    elif failed:
        row["digest"] = f"(not produced: {failed})"
    return row


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


def _stamp(ts: float | None) -> str:
    """An absolute local timestamp, for records read as a ledger rather than a feed.

    `_age` answers "is this stale?" and is right for anything in flight. A learning is
    not in flight: what a reader wants of it is WHEN it was recorded, so they can line
    it up against the decision that produced it.
    """
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


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

    sp = sub.add_parser("cost", help="what the fleet's work has cost in tokens")
    sp.add_argument("target", nargs="?",
                    help="a project, a work-order id, or a feature-order id "
                         "(default: the whole fleet)")
    sp.add_argument("--limit", type=int, default=50,
                    help="work orders per project to measure (default: 50)")
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
    c.add_argument("--parent", default="",
                   help="file it UNDER this feature order: it becomes one of the "
                        "feature's children, so the feature waits for it and shows it "
                        "in its tree (the feature must still be open)")

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

    s = wo.add_parser("show", help="show one work order: what was said (the "
                                   "conversation), what happened (the timeline), and "
                                   "its assumptions")
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

    d = wo.add_parser("defer", help="(workers) hand off work that is not this work "
                                    "order's job — the OS decides where it lands")
    d.add_argument("wo_id")
    d.add_argument("title")
    d.add_argument("--why", required=True,
                   help="why it should not be done now — this is what a reader sees "
                        "months later when deciding whether it is still worth doing")
    d.add_argument("--description", "-d", default="",
                   help="the brief for whoever eventually picks it up")
    d.add_argument("--neo-question", dest="neo_question", type=int, metavar="ID",
                   help="the Neo question this deferral was agreed in")
    d.add_argument("--project")

    f = wo.add_parser("finish", help="(workers) mark a work order finished")
    f.add_argument("wo_id")
    f.add_argument("--summary", required=True)
    f.add_argument("--pr", default="", metavar="URL",
                   help="the pull request this work order opened — parks it in "
                        "'waiting for PR merge' instead of completing it, so it stays "
                        "on the open list until the user merges")
    f.add_argument("--evidence", default="", metavar="TEXT",
                   help="how you tested this: what you ran and what it said. An "
                        "independent reviewer reads it beside your diff")

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
                       help="say what a work order is really waiting on, and unstick it "
                            "if it is a permission prompt (nothing else can be)")
    ra.add_argument("wo_id")
    ra.add_argument("--project")
    ra.add_argument("--force", action="store_true",
                    help="send the nudge even when nothing is stuck — it costs a full "
                         "re-send of the worker's conversation")

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

    f = fo.add_parser("submit", help="(project managers) submit the feature for review "
                                     "again, once the remediation work orders have landed")
    f.add_argument("fo_id")
    f.add_argument("--summary", required=True,
                   help="what changed since the last round, in your own words")
    f.add_argument("--evidence", default="",
                   help="how the feature as a whole was verified — the reviewer reads "
                        "this against the integrated diff, so a claim the diff does not "
                        "support is worse than no claim")
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
    g = ga.add_parser(
        "rules",
        help="the rule base: what counts as a privileged action, and what has been "
             "LEARNED not to. Seeded from the builtins, grown from dismissals",
    )
    g.add_argument("--role", choices=("match", "exempt", "canary"))
    g.add_argument("--kind", help="one gate name")
    g.add_argument("--all", action="store_true", help="include retracted rules")
    g = ga.add_parser("rule-retract",
                      help="retire a rule: it stops applying, the record keeps that it "
                           "once did")
    g.add_argument("rule_id")
    g.add_argument("--reason", required=True)
    g = ga.add_parser(
        "explain",
        help="why a command would or would not trip a gate — paste the exact string "
             "from a gate record to diagnose a false positive",
    )
    g.add_argument("command")
    g.add_argument("--project")

    # backlog ---------------------------------------------------------------------------
    bl = sub.add_parser("backlog", help="unified deferred-work backlog").add_subparsers(
        dest="bl_cmd", required=True)
    b = bl.add_parser("add")
    b.add_argument("project")
    b.add_argument("title")
    b.add_argument("--description", "-d", default="")
    b.add_argument("--depends-on", default="", help="comma-separated backlog ids")
    # Where the item came from, when something deferred it rather than somebody typing
    # it. Flags rather than prose in the description, so the relationship survives being
    # read back by a query instead of by a person.
    b.add_argument("--origin-wo", dest="origin_wo", metavar="WO-ID",
                   help="the work order that suggested this")
    b.add_argument("--origin-fo", dest="origin_fo", metavar="FO-ID",
                   help="the feature order whose plan it came out of")
    b.add_argument("--origin-note", dest="origin_note", default="",
                   help="why it was deferred, and the Neo question id if there was one")
    b = bl.add_parser("list")
    b.add_argument("project", nargs="?")
    b.add_argument("--all", action="store_true", help="include non-open items")
    b = bl.add_parser("show", help="one backlog item in full, including where it "
                                   "came from")
    b.add_argument("item_id")
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
    k.add_argument("--pin", action="store_true",
                   help="inject this entry verbatim into every worker prompt "
                        "(reserve for safety rails; everything else is looked up)")
    k = kn.add_parser("list", help="headlines only unless --full")
    k.add_argument("--project", help="this project + global entries")
    k.add_argument("--topic")
    k.add_argument("--full", action="store_true", help="full text instead of headlines")
    k.add_argument("--limit", type=int, default=100)
    k = kn.add_parser("search", help="full text of entries matching a term")
    k.add_argument("term")
    k.add_argument("--project", help="this project + global entries")
    k.add_argument("--topic")
    k.add_argument("--limit", type=int, default=20)
    k = kn.add_parser("show", help="full text of specific entries, by id")
    k.add_argument("ids", nargs="+")
    k = kn.add_parser("topics", help="topics and how many entries each holds")
    k.add_argument("--project")
    k = kn.add_parser("pin", help="always inject this entry in full")
    k.add_argument("kn_id")
    k = kn.add_parser("unpin", help="demote to an index headline")
    k.add_argument("kn_id")
    k = kn.add_parser("retract", help="retire a superseded entry (kept for the record, "
                                      "dropped from worker prompts and the index)")
    k.add_argument("knowledge_id")
    k.add_argument("--reason", required=True,
                   help="what supersedes it — this is kept beside the entry for ever")

    # brief ---------------------------------------------------------------------------------
    bf = sub.add_parser(
        "brief",
        help="(workers) print one full briefing section on demand — the worker "
             "prompt carries a compressed core plus an index of these")
    bf.add_argument("section", nargs="?", default="",
                    help="one of: contract, gates, record, navigation, knowledge; "
                         "omit to list them")
    bf.add_argument("--wo", default="", metavar="WO_ID",
                    help="render with this work order's real ids")

    # neo -----------------------------------------------------------------------------------
    ne = sub.add_parser("neo", help="Neo: the OS answerer agent (answers workers as you)"
                        ).add_subparsers(dest="neo_cmd", required=True)
    n = ne.add_parser("list", help="questions Neo has handled or is handling")
    n.add_argument("--all", action="store_true", help="include reviewed items")
    n = ne.add_parser("show", help="one question with Neo's full answer")
    n.add_argument("question_id", type=int)
    n.add_argument("--panel", action="store_true",
                   help="also show how the panel deliberated: one line per seat")
    n = ne.add_parser("review", help="approve or correct one of Neo's answers")
    n.add_argument("question_id", type=int)
    n.add_argument("--correct", metavar="FEEDBACK",
                   help="reject the answer and teach Neo what you would have said")
    n.add_argument("--seat", metavar="NAME", default="",
                   help="teach only this panel seat, not all of them (the seat must "
                        "have opined on this question — see `neo show --panel`)")
    n = ne.add_parser("answer", help="answer a question Neo escalated to you")
    n.add_argument("question_id", type=int)
    n.add_argument("text")
    n = ne.add_parser("learnings", help="what Neo has learned from your reviews")
    n.add_argument("--project", default="")
    n.add_argument("--seat", metavar="NAME", default="",
                   help="also show learnings scoped to this panel seat (global rows "
                        "alone are shown without it)")
    n.add_argument("--json", action="store_true", help="machine-readable output")
    n = ne.add_parser("export", help="Neo's whole ledger as one document: every "
                                     "question, learning and panel opinion")
    n.add_argument("--json", action="store_true", help="machine-readable output")
    n = ne.add_parser("learn", help="teach Neo directly (no question needed)")
    n.add_argument("content")
    n.add_argument("--project", default="")
    n = ne.add_parser("retract", help="retire a superseded learning (kept for the "
                                      "record, dropped from Neo's prompt)")
    n.add_argument("learning_id", type=int)
    n.add_argument("--reason", required=True,
                   help="what supersedes it — this is kept beside the learning for ever")

    # validation ----------------------------------------------------------------------
    # The deliberation, on demand and nowhere else. `wo show` and `fo show` carry the
    # ROUNDS — number, outcome, reason — because that is what the unit was told; what
    # lives here is what the seats said, which nobody is made to read.
    va = sub.add_parser(
        "validation", help="how the validation panel judged a work or feature order"
    ).add_subparsers(dest="validation_cmd", required=True)
    v = va.add_parser("show", help="every round in full: each seat's verdict, model, "
                                   "latency and raw reply")
    # One command for both units: a round is the same fact either way, and the id says
    # which one it is (`fo-…` is a feature order, anything else a work order).
    v.add_argument("unit_id", metavar="WO_ID|FO_ID")
    v.add_argument("--project")

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
                # `resume_auto` is present only where a permission prompt is possible at
                # all. Where it is not — everywhere in a fleet running `auto` — naming it
                # sent the user at a command that could not help (GitHub issue 100).
                alt = (f"  ·  or `{a['resume_auto']}`" if a.get("resume_auto") else "")
                print(f"      open it: {a['attach']}{alt}")
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
    # Only offer --repair when something here is actually repairable: OS-level
    # violations never are, and a suggestion that does nothing teaches the user to
    # ignore the line.
    fixable = any(v["repaired"] for p in res["projects"] for v in p["violations"])
    verb = ("repaired" if res["repair"]
            else "found (run with --repair to fix)" if fixable else "found")
    print(f"⚠ {res['violations']} invariant violation(s) {verb}:\n")
    for v in res.get("os", []):
        # OS-level: not about any one project (the dashboard being down, say), and
        # never auto-repairable — there is nothing to derive, only someone to tell.
        print(f"• [os] {v['invariant']}")
        print(f"    {v['detail']}")
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


def _tok(n: int) -> str:
    """Token counts, at the magnitude a reader can hold in their head."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def _print_turn_table(res: dict) -> None:
    """The per-turn breakdown of one work order, from the recorded result JSON.

    Provenance is announced before any number: `recorded` rows are the CLI's own
    accounting (exact), everything else is the transcript estimate, and a mixed
    record says so out loud rather than adding the two systems into one figure.
    """
    rows = res.get("turns_detail") or []
    provenance = res.get("provenance")
    if not rows:
        if provenance == "transcript":
            print("\nno turns on record — the figures above are transcript estimates "
                  "(this session predates turn capture, or was never Jarvis-driven)")
        return
    print()
    if provenance == "recorded":
        print("per-turn, recorded from the CLI's own result JSON — exact:")
    elif provenance == "mixed":
        print(f"⚠ {res['turns_recorded']} of {res['turns_settled']} settled turns are "
              "recorded — the rest exist only as transcript estimates, so the table "
              "below and the totals above are two different accountings:")
    else:
        print("no recorded turns — figures above are transcript estimates; "
              "the turn list below carries timings only:")
    print(f"{'turn':>4} {'kind':>8} {'dur':>6} {'$':>7} {'in':>6} {'cache-w':>8} "
          f"{'1h/5m':>12} {'cache-r':>8} {'out':>7} {'calls':>5}  context")
    for r in rows:
        if not r["recorded"]:
            print(f"{r['seq']:>4} {r['kind']:>8} {_dur(r['duration_s']):>6} "
                  f"{'—':>7} {'—':>6} {'—':>8} {'—':>12} {'—':>8} {'—':>7} {'—':>5}  "
                  f"not recorded ({r['state']})")
            continue
        ctx = _tok(r["context_peak"])
        if r.get("context_pct") is not None:
            ctx += (f" ({r['context_pct']:.0f}% of "
                    f"{_tok(r['context_window'])} window)")
        split = f"{_tok(r['cache_1h'])}/{_tok(r['cache_5m'])}"
        calls = str(r["api_calls"]) if r.get("api_calls") else "—"
        print(f"{r['seq']:>4} {r['kind']:>8} {_dur(r['duration_s']):>6} "
              f"{(r['cost_usd'] or 0):>7.2f} {_tok(r['input']):>6} "
              f"{_tok(r['cache_write']):>8} {split:>12} {_tok(r['cache_read']):>8} "
              f"{_tok(r['output']):>7} {calls:>5}  {ctx}")
    rec = res.get("recorded_totals")
    if rec:
        line = (f"recorded total ${rec['cost_usd']:.2f} — {rec['api_calls']} API "
                f"call{'s' if rec['api_calls'] != 1 else ''}, "
                f"context peak {_tok(rec['context_peak'])}")
        if rec.get("context_window"):
            line += (f" ({100 * rec['context_peak'] / rec['context_window']:.0f}% of "
                     f"the {_tok(rec['context_window'])} window)")
        print(f"\n{line}")


def _print_write_ttl(totals: dict) -> None:
    """What the fleet paid to WRITE to the prompt cache, and at which of the two rates.

    Silent when the whole line was at 1.25x: a report that says "all good" every run is a
    line readers stop seeing. The rate is derived from the split rather than restated from
    a constant, so it says what was PAID (kn-2e0a6317). Spec:
    2026-08-22-the-five-minute-write-everywhere.md.
    """
    from . import usage as usage_mod

    write, hour = totals.get("cache_write") or 0, totals.get("cache_1h") or 0
    if not write or not hour:
        return
    rate = usage_mod.write_rate(write, hour, totals.get("cache_5m") or 0)
    print(f"  cache write   {_tok(write)} tokens at {rate:.2f}x base input — "
          f"{_tok(hour)} of it bought the ONE-HOUR TTL (2x) rather than the "
          f"five-minute one (1.25x)")
    print("                every path Jarvis launches now forces the 5-minute write, "
          "so a figure here is spend that predates that or a `claude` it did not start")


def _print_os_calls(res: dict) -> None:
    """What Jarvis itself spent on this work order, call by call.

    Only on the single-work-order view, and only when there is something to show. This is
    the table that answers "why did an order I never touched cost three dollars" — five
    panel seats on one gate review is a shape a total can never make visible.
    """
    rows = res.get("os_calls_detail") or []
    if not rows:
        return
    unit = (res.get("units") or [{}])[0]
    print(f"\njarvis's own calls for this work order — ~${unit.get('os_cost_usd', 0):.2f} "
          f"of the total, recorded as they happened:")
    print(f"{'kind':>12} {'what':>10} {'$':>7} {'in':>8} {'out':>7}  model")
    for r in rows:
        print(f"{r['kind']:>12} {r['label'][:10]:>10} {r['list_cost_usd']:>7.2f} "
              f"{_tok(r['billed_input']):>8} {_tok(r['output']):>7}  {r['model'][:28]}"
              f"{'' if r['ok'] else '  (failed)'}")
    for kind in unit.get("os_by_kind") or []:
        print(f"  {kind['label']}: {kind['calls']} call"
              f"{'s' if kind['calls'] != 1 else ''}, ~${kind['cost_usd']:.2f}")


def _print_subprocess_calls(res: dict) -> None:
    """What the worker spent in `claude` processes it spawned itself, by what ran them.

    A separate block from `_print_os_calls` because it is a separate class of spend, and
    grouped rather than listed because one eval suite is forty calls. This is the table
    that answers "the worker only took nine turns, so why did this cost five dollars".
    """
    rows = res.get("subproc_detail") or []
    if not rows:
        return
    unit = (res.get("units") or [{}])[0]
    print(f"\nclaude processes this worker spawned — "
          f"~${unit.get('subproc_cost_usd', 0):.2f} of the total:")
    print(f"{'ran by':>16} {'calls':>6} {'$':>7} {'in':>8} {'out':>7}  model")
    for r in rows:
        failed = f"  ({r['failed']} failed)" if r["failed"] else ""
        print(f"{r['label'][:16]:>16} {r['calls']:>6} {r['list_cost_usd']:>7.2f} "
              f"{_tok(r['billed_input']):>8} {_tok(r['output']):>7}  "
              f"{r['model'][:28]}{failed}")


def _print_bill_line(line: dict, depth: int = 0) -> None:
    """One line of the bill tree, and everything under it."""
    pad = "  " * depth
    unit = line.get("unit")
    count = ""
    # "turn 4 (1 turn)" says nothing twice. A count earns its place when there is more
    # than one of something, or when the thing counted is not already in the label. An
    # empty unit means the line MIXES kinds — a turn and the OS calls it caused — and
    # "11 charges" would be a number about nothing, so it is left off rather than
    # guessed at. (The page does the same; the two renderers must not disagree.)
    if line["calls"] and unit != "" and not (line["calls"] == 1 and unit == "turn"):
        count = f" ({line['calls']} {unit or 'charge'}"
        count += "s)" if line["calls"] != 1 else ")"
    print(f"{line['cost']['list_usd']:>9.3f} {_tok(line['tokens']['total']):>8}  "
          f"{pad}{line['label']}{count}")
    for child in line.get("children") or []:
        _print_bill_line(child, depth + 1)


def _print_provenance(acc: dict) -> None:
    """When this bill was worked out, and what it could not see.

    The same sentences the page prints, for the same reason: a caveat that appears in
    one renderer and not the other is one the reader learns to ignore.
    """
    import time as _time

    if acc.get("sealed_at"):
        when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(acc["sealed_at"]))
        print(f"  sealed when this order settled, on {when} — the records behind it "
              f"expire, this figure does not")
        if acc.get("resealed_at"):
            again = _time.strftime("%Y-%m-%d %H:%M",
                                   _time.localtime(acc["resealed_at"]))
            print(f"  re-derived on {again} to add detail the original seal predates; "
                  f"adopted only because it still saw every token the seal held")
    elif acc.get("live"):
        print("  worked out just now, from records that are still live; sealed "
              "automatically once the order settles")
    for gap in acc.get("gaps") or []:
        print(f"  ⚠ {gap}")


def _print_turn_calls(rows: list[dict]) -> None:
    """Every API call of every turn — the grain under the turn.

    A turn is an agent LOOP, not one call: the model answers, a tool runs, the model is
    called again with the result appended. Each call re-sends the conversation, so a
    turn's cache-read is a SUM over its calls, and until this table existed there was no
    way to see that a turn reading 517k was eleven calls re-reading one 55k conversation.
    `ctx` is what a single call carried and is the figure that grows down the table;
    `read` is what that call was billed for reading, and the two are the same number once
    the prefix is warm. The last column is where the money went, call by call.
    """
    detailed = [r for r in rows if r.get("call_rows")]
    if not detailed:
        return
    print("\nevery API call, turn by turn (these sum to the turn — the lead agent's "
          "calls; a subagent has rows of its own):")
    print(f"{'turn':>4} {'call':>5} {'ctx':>8} {'read':>8} {'wrote':>8} {'out':>7}  "
          f"model")
    for r in detailed:
        for n, c in enumerate(r["call_rows"], 1):
            print(f"{r['seq']:>4} {n:>5} {_tok(c['context']):>8} "
                  f"{_tok(c['cache_read']):>8} {_tok(c['cache_write']):>8} "
                  f"{_tok(c['output']):>7}  {c['model']}")
        folded = r.get("calls_folded")
        if folded:
            # Never a silent cap: a list that just stops reads as a complete one.
            print(f"{r['seq']:>4} {'…':>5} {'—':>8} {_tok(folded['cache_read']):>8} "
                  f"{_tok(folded['cache_write']):>8} {_tok(folded['output']):>7}  "
                  f"{folded['count']} further calls, folded into this line")
        cover = r.get("calls_cover") or {}
        if cover and cover.get("total", 0) < (r.get("input", 0) + r.get("cache_write", 0)
                                              + r.get("cache_read", 0)
                                              + r.get("output", 0)):
            print(f"{'':>4} {'':>5}  these are the lead agent's calls; the rest of "
                  f"turn {r['seq']} is the subagents it spawned, itemised above")


def _print_bill(bill: dict) -> None:
    """One order's itemised bill — the same payload the dashboard renders.

    Both views are printed, because both answer a question the other cannot: who spent
    it, and when. They are two foldings of one set of charges, so they come to the same
    number, and `checks` says so on the page rather than leaving it to be trusted.
    """
    from . import bill as bill_mod

    total = bill["total"]
    print(f"{bill['scope']} — {bill['title']}\n")
    exact = total["cost"]["exact_usd"]
    headline = (f"~${total['cost']['list_usd']:.2f} at list prices · "
                f"{_tok(total['tokens']['total'])} tokens")
    if exact and not total["exact_missing"]:
        headline += f" · the claude CLI billed ${exact:.2f} for it"
    elif exact:
        headline += (f" · ${exact:.2f} of it has the CLI's own figure, "
                     f"{total['exact_missing']} line(s) do not")
    print(headline)
    checks = bill.get("checks") or {}
    if checks.get("balanced"):
        print("✓ every line below adds up to that — by actor, turn by turn, and agent "
              "by agent")
    else:
        for problem in checks.get("problems") or []:
            print(f"✗ {problem}")
    _print_provenance(bill.get("accuracy") or {})
    for view, caption in (("actors", "by who spent it"),
                          ("turns", "the same tokens, turn by turn"),
                          ("agents", "agent by agent — the lead, then what it spawned"),
                          ("orders", "the orders under it")):
        lines = bill.get(view) or []
        if not lines:
            continue
        print(f"\n{caption}:")
        print(f"{'$':>9} {'tokens':>8}")
        if view == "orders":
            for order in lines:
                _print_bill_line(order["total"])
            continue
        for line in lines:
            _print_bill_line(line)
    rows = bill.get("turn_rows") or []
    if rows:
        # Not a charge and deliberately outside the bill: the same context is re-read on
        # every API call of a turn, and what was PAID for is the reading. This is the
        # /context statistic — the column that shows an order bloating, which is the
        # other reason someone runs this command.
        print("\nhow the context grew (not a charge — the largest single API call of "
              "each turn):")
        print(f"{'turn':>4} {'kind':>8} {'dur':>6} {'calls':>6}  context peak")
        for r in rows:
            if not r["recorded"]:
                print(f"{r['seq']:>4} {r['kind']:>8} {_dur(r['duration_s']):>6} "
                      f"{'—':>6}  not recorded ({r['state']})")
                continue
            ctx = _tok(r["context_peak"])
            if r.get("context_pct") is not None:
                ctx += (f" ({r['context_pct']:.0f}% of "
                        f"{_tok(r['context_window'])} window)")
            print(f"{r['seq']:>4} {r['kind']:>8} {_dur(r['duration_s']):>6} "
                  f"{r['api_calls'] or '—':>6}  {ctx}")
        _print_turn_calls(rows)
    print(f"\nwhat each class of token cost ({_tok(total['tokens']['total'])} in all):")
    # The cache-write rate is the one this order PAID, worked out from the TTL split the
    # CLI reported, and never the range `1.25–2x` — printing the price list beside a
    # figure computed from the split tells the reader nothing they were asking.
    rate = bill_mod.write_rate_of(total)
    classes = (
        ("input", "fresh input", "base input rate — never cached"),
        ("cache_write", "cache write",
         f"{rate:.2f}x base input — {bill_mod.rate_note(total, _tok)}"),
        ("cache_read", "cache read", "0.1x base input — served from the cache"),
        ("output", "output", "output rate, what the model wrote"),
    )
    for cls, label, why in classes:
        print(f"{total['cost']['by_class'][cls]:>9.3f} "
              f"{_tok(total['tokens'][cls]):>8}  {label:<12} {why}")
    print(f"\n'tokens' above is {bill_mod.TOKENS_MEAN}.")
    # What is NOT here, named. An absent line reads as an omission, and "was nothing at
    # all spent on the OS?" was one of the four questions this surface exists to answer.
    # Said in the terminal exactly as the page says it: a caveat that appears in one
    # renderer and not the other is one the reader learns to ignore.
    absent = {
        "worker": "the worker's own session — nothing measurable (never dispatched, or "
                  "its transcript and every turn's result JSON are gone)",
        "jarvis": "jarvis spent nothing of its own — no Neo question, no panel, no "
                  "digest",
        "subprocesses": "no claude processes the worker spawned itself were recorded",
    }
    present = {line["key"] for line in bill.get("actors") or []}
    missing = [text for key, text in absent.items() if key not in present]
    if missing and bill["kind"] == "work_order":
        print("\nnot on this bill:")
        for text in missing:
            print(f"  · {text}")
    rewrite = bill.get("rewrite") or {}
    if rewrite.get("tokens"):
        print(f"\nre-write tax ~${rewrite['list_usd']:.2f} — "
              f"{_tok(rewrite['tokens'])} tokens of context re-sent to the cache across "
              f"{rewrite['boundaries']} turn "
              f"boundar{'ies' if rewrite['boundaries'] != 1 else 'y'}. "
              f"Not an extra charge: it is the "
              f"part of the cache-write line above that paid to send context a warm "
              f"cache would have served at a tenth of the price.")
    subagents = bill.get("subagents") or {}
    if subagents.get("count"):
        print(f"{subagents['count']} subagent(s), ~${subagents['list_usd']:.2f}, "
              f"itemised above under the turn each ran in — drawn out of that turn's "
              f"total, never added to it")
    for note in bill.get("notes") or []:
        print(f"\n⚠ {note}")
    print(f"\nEvery figure above is {bill['floor_reason']}.")
    print("List prices, as a common unit for comparing token kinds — not a bill.")


def cmd_cost(args: argparse.Namespace) -> int:
    from . import ops
    target = args.target
    # One argument, three kinds of thing. Ids are prefixed and projects are not, so
    # this never has to guess: anything that is not `wo-…`/`fo-…` is a project name.
    is_id = bool(target) and target.split("-")[0] in ("wo", "fo")
    if is_id:
        # One order asks "where did MY tokens go", which is the bill's question. The
        # fleet view below asks "which orders cost the most", which is the report's.
        bill = ops.bill(target)
        if args.json:
            _print(bill, True)
            return 0
        _print_bill(bill)
        return 0
    res = ops.cost_report(project=None if is_id else target,
                          target=target if is_id else None, limit=args.limit)
    if args.json:
        _print(res, True)
        return 0

    units = res["units"]
    totals = res["totals"]
    if not units:
        print(f"no work orders found for {res['scope']}")
        return 0
    header = f"{res['scope']} — {res['measured']} measured"
    if res["unmeasured"]:
        header += f", {res['unmeasured']} with no transcript left"
    print(f"{header}\n")
    # `$` is the whole bill — the worker's conversation, what Jarvis itself spent on the
    # order, and what the worker spent below itself. `jarvis` and `sub` are how much of
    # it was each. A work order whose transcript is gone can still show those two: both
    # were recorded as they happened rather than read back from a file.
    print(f"{'$':>7} {'jarvis':>7} {'sub':>7} {'turns':>5} {'output':>7} "
          f"{'re-write':>9}  work order")
    for u in units:
        os_cost = f"{u['os_cost_usd']:.2f}" if u["os_calls"] else "—"
        sub_cost = f"{u['subproc_cost_usd']:.2f}" if u.get("subproc_calls") else "—"
        if not u["found"]:
            print(f"{'—':>7} {os_cost:>7} {sub_cost:>7} {'—':>5} {'—':>7} {'—':>9}  "
                  f"{u['id']}  {u['title'][:44]}")
            continue
        print(f"{u['total_cost_usd']:>7.2f} {os_cost:>7} {sub_cost:>7} {u['turns']:>5} "
              f"{_tok(u['output']):>7} {_tok(u['rewrite_excess']):>9}  "
              f"{u['id']}  {u['title'][:44]}")

    _print_turn_table(res)
    _print_os_calls(res)
    _print_subprocess_calls(res)

    print(f"\ntotal ~${totals['total_cost_usd']:.2f} at list prices")
    print(f"  workers       ~${totals['list_cost_usd']:.2f} "
          f"({_tok(totals['billed_input'])} in, {_tok(totals['output'])} out)")
    if totals["os_calls"]:
        print(f"  jarvis itself ~${totals['os_cost_usd']:.2f} — "
              f"{totals['os_calls']} call{'s' if totals['os_calls'] != 1 else ''} "
              f"(Neo answering, panel seats, digests), "
              f"{_tok(totals['os_billed_input'])} in, {_tok(totals['os_output'])} out")
    if totals.get("subproc_calls"):
        print(f"  subprocesses  ~${totals['subproc_cost_usd']:.2f} — "
              f"{totals['subproc_calls']} claude "
              f"process{'es' if totals['subproc_calls'] != 1 else ''} the workers "
              f"spawned themselves (eval suites, scripts), "
              f"{_tok(totals['subproc_billed_input'])} in, "
              f"{_tok(totals['subproc_output'])} out")
    if totals["rewrite_excess"]:
        print(f"  re-write tax  ~${totals['rewrite_cost_usd']:.2f} — "
              f"{_tok(totals['rewrite_excess'])} tokens re-sent across "
              f"{totals['resume_boundaries']} turn boundaries")
    if totals["subagent_cost_usd"]:
        print(f"  subagents     ~${totals['subagent_cost_usd']:.2f}")
    _print_write_ttl(totals)
    unattributed = res.get("os_unattributed") or {}
    if unattributed.get("os_calls"):
        print(f"  OS overhead   ~${unattributed['os_cost_usd']:.2f} — "
              f"{unattributed['os_calls']} calls no work order caused "
              f"(already in the total above)")
    # Both said every time rather than in the help text, and for the same reason: the
    # figures are the only numbers on screen, and reading them as an invoice — or as the
    # whole spend — is the likeliest misreading in each direction.
    if res.get("floor_reason"):
        print(f"\nEvery figure above is {res['floor_reason']}.")
    print("List prices, as a common unit for comparing token kinds — not a bill.")
    return 0


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
    from .timeline import build_conversation, build_timeline

    if args.wo_cmd == "create":
        deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
        parent = (args.parent or "").strip()
        wo = ops.create_work_order(
            args.project, args.title, description=args.description, origin=args.origin,
            model=args.model, effort=args.effort, permission_mode=args.permission_mode,
            append_system_prompt=args.append_system_prompt, depends_on=deps,
            parent_id=parent or None,
        )
        _print({"created": wo["id"], "project": args.project, "status": wo["status"],
                "depends_on": deps,
                **({"parent": parent} if parent else {}),
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
            events = store.list_events(args.wo_id)
            messages = store.list_messages(args.wo_id)
            detail = {
                "project": name, **wo,
                "status_label": invariants.status_label(store, wo),
                "blocked_by": store.unfinished_dependencies(args.wo_id),
                "timeline": build_timeline(wo, events, messages,
                                           include_debug=args.debug),
                # What was said, in order, whoever spoke — the worker's questions to
                # Neo included, which `messages` alone never held.
                "conversation": build_conversation(events, messages),
                # Every assumption, each with its `n` and `status` — §4.
                "assumptions": store.all_assumptions(args.wo_id),
                # What this work order was allowed (or refused) permission to ship.
                "gates": [
                    {k: a[k] for k in ("id", "kind", "command", "status", "escalated",
                                       "decided_by", "decision_reason")}
                    for a in store.list_approvals(args.wo_id)
                ],
                # Additive, and always present under `--json` even when empty: a key
                # that comes and goes is one every consumer has to guard. The seats'
                # replies are NOT here — see `jarvis validation show`.
                "validation_rounds": ops.validation_rounds(store, wo_id=args.wo_id),
            }
        finally:
            store.close()
        _print(_readable_rounds(detail) if not args.json else detail, args.json)

    elif args.wo_cmd == "send":
        _print(ops.send_message(args.wo_id, args.message, source=args.source,
                                project_name=args.project), args.json)
    elif args.wo_cmd == "assume":
        _print(ops.assume(args.wo_id, args.content), args.json)
    elif args.wo_cmd == "ask":
        _print(ops.ask_question(args.wo_id, args.question,
                                project_name=args.project), args.json)
    elif args.wo_cmd == "defer":
        _print(ops.defer(args.wo_id, args.title, args.why,
                         description=args.description,
                         neo_question_id=args.neo_question,
                         project_name=args.project), args.json)
    elif args.wo_cmd == "finish":
        _print(ops.finish(args.wo_id, args.summary, pr_url=args.pr or None,
                          evidence=args.evidence), args.json)
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
        _print(ops.resume_in_auto(args.wo_id, project_name=args.project,
                                  force=args.force), args.json)
    return 0


FO_ICON = {"pending": "⏳", "planning": "🧭", "plan_review": "👀", "executing": "🟢",
           "validating": "🔎", "completed": "✅", "failed": "❌", "cancelled": "🚫"}


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
            # Next to the planner, because they are the same question: who holds this
            # feature. Absent, and silent, for a feature planned with validation off.
            if detail["manager"]:
                m = detail["manager"]
                print(f"manager: {m['id']} ({m['status']})")
            if detail["validation_rounds"]:
                print("\nvalidation:")
                for rnd in detail["validation_rounds"]:
                    print(f"  {ops.round_line(rnd)}")
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

    elif args.fo_cmd == "submit":
        _print(ops.submit_feature(args.fo_id, args.summary, evidence=args.evidence,
                                  project_name=args.project), args.json)

    elif args.fo_cmd == "approve":
        _print(ops.review_plan(args.fo_id, accept=not args.reject,
                               feedback=args.feedback, decided_by="user",
                               project_name=args.project), args.json)

    elif args.fo_cmd == "cancel":
        _print(ops.cancel_feature_order(args.fo_id, args.project), args.json)
    return 0


GATE_ICON = {"pending": "⏸", "approved": "✅", "denied": "⛔", "dismissed": "⊘",
             "expired": "⌛"}


_ROLE_ICON = {"match": "✋", "exempt": "✓", "canary": "🔒"}


def _print_gate_rules(data: dict) -> None:
    """The rule base, grouped by role.

    Canaries print last and without ceremony. They are not rules anyone consults; they
    are the reason the other two groups can be changed at all, and the line that matters
    about them is the one at the bottom saying they all still hold.
    """
    rules = data["rules"]
    if not rules:
        print("no gate rules")
        return
    for role in ("match", "exempt", "canary"):
        group = [r for r in rules if r["role"] == role]
        if not group:
            continue
        label = {"match": "recognisers", "exempt": "exemptions (learned)",
                 "canary": "canaries — must always gate"}[role]
        print(f"\n{label}  ({len(group)})")
        for r in group:
            retired = " ⊘ retracted" if r["retired_at"] else ""
            hits = f" · {r['hits']} hits" if r["hits"] else ""
            src = r["source"]
            origin = f" · from gate {r['approval_id']}" if r["approval_id"] else ""
            print(f"  {_ROLE_ICON[role]} {r['id']} [{r['kind'] or 'any'}] "
                  f"{src}{origin}{hits}{retired}")
            print(f"      {r['rendered'].splitlines()[0]}")
            if r["reason"]:
                print(f"      ↳ {r['reason']}")
            if r["retired_at"]:
                print(f"      ↳ retracted: {r['retired_reason']}")
    failures = data["canary_failures"]
    if failures:
        print(f"\n⚠ {len(failures)} command(s) that MUST gate no longer do:")
        for f in failures:
            print(f"    {f['command'].splitlines()[0]} ({f['kind']}): {f['why']}")
    else:
        print("\n✓ every command that must gate still gates")


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
    elif args.ga_cmd == "rules":
        data = ops.list_gate_rules(role=args.role, kind=args.kind,
                                   include_retired=args.all)
        if args.json:
            _print(data, True)
        else:
            _print_gate_rules(data)
    elif args.ga_cmd == "rule-retract":
        data = ops.retract_gate_rule(args.rule_id, args.reason)
        if args.json:
            _print(data, True)
        else:
            print(f"✓ {data['rule']['id']} retracted — {data['note']}")
            for f in data["canary_failures"]:
                print(f"⚠ {f['command'].splitlines()[0]} ({f['kind']}): {f['why']}")
    elif args.ga_cmd == "explain":
        data = ops.explain_gate(args.command, project_name=args.project)
        if args.json:
            _print(data, True)
        else:
            verdict = (f"GATED as `{data['gate']}`" if data["gated"]
                       else "not gated")
            print(f"{'✋' if data['gated'] else '✓'} {verdict}")
            for line in data["trace"]:
                print(f"    · {line}")
            if data["gated"]:
                print(f"    the literal appears {data['where']}")
                print("    a dismissal of this " + (
                    "COULD be generalised into a standing rule"
                    if data["learnable"] else
                    "could NOT be generalised — the literal is where the shell would "
                    "run it"))
    elif args.ga_cmd in ("approve", "deny", "dismiss"):
        verdict = {"approve": "approved", "deny": "denied",
                   "dismiss": "dismissed"}[args.ga_cmd]
        _print(ops.decide_gate(args.approval_id, verdict=verdict,
                               reason=args.reason, project_name=args.project), args.json)
    return 0


def _backlog_origin(item: dict) -> str:
    """One line saying where a deferred item came from, or "" for one somebody typed.

    Empty for every item filed before deferral routing and for every one a human adds
    without the flags, so those keep rendering exactly as they always have — the origin
    is an addition to the listing, never a change to it.
    """
    bits = []
    if item.get("origin_wo_id"):
        bits.append(f"deferred by {item['origin_wo_id']}")
    if item.get("origin_fo_id"):
        bits.append(f"from {item['origin_fo_id']}")
    if item.get("origin_note"):
        bits.append(f"— {item['origin_note']}")
    return " ".join(bits)


def cmd_backlog(args: argparse.Namespace) -> int:
    from . import ops
    from .central_store import CentralStore
    central = CentralStore()
    try:
        if args.bl_cmd == "add":
            deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
            item = central.add_backlog(args.project, args.title,
                                       description=args.description, depends_on=deps,
                                       origin_wo_id=args.origin_wo,
                                       origin_fo_id=args.origin_fo,
                                       origin_note=args.origin_note)
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
                    origin = _backlog_origin(it)
                    if origin:
                        print(f"    ↳ {origin}")
                if not items:
                    print("backlog empty")
        elif args.bl_cmd == "show":
            item = central.get_backlog(args.item_id)
            if not item:
                print(f"backlog item {args.item_id!r} not found")
                return 1
            if args.json:
                _print(item, True)
            else:
                print(f"{item['id']} [{item['project']}] {item['title']} "
                      f"({item['status']})")
                if item["description"]:
                    print(f"\n{item['description']}")
                if item["depends_on"]:
                    print(f"\ndepends on: {', '.join(item['depends_on'])}")
                if item.get("promoted_wo_id"):
                    print(f"promoted to: {item['promoted_wo_id']}")
                origin = _backlog_origin(item)
                if origin:
                    print(f"\norigin: {origin}")
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
    from .central_store import PINNED_TAG, CentralStore, has_tag, headline, split_tags
    from .ops import OpsError

    def digested(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Index form: enough to decide whether to fetch the entry, not the entry.

        `retired` rides along because `list` is an audit surface that shows retracted
        entries: a headline is short enough to be mistaken for standing advice, so the
        one field that says otherwise has to survive the truncation.
        """
        out = []
        for r in rows:
            row = {"id": r["id"], "project": r["project"] or "global", "topic": r["topic"],
                   "pinned": has_tag(r["tags"], PINNED_TAG),
                   "headline": headline(r["content"])}
            if r.get("retired_at"):
                row["retired"] = r["retired_reason"] or "(no reason recorded)"
            out.append(row)
        return out

    central = CentralStore()
    try:
        if args.kn_cmd == "add":
            tags = split_tags(args.tags)
            if args.pin and PINNED_TAG not in tags:
                tags.append(PINNED_TAG)
            _print(central.add_knowledge(args.content, project=args.project,
                                         topic=args.topic, tags=",".join(tags)), args.json)
        elif args.kn_cmd == "list":
            # An audit surface, so retired entries are listed too — `search_knowledge`
            # is the unfiltered read, and `digested` marks what was retracted so a
            # headline cannot pass for standing advice.
            rows = central.search_knowledge("", limit=args.limit, project=args.project,
                                            topic=args.topic)
            _print(rows if args.full else digested(rows), args.json)
        elif args.kn_cmd == "search":
            rows = central.search_knowledge(args.term, limit=args.limit,
                                            project=args.project, topic=args.topic)
            if not rows and not args.json:
                print(f"no knowledge matching {args.term!r} — "
                      f"try `jarvis learn topics` for what is recorded")
            _print(rows, args.json)
        elif args.kn_cmd == "show":
            rows = [r for r in (central.get_knowledge(i) for i in args.ids) if r]
            missing = set(args.ids) - {r["id"] for r in rows}
            if missing:
                raise OpsError(f"unknown knowledge id(s): {', '.join(sorted(missing))}")
            _print(rows, args.json)
        elif args.kn_cmd == "topics":
            _print([{"topic": t or "(no topic)", "entries": n}
                    for t, n in central.knowledge_topics(args.project)], args.json)
        elif args.kn_cmd in ("pin", "unpin"):
            row = central.pin_knowledge(args.kn_id, pinned=args.kn_cmd == "pin")
            if row is None:
                raise OpsError(f"unknown knowledge id {args.kn_id!r}")
            _print(row, args.json)
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


def cmd_brief(args: argparse.Namespace) -> int:
    """`jarvis brief <section> [--wo <wo-id>]` — one full briefing section, on demand.

    Read-only and total: a worker mid-session fetches these against the index its
    opening prompt carries, so the command must always answer — real ids when it can
    resolve them (the --wo flag, or the env dispatch gave this worker), placeholders
    when it cannot, and never an error for a section that exists.
    """
    import os

    from . import worker_brief

    if not args.section:
        for name in worker_brief.section_names():
            print(f"{name:<12} {worker_brief.SECTION_HOOKS[name]}")
        return 0
    if args.section not in worker_brief.section_names():
        print(f"unknown section {args.section!r} — valid sections: "
              + ", ".join(worker_brief.section_names()), file=sys.stderr)
        return 1

    wo_id = args.wo or os.environ.get("JARVIS_WO_ID") or None
    project = os.environ.get("JARVIS_PROJECT") or None
    if args.wo:
        # Resolve the project from the work order when the OS knows it; a worker
        # holding a valid id must never be failed by a lookup miss.
        try:
            from . import ops
            project, _path, _wo = ops.find_work_order(args.wo)
        except Exception:  # noqa: BLE001 — placeholders are the graceful floor
            pass
    gates_enabled: tuple[str, ...] | None = None
    gates_env = os.environ.get("JARVIS_GATES")
    if gates_env:
        try:
            from .gates import GateConfig
            gates_enabled = tuple(GateConfig.from_json(gates_env).enabled)
        except Exception:  # noqa: BLE001
            gates_enabled = None

    print(worker_brief.render_section(args.section, wo_id=wo_id, project=project,
                                      gates_enabled=gates_enabled))
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
            # Read the deliberation only when it is asked for. Panel opinions are
            # inspectable on demand and never pushed, and that starts here: without
            # `--panel` the document is exactly the question record, so nothing
            # consuming it starts carrying deliberation it never asked to see.
            opinions = neo.opinions(args.question_id) if q and args.panel else []
        finally:
            neo.close()
        if q is None:
            print(f"error: neo question {args.question_id} not found", file=sys.stderr)
            return 1
        # `--json` gets the row exactly as it is stored; the human rendering gets the
        # dashboard digest decoded rather than as one long line of JSON.
        row = q if args.json else _with_readable_digest(q)
        if not args.panel:
            _print(row, args.json)
        elif args.json:
            _print({**q, "panel_opinions": opinions}, True)
        else:
            _print(row, False)
            print("\nPanel deliberation:")
            for o in opinions:
                route = f" route={o['route']}" if o["route"] else ""
                print(f"  {o['seat']:<8} {o['status']:<9} "
                      f"verdict={o['verdict'] or '—':<9} "
                      f"{o['latency_ms']}ms{route}")
            if not opinions:
                print("  no panel ran on this question — Neo answered it single-agent")
    elif args.neo_cmd == "review":
        _print(ops.neo_review(args.question_id, approved=args.correct is None,
                              feedback=args.correct or "", seat=args.seat or ""),
               args.json)
    elif args.neo_cmd == "answer":
        _print(ops.neo_answer_escalated(args.question_id, args.text), args.json)
    elif args.neo_cmd == "learnings":
        # The seat scope is additive and the default is NARROW: no `--seat` shows the
        # global rows alone, exactly as Neo's own single-agent prompt sees them.
        ops.validate_seat(args.seat or "")
        neo = NeoStore()
        try:
            # An audit surface: retired learnings are listed, marked, and shown with the
            # reason they were retired. Neo's PROMPT gets the filtered default.
            rows = neo.learnings(args.project, limit=200, seat=args.seat or "",
                                 include_retired=True)
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
                seat = f" · {r['seat']} seat" if r["seat"] else ""
                print(f"{bullet} #{r['id']} {_stamp(r['ts'])} [{scope}{seat}] "
                      f"({r['source']}) {r['content']}{retired}")
            if not rows:
                print("Neo has no learnings yet — review its answers to teach it")
    elif args.neo_cmd == "export":
        _print(ops.neo_export(), args.json)
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


def cmd_validation(args: argparse.Namespace) -> int:
    """`jarvis validation show <id>` — the panel's whole deliberation on one unit.

    Modelled on `neo show --panel`, including what it does with nothing to show: a unit
    that has never been validated gets a plain sentence saying so, not an empty
    structure. A reader who runs this on a fleet with validation switched off — which is
    every fleet until someone turns it on — is owed an answer, and "no rounds" printed
    as `[]` reads like a bug in the command.
    """
    from . import ops

    view = ops.validation_view(args.unit_id, args.project)
    if args.json:
        _print(view, True)
        return 0

    unit = "feature order" if view["unit"] == "feature" else "work order"
    print(f"{view['id']} [{view['project']}] {view['title']} "
          f"({unit}, {view['status']})")
    if not view["rounds"]:
        print(f"\nno validation has run on this {unit} — the panel judges a unit when "
              f"it submits its evidence, and it ships disabled "
              f"(`os.validation.enabled`)")
    for rnd in view["rounds"]:
        print(f"\n{ops.round_line(rnd)}")
        if rnd["evidence"]:
            print(f"  evidence: {rnd['evidence']}")
        for o in rnd["opinions"]:
            print(f"  {o['seat']:<10} {o['status']:<9} "
                  f"verdict={o['verdict'] or '—':<7} "
                  f"{o['latency_ms']}ms  {o['model'] or '—'}")
            for line in (o["reply"] or "").splitlines():
                print(f"      {line}")
        if not rnd["opinions"]:
            print("  no seat opined on this round")
    # The bus, on the same page as the rounds it carried. Delivered envelopes are
    # routine and live here rather than in any default listing; an UNDELIVERABLE one is
    # a failure, so it is also flagged where nobody has to go looking — `jarvis doctor`
    # and the unit's own page.
    if view["envelopes"]:
        print("\nenvelopes:")
        for e in view["envelopes"]:
            mark = "✗" if e["state"] == "undeliverable" else "·"
            note = f" — {e['note']}" if e["note"] else ""
            print(f"  {mark} {e['kind']} to role {e['to_role']} · {e['state']}"
                  f"{' → ' + e['delivered_wo_id'] if e['delivered_wo_id'] else ''}"
                  f"{note}")
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
        if args.cmd == "cost":
            return cmd_cost(args)
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
        if args.cmd == "brief":
            return cmd_brief(args)
        if args.cmd == "learn":
            return cmd_learn(args)
        if args.cmd == "neo":
            return cmd_neo(args)
        if args.cmd == "validation":
            return cmd_validation(args)
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
