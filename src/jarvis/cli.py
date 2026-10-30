"""jarvis — the agentic OS command line.

Grouped commands:
  jarvis start|stop|status|adopt          OS lifecycle
  jarvis cost [project|wo-id|fo-id]       what the work has cost in tokens
  jarvis inspect <wo-id|fo-id>            where its TIME went, and which cache writes
                                          were a defect rather than the cache expiring
  jarvis watch <wo-id>                    what its turn is doing RIGHT NOW, repainted
  jarvis alarms [project] [--wo|--fo|--source]   findings, newest first: a turn raised
                                          WHILE it burned, or a probe's symptom
  jarvis alarms show|review <al-id>       one alarm, and your verdict on the
                                          supervisor's
  jarvis supervisor probes [project]      what this project is watched for, and where
                                          each answer came from
  jarvis wo create|list|show|send|ask|assume|finish|review|cancel|done|inject
  jarvis fo create|list|show|plan|submit|approve|cancel feature orders (planned sets)
  jarvis gate request|contest|list|show|approve|deny|dismiss  privileged-action
                                          approvals (contest = the match was wrong)
  jarvis gate rules|rule-retract|explain  what counts as privileged, and what the OS
                                          has LEARNED does not
  jarvis rules list|show|retract|dry-run|arm|false-positive  the SELF-HEALING registry:
                                          the gaps the OS recognises in itself, and the
                                          person-only verbs that arm one or call a fire
                                          wrong (not the gate rules)
  jarvis neo list|show|review|answer|learnings|learn|export
  jarvis backlog add|list|show|promote|done
  jarvis learn add|list|search|show|topics|stats|pin|unpin
  jarvis notify / jarvis inbox
  jarvis bug report                       file a Jarvis OS bug (GitHub issue + ping)
  jarvis ui                               web dashboard
  jarvis daemon run                       (internal) foreground daemon
  jarvis _hook                            (internal) Claude Code hook handler
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    else:
        _pretty(data)


def _readable_conversation(detail: dict[str, Any]) -> dict[str, Any]:
    """A work order's document with the empty `void` markers dropped"""
    row = dict(detail)
    turns = row.get("conversation")
    if isinstance(turns, list):
        row["conversation"] = [{k: v for k, v in turn.items()
                                if k != "void" or v} for turn in turns]
    return row


#: How many of a work order's newest events `jarvis wo show` renders by default
WO_SHOW_EVENTS = 200


def _readable_events(detail: dict[str, Any]) -> dict[str, Any]:
    """The event counts as one sentence, and only when something was cut"""
    row = dict(detail)
    row.pop("events_shown", None)
    hidden = row.pop("events_hidden", 0) or 0
    if hidden:
        row["events_hidden"] = (
            f"{hidden} earlier event{'s' if hidden != 1 else ''} not shown — "
            f"`jarvis wo show {row.get('id')} --events 0` renders them all")
    return row


def _readable_preconditions(detail: dict[str, Any]) -> dict[str, Any]:
    """The holds collapsed to one line each — §2.7"""
    row = dict(detail)
    held = row.pop("preconditions", None) or []
    if held:
        row["preconditions"] = [
            p["ref"] + (f" — {p['note']}" if p["note"] else "") for p in held]
    return row


def _readable_rounds(detail: dict[str, Any]) -> dict[str, Any]:
    """A unit's document with its validation rounds collapsed to one line each"""
    from . import ops

    row = dict(detail)
    rounds = row.pop("validation_rounds", None) or []
    if rounds:
        row["validation_rounds"] = [ops.round_line(r) for r in rounds]
    return row


def issue_line(row: dict[str, Any]) -> str:
    """One linked issue, in the one wording every surface that prints one uses"""
    number = f"#{row['number']}" if row.get("number") else row.get("url") or "issue"
    state = f" [{(row.get('state') or '').lower()}]" if row.get("state") else ""
    where = []
    if row.get("round"):
        where.append(f"round {row['round']}")
    if row.get("kind") and row["kind"] != "raised":
        where.append(row["kind"])
    tail = f" ({', '.join(where)})" if where else ""
    return f"{number}{state} {row.get('title') or ''}{tail}".strip()


def _readable_issues(detail: dict[str, Any]) -> dict[str, Any]:
    """The issue index collapsed to one line each, for HUMAN output"""
    row = dict(detail)
    index = row.pop("issues", None) or {}
    raised = [issue_line(i) for i in index.get("raised") or []]
    refs = [issue_line(i) for i in index.get("references") or []]
    # NO ISSUE WAS OPENED FOR THESE and that is what the line has to say
    kept = [f"internal only: {i.get('title') or ''}"
            + (f" ({i['file']})" if i.get("file") else "")
            for i in index.get("withheld") or []]
    if raised:
        row["follow_ups_raised"] = raised
    if refs:
        row["issues_referenced"] = refs
    if kept:
        row["follow_ups_kept"] = kept
    return row


def _readable_spec(detail: dict[str, Any]) -> dict[str, Any]:
    """The spec link as one line, for HUMAN output (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""
    row = dict(detail)
    link = row.pop("spec", None)
    if not link:
        return row
    revision = link.get("source") or "the revision this text came from was not recorded"
    row["spec"] = (f"{link['repo_path']} — {revision} · "
                   f"jarvis fo spec {link['fo_id']}")
    return row


def _finding_lines(opinion: dict[str, Any], filed: dict[str, Any]) -> list[str]:
    """One seat's findings, classified, above its raw reply"""
    from . import ops, validation

    if opinion.get("seat") == "chair":
        return []
    found = validation.findings(opinion)
    lines = [f"blocked: {f['title']}" for f in validation.blockers(found)]
    for f in validation.follow_ups(found):
        item = filed.get(ops.follow_up_key(f["title"]))
        lines.append(f"follow-up {item['url']}: {f['title']}" if item
                     else f"follow-up: {f['title']}")
    return lines


def _readable_self_evolution(detail: dict[str, Any]) -> dict[str, Any]:
    """A fix order's four columns collapsed to one line, for HUMAN output (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
    row = dict(detail)
    state = row.pop("self_evolution", None)
    if state:
        for column in ("gap_class", "detector_id", "remedy_rule_id", "trigger_wo_id"):
            row.pop(column, None)
        row["self_evolution"] = state["line"]
    return row


def _readable_automerge(detail: dict[str, Any]) -> dict[str, Any]:
    """The automatic merge collapsed to its one line, for HUMAN output"""
    row = dict(detail)
    state = row.pop("auto_merge", None)
    if state:
        row["auto_merge"] = state["line"]
    return row


def _readable_harvest(detail: dict[str, Any]) -> dict[str, Any]:
    """The harvest collapsed to its one line, for HUMAN output"""
    row = dict(detail)
    state = row.pop("harvest", None)
    if state:
        row["harvest"] = state["line"]
    return row


def _readable_autoreview(detail: dict[str, Any]) -> dict[str, Any]:
    """The automatic assumption review collapsed to its one line"""
    from . import autoreview, ops

    row = dict(detail)
    state = row.pop("auto_review", None)
    pointer = f" · jarvis validation show {row.get('id')}"

    def gave_up(ruling: dict[str, Any] | None) -> bool:
        return str((ruling or {}).get("code") or "") == autoreview.HELD_PANEL_GAVE_UP

    def held_pending(a: dict[str, Any]) -> bool:
        # A SETTLED row renders its own decision reason
        return str(a.get("status") or "") == "pending" and gave_up(a.get("os_ruling"))

    if state:
        row["auto_review"] = state["line"] + (
            pointer if state.get("kind") == "autoreview_held" and gave_up(state) else "")
    rows = row.get("assumptions") or []
    if rows:
        row["assumptions"] = [
            ops.assumption_line(a) + (pointer if held_pending(a) else "")
            for a in rows]
    return row


def _readable_review(detail: dict[str, Any]) -> dict[str, Any]:
    """The decision this order owes, collapsed to its lines, for HUMAN output (docs/superpowers/specs/2026-09-27-a-review-control-for-an-escalated-round.md §4.)"""
    row = dict(detail)
    state = row.pop("review", None)
    if state:
        from . import ops

        # Spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §5
        row["review"] = ops.review_lines(state)
    return row


def _readable_alarms(detail: dict[str, Any]) -> dict[str, Any]:
    """The `wo_alarms` rows collapsed to `ops.alarm_standing_line`, for HUMAN output"""
    from . import ops

    row = dict(detail)
    line = ops.alarm_standing_line(row.pop("alarms", None) or [])
    if line:
        row["alarms"] = line
    return row


def time_in_state_lines(payload: dict[str, Any]) -> list[str]:
    """One line per status this order was in (docs/superpowers/specs/2026-09-27-time-in-each-state.md)"""
    from . import ops

    lines: list[str] = []
    # Said ONCE, before the rows, and in `ops`' words: a coarse span nothing labels is read as a...
    if payload.get("approximate"):
        lines.append(f"({ops.FO_APPROXIMATE_NOTE})")
    held_now = payload.get("held_now")
    for total in payload.get("totals") or []:
        entries = total["entries"]
        word = "entry" if entries == 1 else "entries"
        held = float(total.get("held_seconds") or 0.0)
        current = bool(total["status"]) and total["status"] == payload.get(
            "current_status")
        # The HEADLINE is active and wall is never deleted (Neo 1130, spec §3)
        duration = total["active_human"] if held else total["seconds_human"]
        line = (f"{STATUS_ICON.get(total['status'], '•')} {total['status']:<18} "
                f"{duration:>7}  {entries} {word:<8} "
                f"{total['share'] * 100:>3.0f}%")
        if held and not (current and held_now):
            breakdown = ((payload.get("current_status_held_by") if current
                          else total.get("held_by")) or [])
            line += (f"   ({total['held_human']} held by {_held_phrase(breakdown)}, "
                     f"{total['seconds_human']} wall)")
        if current and held_now:
            line += (f"   ← HELD {held_now['seconds_human']} by {held_now['phrase']}, "
                     f"running {payload['current_status_active_age_human']} of "
                     f"{payload['current_status_age_human']}")
        elif current:
            idle = payload.get("last_activity_age_human")
            since = (f"nothing since {idle} ago" if idle
                     else "nothing on the record at all")
            line += f"   ← now, {payload['current_status_age_human']}, {since}"
        lines.append(line)
    return lines


def _held_phrase(held_by: list[dict[str, Any]]) -> str:
    """"a fleet usage limit and 2 others" — `holds.Hold.phrase` verbatim, biggest first"""
    if not held_by:
        return "a hold"
    others = len(held_by) - 1
    if not others:
        return str(held_by[0]["phrase"])
    return (f"{held_by[0]['phrase']} and {others} "
            f"{'other' if others == 1 else 'others'}")


def _readable_time_in_state(detail: dict[str, Any]) -> dict[str, Any]:
    """The status-span payload collapsed to `time_in_state_lines`, for HUMAN output"""
    row = dict(detail)
    payload = row.pop("time_in_state", None) or {}
    lines = time_in_state_lines(payload)
    if lines:
        row["time in state"] = lines
    return row


def _readable_config(detail: dict[str, Any]) -> dict[str, Any]:
    """`config_version: cfg-a1b2…` replaced by `config: cfg-a1b2… (3 versions since)`"""
    from . import ops

    line = ops.config_version_line(detail.get("config_version"))
    return {("config" if k == "config_version" else k):
            (line if k == "config_version" else v)
            for k, v in detail.items()}


def _with_readable_digest(q: dict[str, Any]) -> dict[str, Any]:
    """A Neo question row with its dashboard digest decoded, for HUMAN output only"""
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
    from . import db  # lazily, like every other sibling this module reaches for

    if not ts:
        return "-"
    delta = db.now() - ts
    if delta < 90:
        return f"{int(delta)}s"
    if delta < 5400:
        return f"{int(delta / 60)}m"
    if delta < 129600:
        return f"{delta / 3600:.1f}h"
    return f"{delta / 86400:.1f}d"


def _stamp(ts: float | None) -> str:
    """An absolute local timestamp, for records read as a ledger rather than a feed"""
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


STATUS_ICON = {
    "pending": "⏳", "dispatching": "🚀", "running": "🟢",
    "idle": "💤", "waiting_input": "🙋", "budget_exhausted": "💸",
    "needs_review": "👀", "waiting_pr_merge": "🔀", "completed": "✅", "failed": "❌",
    "cancelled": "🚫",
}
# What the user is most likely to act on
LIST_PRIORITY = {"running": 0, "waiting_pr_merge": 1}
ORIGIN_BADGE = {"jarvis": "🤖 jarvis", "ui": "🖥 ui", "manual": "⚠ manual",
                "adhoc": "⚠ ad-hoc", "injected": "🔗 injected", "neo": "🧠 neo",
                "schedule": "⏰ scheduled"}


# Prose out of argv: the worktree guard reads a quoted argument as a command (issue 1209)
_PROSE_FILE_HELP = ("read this text from a file instead of the command line, or - for "
                    "stdin. A file rather than an argument on purpose: prose whose "
                    "first word names a binary is refused before jarvis runs")
_PROSE_FILE_SLOTS = {
    "question_file": "question",
    "content_file": "content",
    "message_file": "message",
    "summary_file": "summary",
    "feedback_file": "feedback",
    "why_file": "why",
    "evidence_file": "evidence",
}
# The slots whose argparse requirement this mechanism relaxed; one of the two must arrive
_PROSE_SLOT_REQUIRED = {"question", "content", "message", "summary"}


def fill_prose_from_files(args: argparse.Namespace) -> None:
    """Resolve every --<slot>-file into its slot, before anything reads the slot"""
    from .ops import OpsError
    from_stdin = ""
    for opt, slot in _PROSE_FILE_SLOTS.items():
        if not hasattr(args, opt):
            continue
        flag = "--" + opt.replace("_", "-")
        path = getattr(args, opt) or ""
        if not path:
            if slot in _PROSE_SLOT_REQUIRED and not getattr(args, slot, None):
                raise OpsError(f"pass the {slot} as an argument or with {flag} PATH")
            continue
        if getattr(args, slot, None):
            raise OpsError(f"the {slot} was given twice: as an argument and with "
                               f"{flag}")
        if path == "-":
            if from_stdin:
                raise OpsError(f"only one argument can be read from stdin, and "
                                   f"{from_stdin} already is")
            from_stdin = flag
            text = sys.stdin.read().strip()
        else:
            file = Path(path)
            if not file.is_file():
                raise OpsError(f"no such {slot} file: {file}")
            text = file.read_text().strip()
        if not text:
            raise OpsError(f"{flag} {path} is empty, so there is no {slot} to record")
        # The word cap lives in the PreToolUse hook, which sees argv only
        if slot == "summary" and os.environ.get("JARVIS_WO_ID"):
            from . import concision
            cap, words = concision.summary_cap(), concision.word_count(text)
            if cap and words > cap:
                raise OpsError(f"this {flag} is {words} words; the cap is {cap}. The "
                               f"summary is the HEADLINE — the detail belongs in the "
                               f"final message of your turn")
        setattr(args, slot, text)


class _VersionAction(argparse.Action):
    """`jarvis --version` — resolved lazily so hooks don't pay for dist metadata"""

    def __call__(self, parser, _namespace=None, _values=None, _option_string=None):  # noqa: ANN001
        from .bugreport import jarvis_version
        print(f"jarvis {jarvis_version()}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    from .search import KINDS as SEARCH_KINDS

    # Imported HERE rather than at module scope: `jarvis --help` is the one path that needs the rubric
    from .issues import PRIORITIES as _PRIORITIES
    from .issues import PRIORITY_RUBRIC as _PRIORITY_RUBRIC

    # A leaf module with no store or CLI dependency
    from . import catalog
    from .project_store import ALARM_SOURCES

    p = argparse.ArgumentParser(prog="jarvis", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    # Which release am I on? install.sh checks this against the tag it installed
    p.add_argument("-V", "--version", nargs=0, action=_VersionAction,
                   help="print the installed Jarvis OS version")
    sub = p.add_subparsers(dest="cmd", required=True)

    # start / stop / status
    sp = sub.add_parser("start", help="start the OS: bootstrap projects + run the daemon")
    sp.add_argument("--catalog", required=True, help="path to the project catalog JSON")
    sp.add_argument("--force-config", action="store_true",
                    help="overwrite manually-edited injected settings, and a committed "
                         "OPERATION.md the generator would otherwise leave alone")
    sp.add_argument("--foreground", action="store_true", help="run the daemon in-process")
    sp.add_argument("--poll-interval", type=float, default=5.0)

    sub.add_parser("stop", help="stop the daemon")

    sp = sub.add_parser(
        "pause", help="stop the fleet starting turns (only allow-listed orders run)",
        description="The user's brake (issue #843). No work order starts a turn — "
                    "dispatch, paused-turn resume, message delivery, validation — "
                    "unless it is allow-listed. Turns in flight finish; nothing is "
                    "killed. Re-running replaces the allow-list.")
    sp.add_argument("--allow", default="",
                    help="comma-separated work/feature order ids that may keep running")
    sp.add_argument("--reason", default="", help="why, shown on every held order")

    sp = sub.add_parser(
        "resume", help="let orders through a pause, or lift it with --all")
    sp.add_argument("order_ids", nargs="*", help="work/feature order ids to let through")
    sp.add_argument("--all", action="store_true", dest="everything",
                    help="lift the pause for the whole fleet")

    sp = sub.add_parser("status", help="whole-OS status; flags what needs your attention")
    sp.add_argument("--attention", action="store_true", help="only show attention items")

    sp = sub.add_parser("doctor", help="check the OS's own post-conditions (invariants)")
    sp.add_argument("project", nargs="?", help="one project (default: the whole fleet)")
    sp.add_argument("--repair", action="store_true",
                    help="apply the repairs instead of only reporting them")
    sp.add_argument("--skip-os", action="store_true", dest="skip_os",
                    help="check the project(s) only, not the OS itself — what the "
                         "scheduled daily run passes in every project but the one that "
                         "owns the install, so one broken dashboard is reported once")
    sp.add_argument("--catalog", help="catalog to read the fleet from")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("cost", help="what the fleet's work has cost in tokens")
    sp.add_argument("target", nargs="?",
                    help="a project, a work-order id, or a feature-order id "
                         "(default: the whole fleet)")
    sp.add_argument("--limit", type=int, default=50,
                    help="work orders per project to measure (default: 50)")
    # The DISTRIBUTION, beside the listing rather than instead of it: what a typical order costs (docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md)
    sp.add_argument("--fleet", action="store_true",
                    help="what a TYPICAL order costs: n, avg, p90 and max per metric "
                         "over a window, plus what Jarvis itself spent by kind and "
                         "whether the compactions paid off")
    sp.add_argument("--since", type=_when,
                    help="start of the window, a date or ISO datetime "
                         "(default: this Claude usage week; naive = UTC)")
    sp.add_argument("--until", type=_when, help="end of the window, exclusive")
    # The window SELECTOR, beside the raw range rather than instead of it (docs/superpowers/specs/2026-10-07-cost-window-selector.md)
    sp.add_argument("--window", choices=["week", "5h"],
                    help="which window to report over: the Claude usage week, or one 5h "
                         "SLICE of it anchored on the weekly reset (nothing records "
                         "Claude's own session boundary, so a 5h window is not a claim "
                         "about its session accounting)")
    sp.add_argument("--offset", type=int, default=0,
                    help="step back whole windows: 0 is the current one, -1 the "
                         "previous (--fleet only; 0 or negative)")
    # §11 of the same spec: DISPLAY only, so the CLI label and the page label agree
    sp.add_argument("--tz", help="IANA time zone to DISPLAY the window in (default: the "
                                 "catalog's cost.week_reset_zone). No boundary moves, "
                                 "and --since/--until are still read as UTC when naive")
    sp.add_argument("--project", help="one project instead of the whole fleet "
                                      "(--fleet only)")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "inspect",
        help="where an order's TIME went: generating, blocked or running tools — and "
             "which cache writes were a defect rather than the cache expiring",
    )
    sp.add_argument("target", help="a work-order id or a feature-order id")
    sp.add_argument("--project")
    sp.add_argument("--writes-over", type=int, metavar="TOKENS",
                    help="classify cache writes at or above this size, for this run "
                         "only (default: the project's os.inspect.report_write_floor, "
                         f"{catalog.DEFAULT_INSPECT_REPORT_WRITE_FLOOR:,})")
    sp.add_argument("--joins-over", type=int, metavar="SECONDS",
                    help="list blocking joins at or above this long, for this run only "
                         "(default: the project's os.inspect.report_join_floor, "
                         f"{catalog.DEFAULT_INSPECT_REPORT_JOIN_FLOOR})")
    # OFF BY DEFAULT: a tool input can be a whole file
    sp.add_argument("--params", action="store_true",
                    help="also print each turn's tool calls with their parameters, "
                         "and the caps those were truncated at")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "navigation",
        help="how the fleet NAVIGATED code — symbol calls against shell reads, by "
             "side, as shares of read volume",
    )
    sp.add_argument("target", nargs="?",
                    help="a work-order id, a feature-order id, or a project name")
    sp.add_argument("--project")
    # OPT-IN, because the wide walk is 2.7G and 11,889 lead transcripts: a no-argument run is refused...
    sp.add_argument("--fleet", action="store_true",
                    help="every project's transcripts, within --days")
    sp.add_argument("--days", type=int, metavar="N",
                    help="only transcripts modified in the last N days, for a project "
                         "or --fleet; N must be positive (default: the project's "
                         "os.navigation.window_days, "
                         f"{catalog.DEFAULT_NAVIGATION_WINDOW_DAYS})")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "watch",
        help="what a work order's turn is doing RIGHT NOW: the tool in flight and how "
             "long it has been running — or, when nothing has been written, since when",
    )
    sp.add_argument("target", help="a work-order id")
    sp.add_argument("--project")
    sp.add_argument("--interval", type=float, default=2.0,
                    help="seconds between repaints (default: 2.0)")
    sp.add_argument("--once", action="store_true",
                    help="print one frame and exit, instead of repainting")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "search",
        help="find any artifact — work orders, feature orders, Neo questions, alarms, "
             "gates, backlog items, knowledge — across the fleet or one project",
    )
    sp.add_argument("query", help="words to look for, or an id to jump to")
    sp.add_argument("--project", help="one project (default: the whole fleet)")
    sp.add_argument("--kind", action="append", choices=list(SEARCH_KINDS),
                    help="restrict to one kind; repeatable")
    sp.add_argument("--limit", type=int, default=30, help="hits to show (default: 30)")
    sp.add_argument("--json", action="store_true")

    # §4.3 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md: these three spellings are a...
    spec_p = sub.add_parser(
        "spec",
        help="read a markdown document the way code is read — list its sections, open "
             "one, or find which section says a thing",
    ).add_subparsers(dest="spec_cmd", required=True)

    sp = spec_p.add_parser("toc", help="every heading of one document, with an ESTIMATE "
                                       "of each section's size")
    sp.add_argument("path", help="a path inside the project (a relative one is read "
                                 "from the current directory, as the shell reads it)")
    sp.add_argument("--project", help="one project, by name (default: the one owning the "
                                      "current directory)")
    sp.add_argument("--json", action="store_true")

    sp = spec_p.add_parser("section", help="one section of one document, by number or by "
                                           "heading substring")
    sp.add_argument("path", help="a path inside the project (a relative one is read "
                                 "from the current directory, as the shell reads it)")
    sp.add_argument("which", help="a section number (`4`, `4.2`) or part of its heading")
    sp.add_argument("--project", help="one project, by name (default: the one owning the "
                                      "current directory)")
    sp.add_argument("--json", action="store_true")

    sp = spec_p.add_parser(
        "search",
        help="which SECTIONS of this project's documents contain every word — the next "
             "step is `jarvis spec section`, and each hit prints it",
        description="Every whitespace-separated word must appear in a line, "
                    "case-insensitively, and the SECTION containing it is reported once. "
                    "One project at a time: there is no --all and no comma-separated "
                    "list. KEEP SHELL METACHARACTERS OUT OF THE QUERY: a command "
                    "containing | ; ` $ < or > is refused by "
                    "hooks.is_jarvis_command_chain, so it stops being auto-allowed and "
                    "prompts instead — which stalls a background worker.",
    )
    sp.add_argument("words", help="the words to look for, quoted")
    sp.add_argument("--project", help="one project, by name (default: the one owning the "
                                      "current directory)")
    sp.add_argument("--limit", type=int, default=40, help="hits to show (default: 40)")
    sp.add_argument("--json", action="store_true")

    # `jarvis issues [project]` keeps working verbatim: `_normalise_issues` inserts the implicit `list`
    issues_p = sub.add_parser(
        "issues",
        help="tracker issues the fleet keeps running into, most-referenced first",
    ).add_subparsers(dest="issues_cmd", required=True)

    sp = issues_p.add_parser("list", help="every linked issue, most-referenced first "
                                          "(the default)")
    sp.add_argument("project", nargs="?", help="one project (default: the whole fleet)")
    sp.add_argument("--limit", type=int, default=50, help="issues to show (default: 50)")
    sp.add_argument("--json", action="store_true")

    sp = issues_p.add_parser(
        "start", help="open a work order on a tracker issue — the route a bug that was "
                      "not dispatched takes")
    sp.add_argument("issue", help="an issue URL, `#123` or `123`")
    sp.add_argument("--project", help="whose tracker the issue is on (default: the "
                                      "project whose git `origin` is the OS's own "
                                      "bug repository)")
    sp.add_argument("--json", action="store_true")

    # docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md
    steward = sub.add_parser(
        "steward",
        help="what the OS may spend on itself: the ledger, and every decision with its rule",
    ).add_subparsers(dest="steward_cmd", required=True)
    sp = steward.add_parser("ledger", help="this week's evolution spend against the cap")
    sp.add_argument("--json", action="store_true")
    sp = steward.add_parser("decisions", help="every decision, newest first")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--json", action="store_true")
    sp = steward.add_parser("why", help="the decisions that led to, or came from, one order")
    sp.add_argument("wo_id")
    sp.add_argument("--json", action="store_true")
    sp = steward.add_parser("digest", help="the weekly digest, now (sends nothing)")

    # `jarvis alarms` and `jarvis alarms <project>` are the two spellings that already exist (docs/superpowers/specs/2026-08-31-the-supervisor.md)
    alarms = sub.add_parser(
        "alarms",
        help="turns the OS raised WHILE they were still costing money, newest first",
    ).add_subparsers(dest="alarms_cmd", required=True)

    sp = alarms.add_parser("list", help="every alarm, newest first (the default)")
    sp.add_argument("project", nargs="?", help="one project (default: the whole fleet)")
    sp.add_argument("--limit", type=int, default=50,
                    help="alarms to show (default: 50)")
    sp.add_argument("--wo", help="one work order's alarms, with their ids")
    sp.add_argument("--fo", help="one feature order's findings, however they were "
                                "carried, with their ids")
    sp.add_argument("--source", choices=ALARM_SOURCES,
                    help="cost (a burning turn) or health (a probe found a symptom)")
    sp.add_argument("--json", action="store_true")

    sp = alarms.add_parser("show", help="one alarm: what fired, and what came of it")
    sp.add_argument("alarm_id", help="an al-… id (from `jarvis alarms --wo <wo-id>`)")
    sp.add_argument("--project")
    sp.add_argument("--json", action="store_true")

    sp = alarms.add_parser(
        "review", help="approve or correct the supervisor's verdict on one alarm")
    sp.add_argument("alarm_id")
    sp.add_argument("--reject", action="store_true",
                    help="the supervisor got it wrong; --feedback says what it should "
                         "have decided")
    sp.add_argument("--feedback", default="", help="required with --reject")
    sp.add_argument("--project")
    sp.add_argument("--json", action="store_true")

    # Morpheus: production validation of shipped PRs (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)
    mo = sub.add_parser(
        "morpheus", help="Morpheus: every shipped PR watched in production until validated",
    ).add_subparsers(dest="morpheus_cmd", required=True)
    m = mo.add_parser("status", help="counts by state, the session, then every PR")
    m.add_argument("--all", action="store_true", help="list settled PRs too")
    m = mo.add_parser("show", help="one PR: its hints, script, last output and action")
    m.add_argument("item_id")
    m = mo.add_parser("enqueue-release",
                      help="queue every PR a release carries (shipit runs this)")
    m.add_argument("tag", help="the release tag, e.g. jarvis-0.9.3")
    m.add_argument("--from", dest="from_ref",
                   help="the previous release's ref (default: the tag before TAG)")
    m.add_argument("--to", dest="to_ref", help="the shipped commit (default: TAG)")
    m.add_argument("--repo", help="the git checkout to read (default: production's)")
    m.add_argument("--project", help="the onboarded project the release is of (default: "
                                     "the one whose origin is --repo, else the OS)")
    m = mo.add_parser("add", help="queue one PR by number or URL, outside a release")
    m.add_argument("pr")
    m.add_argument("--repo", help="the git checkout whose origin owns a bare PR number")
    m.add_argument("--release", default="", help="the release to file it under")
    m.add_argument("--project", help="the onboarded project the PR is of (default: "
                                     "matched by the PR's repository)")
    m = mo.add_parser("script", help="(Morpheus's subagents) give a PR its validation "
                                     "script; a queued PR starts being monitored")
    m.add_argument("item_id")
    m.add_argument("--file", required=True, help="the script, or - for stdin")
    m.add_argument("--source", default="agent", choices=["agent", "user"])
    m = mo.add_parser("check", help="run a PR's script now, the way the daemon would")
    m.add_argument("item_id")
    m = mo.add_parser("waive", help="rule a PR validated yourself")
    m.add_argument("item_id")
    m.add_argument("--reason", required=True)
    m = mo.add_parser("requeue", help="watch a settled PR again with a fresh deadline")
    m.add_argument("item_id")
    m = mo.add_parser("rewrite", help="rule the check script wrong, not the PR: Morpheus "
                                      "writes a new one, then the OS watches it again")
    m.add_argument("item_id")
    m.add_argument("--why", required=True,
                   help="what the script gets wrong and what the right check is")
    m = mo.add_parser("confirm", help="rule a failed check real and file the work order "
                                      "that fixes it")
    m.add_argument("item_id")
    m.add_argument("--fix", required=True, help="what needs fixing: the order's description")
    m = mo.add_parser("pause", help="hold Morpheus's session turns, whatever the fleet does "
                                    "(checks still run; they spend nothing)")
    m.add_argument("--reason", required=True)
    m = mo.add_parser("unpause", help="let Morpheus's session turns run, even through a "
                                      "fleet `jarvis pause`")
    m.add_argument("--reason", required=True)
    mo.add_parser("follow-fleet", help="drop the pause/unpause pin: Morpheus pauses with "
                                       "the fleet again")
    for m in mo.choices.values():
        m.add_argument("--json", action="store_true", help="machine-readable output")

    # §7 of docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets- investigated.md
    sp = sub.add_parser(
        "stuck", help="open orders judged on time in status, most overdue first")
    sp.add_argument("project", nargs="?", help="one project (default: the whole fleet)")
    sp.add_argument("--limit", type=int, default=50, help="rows to show (default: 50)")
    sp.add_argument("--json", action="store_true")

    # the supervisor's own settings, as opposed to what it decided (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §2.)
    sup = sub.add_parser(
        "supervisor", help="what the supervisor watches for, and how it is configured",
    ).add_subparsers(dest="supervisor_cmd", required=True)
    sp = sup.add_parser(
        "probes", help="the health probes in force, and where each one came from")
    sp.add_argument("project", nargs="?",
                    help="one project (default: the fleet's own list)")
    sp.add_argument("--catalog", help="read this catalog instead of the registered one")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("adopt", help="make a project OS-ready (README, OPERATION.md, "
                                      "settings); to choose its settings in an interview "
                                      "instead, `jarvis onboard start <path>`")
    sp.add_argument("path", help="project directory")
    sp.add_argument("--name", help="project name (default: directory name)")
    sp.add_argument("--catalog", help="catalog to take overrides/defaults from")
    sp.add_argument("--force-config", action="store_true",
                    help="overwrite manually-edited injected settings, and a committed "
                         "OPERATION.md the generator would otherwise leave alone")
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("offboard", help="stop the OS doing anything for a project — "
                                         "archive its record, cancel its orders, and "
                                         "remove the OS's files from its repo")
    sp.add_argument("name", help="project name")
    sp.add_argument("--purge", action="store_true",
                    help="delete its record and <project>/.jarvis/ too (needs --yes)")
    sp.add_argument("--yes", action="store_true", help="confirm --purge")
    sp.add_argument("--force", action="store_true", help="cancel its open orders")
    sp.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    sp.add_argument("--reason", default="", help="goes on the config version row")
    sp.add_argument("--catalog", help="rewrite this catalog instead of the registered one")
    sp.add_argument("--json", action="store_true")

    # work orders
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
    # §2.3 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md
    c.add_argument("--blocked-by", action="append", default=None, metavar="REF",
                   help="something that is NOT a work order COMPLETING and must clear "
                        "before this one is dispatched (`deployed:<wo-id|sha>`, `bl-…`, "
                        "`kn-…`, `io-…`, `#123`, a URL, or the words for the wait). "
                        "Repeatable. It becomes a row the claim SQL evaluates, so the "
                        "order stays `pending` and costs no worker turn. The OS clears "
                        "two kinds itself: a `bl-` ref once the backlog item settles, "
                        "and a `deployed:` ref once a release carrying that commit is "
                        "what production RUNS — which `--depends-on` can never say, "
                        "since a dependency completes at merge. `jarvis wo unblock` "
                        "clears the rest")
    c.add_argument("--parent", default="",
                   help="file it UNDER this feature order: it becomes one of the "
                        "feature's children, so the feature waits for it and shows it "
                        "in its tree (the feature must still be open)")
    c.add_argument("--budget", metavar="USD", help="cap this order's spend at N dollars. It governs the WHOLE bill `jarvis cost` reports — the worker's turns plus what Jarvis spends on it (Neo, the validation panel) — and every turn is launched with no more than what is left. At the cap the order stops in `budget_exhausted` and asks you; raise it with `jarvis wo budget` and it carries on in the same session. Omit for no ceiling, which is the default and what the OS has always done")

    c.add_argument("--observability", choices=["off", "normal", "full"],
                   help="how much debug data Jarvis COLLECTS for this order (default: the project's setting, normally `normal`). The three levels: "
                        "`off` — no per-turn context ledger and no sealed autopsy. It does NOT disable `jarvis watch`, `jarvis inspect`, `jarvis wo why` or the debug page: those read files that already exist, so the autopsy READING — every turn, its tools, its token classes and its context total, delta, peak and composition — is shown for every order at every level. What `off` stops is the autopsy being SEALED, so that reading survives only as long as Claude Code keeps the transcript. "
                        "`normal` — the default: the per-turn context ledger, and the autopsy sealed onto the order when it settles. "
                        "`full` — that, plus the detail a `full` seal retains: verbatim tool parameters and nested subagent anatomies")

    # WHAT MARKS THIS A FIX ORDER — §7.1 of docs/superpowers/specs/2026-09-27-self-evolution.md
    c.add_argument("--gap-class", default="",
                   help="the class of gap this order closes (a slug, e.g. "
                        "`stale-hold`). Marks it a FIX ORDER: before it may be submitted "
                        "for validation, its detector and remedy must be registered and "
                        "the detector must be shown to fire on --trigger-wo-id. Omit for "
                        "an ordinary work order")
    c.add_argument("--detector-id", default="",
                   help="the detector that recognises that gap — a `detectors.id`, or "
                        "the id a detector is being proposed under. Not yet registered "
                        "is the NORMAL state of a fresh fix order")
    c.add_argument("--remedy-rule-id", default="",
                   help="the remedy rule under that detector — a `remedy_rules.id`. Omit "
                        "to accept any live remedy the detector has")
    c.add_argument("--trigger-wo-id", default="",
                   help="the work order the investigation was filed FROM: what the "
                        "detector's condition is proved against. Read FIRST and, when "
                        "set, the whole answer — nothing infers over it")

    b = wo.add_parser("budget", help="show, set, raise or clear a work order's dollar "
                                     "ceiling — and resume it if it stopped at one")
    b.add_argument("wo_id")
    b.add_argument("amount", nargs="?", metavar="USD",
                   help="the new ceiling, in dollars ('5', '5.50', '$5.50'). Omit to "
                        "just show what the ceiling is and what has gone against it")
    b.add_argument("--clear", action="store_true",
                   help="remove the ceiling: the order runs uncapped again")
    b.add_argument("--project")

    # Both kinds of hold, one command (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)
    ub = wo.add_parser("unblock", help="cut what is holding a work order back — "
                                       "dependency edges and `--blocked-by` "
                                       "preconditions (by default only the ones that "
                                       "can never clear)")
    ub.add_argument("wo_id")
    ub.add_argument("--project")
    ub.add_argument("--all", action="store_true", dest="drop_all",
                    help="cut every remaining hold, including dependencies still "
                         "running and a precondition whose backlog item is still open")

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
    s.add_argument("--events", type=int, default=WO_SHOW_EVENTS, metavar="N",
                   help=f"how many of the NEWEST events the timeline renders (default "
                        f"{WO_SHOW_EVENTS}); 0 renders every one")

    # §9.2 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md
    m = wo.add_parser("send", help="send feedback to the worker handling a work order — "
                                   "on a failed order this also revives the session; "
                                   "`wo retry` is the named form")
    m.add_argument("wo_id")
    m.add_argument("message", nargs="?")
    m.add_argument("--message-file", dest="message_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
    m.add_argument("--project")
    m.add_argument("--source", default="jarvis", choices=["jarvis", "ui", "direct"])

    a = wo.add_parser("assume", help="(workers) record an assumption for user review")
    a.add_argument("wo_id")
    a.add_argument("content", nargs="?")
    a.add_argument("--content-file", dest="content_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)

    q = wo.add_parser("ask", help="(workers) ask a question — Neo answers as the user")
    q.add_argument("wo_id")
    q.add_argument("question", nargs="?")
    q.add_argument("--question-file", dest="question_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
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
    f.add_argument("--summary", default="")
    f.add_argument("--summary-file", dest="summary_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
    f.add_argument("--pr", default="", metavar="URL",
                   help="the pull request this work order opened — parks it in "
                        "'waiting for PR merge' instead of completing it, so it stays "
                        "on the open list until the user merges")
    f.add_argument("--evidence", default="", metavar="TEXT",
                   help="how you tested this: what you ran and what it said. An "
                        "independent reviewer reads it beside your diff")
    f.add_argument("--abandon", default="", metavar="WHY",
                   help="this work order wrote code that is deliberately NOT being "
                        "landed, and this is why. Without it, finishing over commits "
                        "that no pull request covers is refused (GitHub issue #232)")
    f.add_argument("--superseded-by", dest="superseded_by", default="", metavar="WO_ID",
                   help="another order carries this one's work (a duplicate): links "
                        "follow it, e.g. Morpheus re-watches a confirmed failure when "
                        "THAT order's fix reaches production")
    f.add_argument("--needs-user", dest="needs_user", default="", metavar="COMMAND",
                   help="the exact command only the user may run that the rest of "
                        "this work waits on (e.g. `jarvis config set …`): a "
                        "submission that changes nothing escalates naming it")

    r = wo.add_parser("review", help="accept/reject a work order's pending assumptions")
    r.add_argument("wo_id")
    r.add_argument("--reject", action="store_true")
    r.add_argument("--feedback", default="",
                   help="your reasoning — teaches Neo, and on --reject is delivered "
                        "to the worker as guidance")
    r.add_argument("--feedback-file", dest="feedback_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
    r.add_argument("--false-alarm", action="store_true",
                   help="the high-stakes net was WRONG to hold these: record a defect "
                        "in the net that fired (also read from 'false alarm' in "
                        "--feedback)")

    x = wo.add_parser("cancel", help="cancel a work order")
    x.add_argument("wo_id")

    dn = wo.add_parser("done", help="close a work order yourself — the work is "
                                    "finished; stops the worker if one is still running")
    dn.add_argument("wo_id")
    dn.add_argument("--project")

    # docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §8
    pr = wo.add_parser("pr", help="record the pull request you opened for a work order "
                                  "whose branch is pushed — it goes back in the merge "
                                  "queue and the OS watches it merge")
    pr.add_argument("wo_id")
    pr.add_argument("url")
    pr.add_argument("--project")

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

    cx = wo.add_parser("context",
                       help="what Jarvis put in this work order's context window, turn "
                            "by turn: the ingredient sizes (estimated), the inferred "
                            "residual, and what changed since the previous turn")
    cx.add_argument("wo_id")
    cx.add_argument("--project")
    cx.add_argument("--turn", type=int, metavar="N",
                    help="one turn only, by its sequence number")
    cx.add_argument("--json", action="store_true")

    ra = wo.add_parser("resume-auto",
                       help="say what a work order is really waiting on, and unstick it "
                            "if it is a permission prompt (nothing else can be)")
    ra.add_argument("wo_id")
    ra.add_argument("--project")
    ra.add_argument("--force", action="store_true",
                    help="send the nudge even when nothing is stuck — it costs a full "
                         "re-send of the worker's conversation")

    # §9.1 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md
    rt = wo.add_parser("retry", help="relaunch a FAILED work order in its own session — "
                                     "the named form of `wo send`'s revive. Nothing "
                                     "automatic: a turn that died with no result is "
                                     "never replayed by the OS")
    rt.add_argument("wo_id")
    rt.add_argument("--message", help="what to tell the worker; omitted sends the OS's "
                                      "own relaunch note, unattributed")
    rt.add_argument("--model", help="relaunch on this model id instead (e.g. after "
                                    "claude rejected the order's own)")
    rt.add_argument("--project")

    # docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md
    rs = wo.add_parser("respawn", help="restart a background-transport order's own session "
                                       "process under the SAME id (`claude respawn`), e.g. "
                                       "after a Claude Code upgrade; sends nothing")
    rs.add_argument("wo_id")
    rs.add_argument("--project")

    wy = wo.add_parser("why", help="why is this order not moving: what it waits for, "
                                   "what has held it, what the OS spent on it, and the "
                                   "commands that would be accepted right now")
    wy.add_argument("wo_id")
    wy.add_argument("--project")

    fx = wo.add_parser("fix", help="clear the blocker `jarvis wo why` just named: the OS "
                                   "matches it against the remedies it has, and says what "
                                   "one of them would do. Nothing is done without "
                                   "--confirm, and a confirmed fix is filed for a "
                                   "reviewer to approve before the OS acts")
    fx.add_argument("wo_id")
    fx.add_argument("--project")
    fx.add_argument("--remedy", help="name the remedy instead of letting the blocker "
                                     "choose it — `file_work_order` is reachable only "
                                     "this way, and only with --argument")
    fx.add_argument("--argument", help="what the remedy is asked to do: the message a "
                                      "nudge carries, or the title and brief of the work "
                                      "order `file_work_order` files")
    fx.add_argument("--confirm", action="store_true",
                    help="file it. A reviewer decides, and the OS acts only after the "
                         "gate opens")
    fx.add_argument("--json", action="store_true")

    # feature orders ------------------------------------------------------------------- Parallel to `wo`...
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
    f.add_argument("--budget", metavar="USD", help='cap the WHOLE FEATURE at N dollars — its planner, its manager and every child together, the same total `jarvis cost <fo-id>` adds up. Each child takes a slice of what is left when it is dispatched, so two running at once cannot both spend the same remainder. Omit for no ceiling')

    f = fo.add_parser("budget", help="show, set, raise or clear a feature's FAMILY "
                                     "budget — the ceiling on its whole rollup")
    f.add_argument("fo_id")
    f.add_argument("amount", nargs="?", metavar="USD",
                   help="the new ceiling, in dollars. Omit to just show it")
    f.add_argument("--clear", action="store_true", help="remove the ceiling")
    f.add_argument("--project")

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

    f = fo.add_parser("agent", help="rebuild this feature's agent type from its spec")
    f.add_argument("fo_id")
    f.add_argument("--project")

    f = fo.add_parser("spec", help="print the design document this feature holds — the "
                                   "snapshot every reviewer and every child was given, "
                                   "which exists before any pull request does")
    f.add_argument("fo_id")
    f.add_argument("--section", default="",
                   help="one section only, by number or by heading substring — the same "
                        "reference a child's brief names")
    f.add_argument("--project")

    f = fo.add_parser("resume", help="put a FAILED feature order back to work — the way "
                                     "past a dead child, without touching the database")
    f.add_argument("fo_id")
    f.add_argument("--fix", default="",
                   help="what still needs doing, in your own words. Filed as a new child "
                        "work order under the feature, and the only thing that worker "
                        "sees. Omit it when the failure was spurious and the work is "
                        "already done")
    f.add_argument("--project")

    # improvement orders ----------------------------------------------------------------- Modelled on... (docs/superpowers/specs/2026-09-23-improvement-orders.md)
    io = sub.add_parser(
        "io",
        help="improvement orders: an observation about the fleet that an analyst "
             "investigates and reports findings on",
    ).add_subparsers(dest="io_cmd", required=True)

    i = io.add_parser("create", help="file an observation — the OS opens an analyst for "
                                     "it and the findings come back for your decision")
    i.add_argument("project")
    i.add_argument("title")
    i.add_argument("--description", "-d", default="",
                   help="what you observed. Required: the analyst sees only this text")
    i.add_argument("--ref", dest="refs", action="append", default=[], metavar="REF",
                   help="evidence, repeatable and REQUIRED: a wo-/fo-/io-/al- id, "
                        "#<issue>, a URL, or free text. Stored verbatim and resolved by "
                        "the analyst — a reference that does not resolve is a finding "
                        "about the OS's records, not an error here")
    i.add_argument("--origin", default="jarvis", choices=["jarvis", "ui", "manual"])
    i.add_argument("--budget", metavar="USD",
                   help="cap the whole order — it and its analyst — at N dollars. Omit "
                        "for no ceiling")

    i = io.add_parser("list", help="improvement orders and where each one stands")
    i.add_argument("project", nargs="?")
    i.add_argument("--all", action="store_true", help="include settled ones")

    i = io.add_parser("show", help="one improvement order: the counts, the observation "
                                   "and the evidence it was filed with")
    i.add_argument("io_id")
    i.add_argument("--project")
    i.add_argument("--full", action="store_true",
                   help="every finding in full, and the whole brief (default: one line "
                        "per finding)")
    i.add_argument("--finding", metavar="KEY",
                   help="only this finding, in full")

    i = io.add_parser("report", help="(analysts) submit the findings report — the "
                                     "analyst's terminal action, and what settles its "
                                     "work order")
    i.add_argument("io_id")
    i.add_argument("--from-file", required=True, dest="from_file", metavar="PATH",
                   help="the report, as JSON. A file rather than an argument on purpose: "
                        "a report is a long string full of quoted output, which is "
                        "exactly what trips the privileged-action classifier")
    i.add_argument("--project")

    i = io.add_parser("review", help="decide the findings, one by one — accepting one "
                                     "writes its root cause to the knowledge base and "
                                     "files the orders it proposed")
    i.add_argument("io_id")
    i.add_argument("--accept", dest="accept", action="append", default=[],
                   metavar="KEY",
                   help="a finding key to accept, repeatable: its root cause becomes a "
                        "knowledge entry and each order it proposed is filed")
    i.add_argument("--reject", dest="reject", action="append", default=[],
                   metavar="KEY",
                   help="a finding key to reject, repeatable. Needs --feedback: the "
                        "reason is what teaches Neo, and it is the whole signal")
    i.add_argument("--feedback", default="",
                   help="your reasoning. On --reject it becomes a Neo learning scoped "
                        "to the project")
    i.add_argument("--accept-all", dest="accept_all", action="store_true",
                   help="accept every finding still awaiting you. Cannot be combined "
                        "with --reject")
    i.add_argument("--project")

    i = io.add_parser("ask-neo", help="put the findings awaiting you to Neo: never asked, "
                                      "escalated, or Neo was unreachable. Skips any Neo "
                                      "accepted, which wait only on your yes to the spend")
    i.add_argument("io_id")
    i.add_argument("--project")

    i = io.add_parser("cancel", help="stop an improvement order and its analyst")
    i.add_argument("io_id")
    i.add_argument("--project")

    i = io.add_parser("budget", help="show, set, raise or clear an improvement order's "
                                     "budget")
    i.add_argument("io_id")
    i.add_argument("amount", nargs="?", metavar="USD",
                   help="the new ceiling, in dollars. Omit to just show it")
    i.add_argument("--clear", action="store_true", help="remove the ceiling")
    i.add_argument("--project")

    for i in io.choices.values():
        i.add_argument("--json", action="store_true", help="machine-readable output")

    # investigation orders --------------------------------------------------------------- Beside the... (docs/superpowers/specs/2026-09-27-investigation-orders.md)
    inv = sub.add_parser(
        "investigate",
        help="investigation orders: diagnose one stuck order read-only and classify it",
    ).add_subparsers(dest="inv_cmd", required=True)

    v = inv.add_parser("create", help="open an investigation into one stuck order (the "
                                      "default: `jarvis investigate <subject>`)")
    v.add_argument("subject", help="the wo-/fo-/io- id that is not progressing")
    v.add_argument("--why", required=True,
                   help="what you saw. Required: the investigator's first reader is a "
                        "fresh session with no memory of the conversation that produced "
                        "it")
    v.add_argument("--project", help="whose project the subject is in (default: resolved "
                                     "from the subject id)")
    v.add_argument("--origin", default="jarvis", choices=["jarvis", "ui", "manual"])
    v.add_argument("--budget", metavar="USD",
                   help="cap the whole order — it and its investigator — at N dollars. "
                        "Omit for the catalog's `investigation_budget_usd`, which is NOT "
                        "no ceiling: the caller here is usually a daemon loop")

    v = inv.add_parser("list", help="investigations and where each one stands")
    v.add_argument("project", nargs="?")
    v.add_argument("--all", action="store_true", help="include settled ones")

    v = inv.add_parser("show", help="one investigation: the classification and the "
                                    "subject, then the root cause and the evidence")
    v.add_argument("inv_id")
    v.add_argument("--project")

    v = inv.add_parser("cancel", help="stop an investigation and its investigator")
    v.add_argument("inv_id")
    v.add_argument("--project")

    v = inv.add_parser("budget", help="show, set, raise or clear an investigation's "
                                      "family budget")
    v.add_argument("inv_id")
    v.add_argument("amount", nargs="?", metavar="USD",
                   help="the new ceiling, in dollars. Omit to just show it")
    v.add_argument("--clear", action="store_true", help="remove the ceiling")
    v.add_argument("--project")

    v = inv.add_parser("verdict", help="(investigators) submit the verdict — the "
                                       "investigator's terminal action, and what settles "
                                       "the order")
    v.add_argument("inv_id")
    v.add_argument("--from-file", required=True, dest="from_file", metavar="PATH",
                   help="the verdict, as JSON. A file rather than an argument on purpose: "
                        "a verdict is full of repo paths and quoted log lines, which is "
                        "exactly what trips the privileged-action classifier")
    v.add_argument("--project")

    for v in inv.choices.values():
        v.add_argument("--json", action="store_true", help="machine-readable output")

    # gates (privileged-action approvals)
    ga = sub.add_parser(
        "gate",
        help="privileged-action approvals: shipping actions a worker may attempt only "
             "with an independent review",
    ).add_subparsers(dest="ga_cmd", required=True)
    g = ga.add_parser("request",
                      help="(workers) ask permission to run a privileged command")
    # Two spellings; the SHORT one is the one a blocked worker can actually type — spec 2026-09-12 §8
    g.add_argument("wo_id", metavar="request-number | wo-id",
                   help="the request number the block printed — or a work order id, "
                        "followed by the command")
    g.add_argument("command", nargs="?",
                   help="the EXACT command you will run if approved; omit it when you "
                        "gave a request number")
    # Kind-neutral: the parser is built before any command is parsed — spec §6
    g.add_argument("--why", default="",
                   help="why this action should proceed — each gate kind asks a "
                        "different question; the block message states yours")
    g.add_argument("--evidence", default="",
                   help="what a reviewer can check — the reviewer sees only this")
    g.add_argument("--why-file", dest="why_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
    g.add_argument("--evidence-file", dest="evidence_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
    g.add_argument("--project")
    g = ga.add_parser(
        "contest",
        help="(workers ONLY — needs JARVIS_WO_ID) dispute the MATCH: this command "
             "performs no privileged action. Reaches the reviewer as a candidate "
             "dismissal, and authorises nothing. To rule on one yourself, use "
             "`jarvis gate dismiss`",
    )
    g.add_argument("wo_id", metavar="request-number | wo-id",
                   help="the request number the block printed — or a work order id, "
                        "followed by the command")
    g.add_argument("command", nargs="?",
                   help="the EXACT command that was blocked; omit it when you gave a "
                        "request number")
    g.add_argument("--why", required=True,
                   help="why this performs no privileged action — where the matched "
                        "text sits, and what the command really does")
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
        help="why a command would or would not trip a gate — give the request number "
             "the block printed, or paste the exact string, to diagnose a false positive",
    )
    g.add_argument("command", metavar="request-number | command")
    g.add_argument("--project")

    # rules (the self-healing detector/remedy registry) ----------------------------------... (docs/superpowers/specs/2026-09-27-self-evolution.md §3.4.)
    ru = sub.add_parser(
        "rules",
        help="the self-healing registry: the gaps the OS recognises in itself, and what "
             "it proposes doing about each one",
        description="the self-healing registry — the gaps the OS has learned to "
                    "recognise in itself (a DETECTOR) and what it proposes doing about "
                    "each one (a REMEDY RULE). This is NOT `jarvis gate rules`, which "
                    "is the other registry: that one answers what counts as a "
                    "privileged action, this one answers what counts as a gap.",
    ).add_subparsers(dest="rules_cmd", required=True)

    r = ru.add_parser("list", help="the registry, counts first")
    r.add_argument("--project", default="",
                   help="this project's rules AND the fleet-wide ones")
    r.add_argument("--status", choices=("dry_run", "armed", "retracted"))
    r.add_argument("--gap-class", dest="gap_class", default="")
    r.add_argument("--days", type=int, default=30,
                   help="window of each rule's cost line (default 30; 0 = all time)")

    r = ru.add_parser("show", help="one detector: its condition in prose, its remedy "
                                   "rules, its provenance, what it cost and its newest "
                                   "fires")
    r.add_argument("detector_id", metavar="dt-id")
    r.add_argument("--days", type=int, default=30,
                   help="window of the cost line (default 30; 0 = all time)")

    r = ru.add_parser("retract", help="retire a detector or one remedy rule: it stops "
                                      "applying, the record keeps that it once did")
    r.add_argument("rule_id", metavar="dt-id | rm-id")
    r.add_argument("--reason", required=True,
                   help="why — the only record of what changed the OS's mind")

    # §6 of the same spec
    r = ru.add_parser("arm", help="let a detector's fires raise an alarm and take the "
                                  "existing self-heal gate. A PERSON arms a rule; the "
                                  "gate still decides whether anything runs")
    r.add_argument("detector_id", metavar="dt-id")
    r.add_argument("--reason", required=True,
                   help="why this rule has earned it — recorded on the row")

    r = ru.add_parser("false-positive", help="say an armed fire was wrong. Enough of "
                                             "them return the detector to dry run")
    r.add_argument("fire_id", metavar="fire-id", type=int)
    r.add_argument("--reason", required=True,
                   help="what was wrong about it — recorded on the fire")

    r = ru.add_parser("dry-run", help="what this detector reads, and what it would "
                                      "decide about one order. WRITES NOTHING")
    r.add_argument("detector_id", metavar="dt-id")
    r.add_argument("order_id", nargs="?", default="",
                   help="an order to evaluate against. Omit to just see what the "
                        "condition reads")

    # docs/superpowers/specs/2026-09-27-self-evolution.md §9
    r = ru.add_parser("summary", help="what the project learned, and whether it is "
                                      "compounding")
    r.add_argument("project", nargs="?", default="",
                   help="one project's history. Omit for the whole fleet")
    r.add_argument("--days", type=int, default=90,
                   help="how far back the window reaches (default 90)")

    for r in ru.choices.values():
        r.add_argument("--json", action="store_true", help="machine-readable output")

    # config (the versioned configuration console) ---------------------------------------... (docs/superpowers/specs/2026-08-27-the-config-console.md §8.)
    cf = sub.add_parser(
        "config",
        help="the fleet's configuration: read it, change it, and see who changed it "
             "when and why. Every write records a version and rewrites the catalog",
    ).add_subparsers(dest="cfg_cmd", required=True)

    c = cf.add_parser("show", help="the effective configuration, and which version it is")
    c.add_argument("project", nargs="?", help="only this project's settings, plus the "
                                              "fleet settings it runs under")
    c.add_argument("--version", help="a stored version instead of the live file")
    c.add_argument("--catalog", help="a catalog file other than the registered one")

    c = cf.add_parser("get", help="one setting, as the fleet reads it")
    c.add_argument("project", nargs="?")
    c.add_argument("path", help="e.g. os.validation.enabled, or validation.enabled "
                                "when a project is named")
    c.add_argument("--catalog")

    c = cf.add_parser("set", help="change one setting")
    c.add_argument("project", nargs="?")
    c.add_argument("path")
    c.add_argument("value", help="JSON if it parses as JSON (true, 3, null, [\"a\"]), "
                                 "otherwise the literal string")
    c.add_argument("--reason", default="",
                   help="REQUIRED for a safety setting (permission_mode, gates, "
                        "validation, neo.enabled) — it goes on the version row")
    c.add_argument("--catalog")

    c = cf.add_parser("unset", help="drop one setting, so it falls back to its default")
    c.add_argument("project", nargs="?")
    c.add_argument("path")
    c.add_argument("--reason", default="")
    c.add_argument("--catalog")

    c = cf.add_parser(
        "wiring",
        help="which of YOUR MCP servers, skills and plugins reach this project's "
             "workers. Reads your Claude configuration to list them and never writes "
             "to it; deselect on the dashboard's /config page")
    c.add_argument("project", nargs="?", help="a project, or the fleet base if omitted")
    c.add_argument("--refresh", action="store_true",
                   help="re-read your Claude configuration instead of the cached answer")
    c.add_argument("--catalog")

    c = cf.add_parser("history", help="who changed what, when and why")
    c.add_argument("project", nargs="?")
    c.add_argument("--limit", type=int, default=20)

    c = cf.add_parser("diff", help="every setting two versions disagree on")
    c.add_argument("a", help="a version id, or any unambiguous prefix of one")
    c.add_argument("b")

    c = cf.add_parser("restore", help="put an old version back — writes forward")
    c.add_argument("version_id")
    c.add_argument("--reason", default="")
    c.add_argument("--catalog")

    c = cf.add_parser(
        "adopt",
        help="record a hand-edited catalog as a version, so the record catches up "
             "with the file")
    c.add_argument("--reason", default="")
    c.add_argument("--catalog")

    # onboard: `questions` and `explain` are added to THIS group (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §0.2)
    ob = sub.add_parser(
        "onboard",
        help="onboard a project: decide what the OS does for it automatically, then "
             "apply that as one configuration version plus a bootstrap",
    ).add_subparsers(dest="ob_cmd", required=True)

    c = ob.add_parser("start", help="open the interview: an interactive greeting_agent "
                                    "session in this terminal")
    c.add_argument("path", help="the project's git repository")
    c.add_argument("--name", help="project name (default: directory name)")
    c.add_argument("--print-prompt", dest="print_prompt", action="store_true",
                   help="print the persona root and the opening prompt instead of "
                        "launching claude")

    c = ob.add_parser(
        "apply",
        help="apply an interview's draft: ONE configuration version, then the bootstrap")
    c.add_argument("--from", dest="draft", required=True, metavar="PATH",
                   help="the draft file the interview wrote")
    c.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="what it WOULD change, writing nothing — the confirmation diff")
    c.add_argument("--catalog")
    c.add_argument("--json", action="store_true", help="machine-readable output")

    c = ob.add_parser("questions", help="the whole interview as data — every area, "
                                        "its postures and its knobs; no model needed")
    c.add_argument("--area", help="one area only")
    c.add_argument("--json", action="store_true")

    c = ob.add_parser("explain", help="what an area or one setting does and costs, "
                                      "its current value, and how to change it later")
    c.add_argument("target", help="an area (`validation`) or a setting "
                                  "(`os.neo.enabled`, `validation.auto_merge`)")
    c.add_argument("--project", help="read current values from this project "
                                     "(default: the shipped defaults)")
    c.add_argument("--catalog", help="a catalog file other than the registered one")
    c.add_argument("--json", action="store_true")

    # backlog
    bl = sub.add_parser("backlog", help="unified deferred-work backlog").add_subparsers(
        dest="bl_cmd", required=True)
    b = bl.add_parser("add")
    b.add_argument("project")
    b.add_argument("title")
    b.add_argument("--description", "-d", default="")
    b.add_argument("--depends-on", default="", help="comma-separated backlog ids")
    # Where the item came from, when something deferred it rather than somebody typing it
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

    # knowledge
    kn = sub.add_parser("learn", help="central knowledge base").add_subparsers(
        dest="kn_cmd", required=True)
    k = kn.add_parser("add")
    k.add_argument("content", nargs="?")
    k.add_argument("--content-file", dest="content_file", metavar="PATH",
                   help=_PROSE_FILE_HELP)
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
    k = kn.add_parser("search", help="which entries match a term: headline, id, one "
                                     "matching line (`learn show` for a body)")
    k.add_argument("term")
    k.add_argument("--project", help="this project + global entries")
    k.add_argument("--topic")
    k.add_argument("--limit", type=int, default=20)
    k = kn.add_parser("graph", help="(prototype) export the knowledge graph as GEXF for "
                                    "Gephi Lite: entries, the orders that wrote and "
                                    "retired them, learned gate rules, Neo learnings")
    k.add_argument("-o", "--output", default="knowledge.gexf")
    k = kn.add_parser("review", help="review the base by hand: domains, assignments and "
                                     "links, drawn as a graph in the browser")
    k.add_argument("--port", type=int, help="default: a free port")
    k.add_argument("--host", default="127.0.0.1")
    k.add_argument("--no-browser", dest="no_browser", action="store_true",
                   help="print the address instead of opening it")
    k = kn.add_parser("show", help="full text of specific entries, by id")
    k.add_argument("ids", nargs="+")
    k = kn.add_parser("topics", help="topics and how many entries each holds")
    k.add_argument("--project")
    k = kn.add_parser("stats", help="what memory costs and whether anyone reads it")
    k.add_argument("--project")
    k.add_argument("--days", type=int, help="only reads and orders this recent")
    k.add_argument("--limit", type=int, default=10, help="rows per list")
    k = kn.add_parser("pin", help="always inject this entry in full")
    k.add_argument("kn_id")
    k = kn.add_parser("unpin", help="demote to an index headline")
    k.add_argument("kn_id")
    k = kn.add_parser("retract", help="retire a superseded entry (kept for the record, "
                                      "dropped from worker prompts and the index)")
    k.add_argument("knowledge_id")
    k.add_argument("--reason", required=True,
                   help="what supersedes it — this is kept beside the entry for ever")

    # brief
    bf = sub.add_parser(
        "brief",
        help="(workers) print one full briefing section on demand — the worker "
             "prompt carries a compressed core plus an index of these")
    bf.add_argument("section", nargs="?", default="",
                    help="one of: contract, gates, record, navigation, knowledge; "
                         "omit to list them")
    bf.add_argument("--wo", default="", metavar="WO_ID",
                    help="render with this work order's real ids")

    # neo
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
                   help="also show learnings scoped to this seat — a panel seat, or "
                        "`supervisor` for what your alarm reviews have taught the "
                        "supervisor (global rows alone are shown without it)")
    n.add_argument("--json", action="store_true", help="machine-readable output")
    n = ne.add_parser("stats", help="Neo's volume, outcomes, escalation causes, spend "
                                    "and latency")
    n.add_argument("--project", default="")
    n.add_argument("--days", type=int, default=None,
                   help="window in days; omit for all time")
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

    # validation ---------------------------------------------------------------------- The deliberation
    va = sub.add_parser(
        "validation", help="how the validation panel judged a work or feature order"
    ).add_subparsers(dest="validation_cmd", required=True)
    v = va.add_parser("show", help="every round in full: each seat's verdict, model, "
                                   "latency and raw reply")
    # One command for both units: a round is the same fact either way
    v.add_argument("unit_id", metavar="WO_ID|FO_ID")
    v.add_argument("--project")
    # ...and the one write verb on the same noun
    f = va.add_parser(
        "force", help="open a fresh validation round on a work order by hand, with no "
                      "worker and no `finished` event. ONLY on an order that has "
                      "delivered and whose worker is not typing — `waiting_pr_merge` "
                      "(the case it exists for) or `needs_review`; a live or settled "
                      "one is refused. It goes to `validating`, and back to "
                      "`waiting_pr_merge` if the round passes. It spends a round number "
                      "like any other, so a rejection at or past `max_rounds` is "
                      "escalated to you rather than sent to a worker")
    f.add_argument("wo_id", metavar="WO_ID")
    f.add_argument("--reason", required=True,
                   help="why you are forcing it — recorded on the round, so the "
                        "re-judgement never reads afterwards like a worker's own")
    f.add_argument("--project")

    # notifications
    n = sub.add_parser("notify", help="emit a notification into the OS pipeline")
    n.add_argument("title")
    n.add_argument("body", nargs="?", default="")
    n.add_argument("--project", help="project name (auto-detected from cwd when omitted)")
    n.add_argument("--level", default="info", choices=["info", "warning", "critical"])
    n.add_argument("--wo-id")

    ibp = sub.add_parser("inbox", help="central notification inbox")
    ib = ibp.add_subparsers(dest="ib_cmd", required=False)
    ibl = ib.add_parser("list")
    for ibx, sub_level in ((ibp, False), (ibl, True)):
        # SUPPRESS on `list` so its defaults never overwrite flags given before it
        d = (lambda v: argparse.SUPPRESS) if sub_level else (lambda v: v)
        ibx.add_argument("--level", choices=["critical", "warning", "info"], default=d(""),
                         help="only this criticality")
        ibx.add_argument("--project", default=d(""), help="only this project")
        ibx.add_argument("--search", "-s", default=d(""),
                         help="words that must all appear in title, body or wo id")
        ibx.add_argument("--sort", choices=["priority", "newest", "oldest"],
                         default=d("priority"),
                         help="priority (default) = critical first, newest first within a level")
    i = ib.add_parser("ack")
    i.add_argument("inbox_id", nargs="?", type=int, help="omit to ack everything")

    # bug reports
    bug = sub.add_parser(
        "bug", help="report a bug in Jarvis OS itself").add_subparsers(
        dest="bug_cmd", required=True)
    br = bug.add_parser("report", help="file a Jarvis OS bug as a GitHub issue")
    br.add_argument("title")
    br.add_argument("--description", "-d", required=True,
                    help="what happened, in Jarvis OS terms")
    br.add_argument("--expected", "-e", required=True, help="what you expected")
    br.add_argument("--actual", "-a", required=True, help="what you got instead")
    # REQUIRED, with no default and no inference: it is the only thing that decides what happens to the...
    br.add_argument("--priority", "-p", required=True, choices=list(_PRIORITIES),
                    help="how bad this is for the FLEET. " + _PRIORITY_RUBRIC)
    # A SCHEDULING DECISION, kept out of --priority on purpose: the rating stays an honest description of...
    br.add_argument("--expedite", action="store_true",
                    help="(the USER only — ignored inside a work-order session) "
                         "work on it NOW, whatever the priority: the issue is filed as "
                         "usual and a work order is dispatched on it immediately, "
                         "instead of waiting on the tracker (or, for critical/blocker, "
                         "on Neo's re-assessment — which still runs and still settles "
                         "the rating, so an unconfirmed claim ships no release)")
    br.add_argument("--steps", default="", help="optional steps to reproduce")
    br.add_argument("--project", default="", help="reporting project (default: $JARVIS_PROJECT)")
    br.add_argument("--wo-id", default="", help="reporting work order (default: $JARVIS_WO_ID)")

    # ui / daemon / hook
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


# -- command implementations


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


def cmd_pause(args: argparse.Namespace) -> int:
    from . import ops
    allow = [i.strip() for i in args.allow.split(",") if i.strip()]
    _print(ops.pause_fleet(reason=args.reason, allow=allow), args.json)
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from . import ops
    _print(ops.resume_fleet(args.order_ids, everything=args.everything), args.json)
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
    fl = st.get("fleet") or {}
    if fl.get("paused"):
        allowed = ", ".join(fl["allow"]) or "nothing"
        print(f"⏸ FLEET PAUSED{' — ' + fl['reason'] if fl.get('reason') else ''} · "
              f"allowed through: {allowed} · `jarvis resume <wo-id>` / `--all`")
    elif fl.get("ramp"):
        r = fl["ramp"]
        print(f"↗ ramping up after the usage window reopened: at most {r['cap']} "
              f"worker turn(s) in flight"
              + (" (breaker tripped: the last window was spent again soon after "
                 "reopening)" if r.get("tripped") else ""))
    if st["attention"]:
        print(f"\n⚠ NEEDS YOUR ATTENTION ({len(st['attention'])}):")
        print(f"   {ATTENTION_LIST_NOTE}")
        for a in st["attention"]:
            ident = a["wo_id"] or a.get("fo_id")
            print(f"  • [{a['project']}]{' ' + ident if ident else ''} {a['title']} "
                  f"— {a['reason']}")
            if a.get("parked"):
                # The flag carries one sentence and this order needed two: what the user owes it
                print(f"      also: {a['parked']}")
            if a.get("attach"):
                # `resume_auto` is present only where a permission prompt is possible at all
                alt = (f"  ·  or `{a['resume_auto']}`" if a.get("resume_auto") else "")
                print(f"      open it: {a['attach']}{alt}")
            if a.get("decide"):
                # A gate item carries the command to run AND the request to read
                see = (f"  ·  see it: `jarvis gate show {a['approval_id']}`"
                       if a.get("approval_id") else "")
                print(f"      {a['decide']}{see}")
    if st["inbox"]["unacked"]:
        print(f"\n📥 inbox: {st['inbox']['unacked']} unacked"
              f" ({st['inbox']['critical']} critical) — `jarvis inbox list`")
        print(f"   {INBOX_LIST_NOTE}")
    neo = st.get("neo", {})
    if (neo.get("queued") or neo.get("unreviewed") or neo.get("escalated")
            or neo.get("failed")):
        # `unreachable` is its own clause and never folded into `escalated`
        unreachable = (f"{neo['failed']} unreachable (NOT judged), "
                       if neo.get("failed") else "")
        print(f"\n🕶 neo: {neo.get('queued', 0)} queued, "
              f"{neo.get('escalated', 0)} escalated to you, {unreachable}"
              f"{neo.get('unreviewed', 0)} answers awaiting your review — `jarvis neo list`")
    if args.attention:
        return 0
    print()
    for p in st["projects"]:
        counts = ", ".join(f"{k}:{v}" for k, v in p.get("summary", {}).get("by_status", {}).items())
        drift = " ⚠ settings drift" if p.get("settings_drift") else ""
        print(f"• {p['name']} — {counts or 'no work orders'}{drift}")
        # A scheduled job that has wanted to fire and could not
        for held in p.get("schedule_held", []):
            print(f"    ⏰ {held['job_id']} held: {held['reason']}")
        for wo in p.get("open_work_orders", []):
            icon = STATUS_ICON.get(wo["status"], "•")
            badge = ORIGIN_BADGE.get(wo["origin"], wo["origin"])
            att = f"  ⚠ {wo['attention_reason']}" if wo["needs_attention"] else ""
            # `status_label`, not `status`: for a `pending` order it also says WHY it is not starting
            print(f"    {icon} {wo['id']} [{badge}] {wo['title']} "
                  f"({wo.get('status_label') or wo['status']}){att}")
    if st["backlog"]["open"]:
        print(f"\n🗂 backlog: {st['backlog']['open']} open items — `jarvis backlog list`")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import ops
    res = ops.run_doctor(project=args.project, repair=args.repair,
                         catalog_path=args.catalog, include_os=not args.skip_os)
    if args.json:
        _print(res, True)
        return 1 if res["violations"] else 0
    orphans = res.get("orphaned_sessions") or []
    if not res["violations"]:
        print("✓ all OS invariants hold")
        _print_orphans(orphans)
        return 0
    # Only offer --repair when something here is actually repairable: OS-level violations never are
    fixable = any(v["repaired"] for p in res["projects"] for v in p["violations"])
    verb = ("repaired" if res["repair"]
            else "found (run with --repair to fix)" if fixable else "found")
    print(f"⚠ {res['violations']} invariant violation(s) {verb}:\n")
    for v in res.get("os", []):
        # OS-level: not about any one project (the dashboard being down, say), and never auto-repairable
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
    """Token counts, at the magnitude a reader can hold in their head"""
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
    """The per-turn breakdown of one work order, from the recorded result JSON"""
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


def _rewrite_cause(share: float | None, ttl_boundaries: int, boundaries: int) -> str:
    """The half-sentence saying WHICH of the two cold-boundary causes this tax was (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""
    if share is None:
        return ""
    prefix = boundaries - ttl_boundaries
    return (f" {share:.0%} of it was the cache entry EXPIRING ({ttl_boundaries} "
            f"boundar{'ies' if ttl_boundaries != 1 else 'y'}, the part a longer TTL "
            f"would buy back); the other {1 - share:.0%} was the prompt PREFIX moving "
            f"({prefix} boundar{'ies' if prefix != 1 else 'y'}), which no TTL can help.")


def _print_write_ttl(totals: dict) -> None:
    """What the fleet paid to WRITE to the prompt cache, and at which of the two rates"""
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
    """What Jarvis itself spent on this work order, call by call"""
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
        # The largest input only where one was measured (spec §3 (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        biggest = kind.get("max_input_chars") or 0
        size = f", largest input {biggest:,} characters" if biggest else ""
        print(f"  {kind['label']}: {kind['calls']} call"
              f"{'s' if kind['calls'] != 1 else ''}, ~${kind['cost_usd']:.2f}{size}")


def _print_subprocess_calls(res: dict) -> None:
    """What the worker spent in `claude` processes it spawned itself, by what ran them"""
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
    """One line of the bill tree, and everything under it"""
    pad = "  " * depth
    unit = line.get("unit")
    count = ""
    # "turn 4 (1 turn)" says nothing twice
    if line["calls"] and unit != "" and not (line["calls"] == 1 and unit == "turn"):
        count = f" ({line['calls']} {unit or 'charge'}"
        count += "s)" if line["calls"] != 1 else ")"
    print(f"{line['cost']['list_usd']:>9.3f} {_tok(line['tokens']['total']):>8}  "
          f"{pad}{line['label']}{count}")
    for child in line.get("children") or []:
        _print_bill_line(child, depth + 1)


def _print_provenance(acc: dict) -> None:
    """When this bill was worked out, and what it could not see"""
    import time as _time

    if acc.get("sealed_at"):
        when = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(acc["sealed_at"]))
        print(f"  sealed when this order settled, on {when} — the records behind it "
              f"expire, this figure does not")
        if acc.get("resealed_at"):
            again = _time.strftime("%Y-%m-%d %H:%M",
                                   _time.localtime(acc["resealed_at"]))
            was = (acc.get("corrected_from") or {}).get("total")
            if was:
                print(f"  re-derived on {again}: the seal counted {was:,} tokens, "
                      f"each turn carrying the whole session's running total")
            else:
                print(f"  re-derived on {again} to add detail the original seal "
                      f"predates; adopted only because it still saw every token the "
                      f"seal held")
    elif acc.get("live"):
        print("  worked out just now, from records that are still live; sealed "
              "automatically once the order settles")
    for gap in acc.get("gaps") or []:
        print(f"  ⚠ {gap}")


def _print_turn_calls(rows: list[dict]) -> None:
    """Every API call of every turn — the grain under the turn"""
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
            # Never a silent cap: a list that just stops reads as a complete one
            print(f"{r['seq']:>4} {'…':>5} {'—':>8} {_tok(folded['cache_read']):>8} "
                  f"{_tok(folded['cache_write']):>8} {_tok(folded['output']):>7}  "
                  f"{folded['count']} further calls, folded into this line")
        cover = r.get("calls_cover") or {}
        if cover and cover.get("total", 0) < (r.get("input", 0) + r.get("cache_write", 0)
                                              + r.get("cache_read", 0)
                                              + r.get("output", 0)):
            print(f"{'':>4} {'':>5}  these are the lead agent's calls; the rest of "
                  f"turn {r['seq']} is the subagents it spawned and what no transcript "
                  f"accounts for, both itemised above")


def _print_bill(bill: dict) -> None:
    """One order's itemised bill — the same payload the dashboard renders"""
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
                          ("agents", "agent by agent — the lead, then what it spawned, "
                                     "then what no transcript accounts for"),
                          ("orders", "the orders under it")):
        lines = bill.get(view) or []
        own = bill.get("own") if view == "orders" else None
        if not lines and not own:
            continue
        print(f"\n{caption}:")
        print(f"{'$':>9} {'tokens':>8}")
        if view == "orders":
            for order in lines:
                _print_bill_line(order["total"])
            if own:
                # Relabelled: its label is the parent's id
                _print_bill_line({**own, "label": bill_mod.OWN_LABEL})
            continue
        for line in lines:
            _print_bill_line(line)
    rows = bill.get("turn_rows") or []
    if rows:
        # Not a charge and deliberately outside the bill: the same context is re-read on every API call of a...
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
    # The cache-write rate is the one this order PAID
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
    # What is NOT here, named — the sentences come from `bill.ABSENT_NOTES`
    missing = bill_mod.absent_notes(bill)
    if missing and bill["kind"] == "work_order":
        print("\nnot on this bill:")
        for _key, text in missing:
            print(f"  · {text}")
    rewrite = bill.get("rewrite") or {}
    if rewrite.get("tokens"):
        print(f"\nre-write tax ~${rewrite['list_usd']:.2f} — "
              f"{_tok(rewrite['tokens'])} tokens of context re-sent to the cache across "
              f"{rewrite['boundaries']} turn "
              f"boundar{'ies' if rewrite['boundaries'] != 1 else 'y'}. "
              f"Not an extra charge: it is the "
              f"part of the cache-write line above that paid to send context a warm "
              f"cache would have served at a tenth of the price."
              + _rewrite_cause(rewrite.get("ttl_share"),
                               rewrite.get("ttl_boundaries") or 0,
                               rewrite["boundaries"]))
        # WHICH SIDE PAID IT (spec 2026-10-02 §1.5)
        sides = rewrite.get("by_side") or {}
        sub_side = sides.get("subagent") or {}
        if sub_side.get("structural_zero"):
            print(bill_mod.SUBAGENT_REWRITE_ZERO.format(
                count=sub_side.get("count", 0),
                written=f"{sub_side.get('cache_write', 0):,}"))
        elif sub_side:
            main_side = sides.get("main") or {}
            print(f"by side — main {main_side.get('tokens', 0):,} tokens across "
                  f"{main_side.get('boundaries', 0)}, "
                  f"subagent {sub_side.get('tokens', 0):,} across "
                  f"{sub_side.get('boundaries', 0)}")
    subagents = bill.get("subagents") or {}
    if subagents.get("count"):
        print(f"{subagents['count']} subagent(s), ~${subagents['list_usd']:.2f}, "
              f"itemised above under the turn each ran in — drawn out of that turn's "
              f"total, never added to it")
    # PRESENCE, never truthiness: absent means this seal predates the measurement
    placeholder = bill.get("placeholder")
    if placeholder is not None and placeholder.get("calls"):
        print(f"{placeholder['calls']:,} of those API calls report placeholder output "
              f"totalling {placeholder['output']:,} tokens "
              f"({placeholder['subagent_calls']:,} in a subagent's transcript, "
              f"{placeholder['main_calls']:,} in the lead's) — "
              + bill_mod.PLACEHOLDER_NOTE)
    elif placeholder is not None:
        print(bill_mod.PLACEHOLDER_ZERO)
    for note in bill.get("notes") or []:
        print(f"\n⚠ {note}")
    print(f"\nEvery figure above is {bill['floor_reason']}.")
    print("List prices, as a common unit for comparing token kinds — not a bill.")


#: Above this many seconds a duration reads better in minutes
MINUTES_OVER_SECONDS = 90

#: Width of the rule under a unit's heading
RULE_WIDTH = 78


def _mins(seconds: float) -> str:
    """Durations at the magnitude a reader can hold in their head, like `_tok`"""
    if seconds >= MINUTES_OVER_SECONDS:
        return f"{seconds / 60:.1f}m"
    return f"{seconds:.1f}s"


#: What each bucket of the partition is called on the page
PART_LABELS = {"generating": "generating", "blocked": "blocked on a subagent",
               "tools": "running tools", "idle": "between turns, nothing running",
               "unaccounted": "unaccounted — no API response has completed"}

#: The same buckets on the per-TURN line, where a full label does not fit
PART_SHORT = {"generating": "gen", "blocked": "blocked", "tools": "tools",
              "idle": "idle", "unaccounted": "unacc"}

#: The glyph each bucket occupies in the per-turn bar (spec §4c)
BAR_GLYPHS = {"generating": "█", "blocked": "▓", "tools": "▒", "idle": "░",
              "unaccounted": "·"}

#: Fixed, so every turn's bar is comparable at a glance and the columns after it line up
BAR_WIDTH = 20

#: A SUBAGENT'S FOUR CACHE STATES (spec 2026-10-02 §1.4)
SUB_UNDER_FLOOR = ("wrote {total} in {calls} calls — no single write reached the "
                   "{floor:,} floor (largest {largest})")
SUB_NO_WRITES = "wrote nothing to the cache"
#: THE FOURTH STATE, ahead of the other three: the figures are ABSENT, not zero
SUB_NOT_SEALED = "cache anatomy not in this seal — sealed before it was recorded"
#: The ZERO as the FINDING rather than as silence: a subagent's written volume never exceeds its own...
SUB_NO_BOUNDARY = ("no boundary — one continuous conversation, so no re-write tax. "
                   "STRUCTURAL, not small: {written} was still written")
#: NEVER "0 prefix misses", which would read as a finding: with no floor the TTL-vs-prefix split is...
SUB_UNCLASSIFIED = "unclassified (no os.cold_prefix_floor)"

#: What a turn with no API call says, ahead of any duration breakdown
NO_CALL_FLAG = "NO API CALL"

#: The same column when a request is IN FLIGHT — spec of 2026-09-27 §1
AWAITING_FLAG = "AWAITING MODEL"

#: One width for both, so the split after the flag stays a column either way
FLAG_WIDTH = max(len(NO_CALL_FLAG), len(AWAITING_FLAG))


def _print_partition(unit: dict[str, Any]) -> None:
    """The headline: both clocks, then how the wall one divides"""
    part, share = unit["partition"], unit["share"]
    held = part.get("held", 0.0)
    clocks = f"  wall clock {_mins(part['wall'])}"
    if held:
        clocks += f" · active {_mins(part.get('active', part['wall']))}"
    print(f"{clocks}, peak context {_tok(unit['context_peak'])}")
    causes = unit.get("hold_causes") or {}
    for cause, seconds in (unit.get("held_by") or {}).items():
        print(f"    {seconds / part['wall'] * 100:>4.0f}%  {_mins(seconds):>8}  "
              f"HELD by {causes.get(cause, cause)}")
    for name in PART_LABELS:
        print(f"    {share[name] * 100:>4.0f}%  {_mins(part[name]):>8}  "
              f"{PART_LABELS[name]}")
    if held:
        # THE RESIDUAL, AND IT IS THE POINT OF SPLITTING THE CLOCK
        print(f"    {'':>4}  {_mins(part.get('unexplained', 0.0)):>8}  "
              f"of that idle, nothing on record was holding it")


def _bar(share: dict[str, float]) -> str:
    """The turn's partition as one fixed-width stacked bar, in `PARTS` order"""
    keys = list(BAR_GLYPHS)
    out, done = [], 0
    for index, name in enumerate(keys, start=1):
        edge = min(BAR_WIDTH,
                   round(sum(share.get(k, 0.0) for k in keys[:index]) * BAR_WIDTH))
        out.append(BAR_GLYPHS[name] * max(0, edge - done))
        done = max(done, edge)
    return "".join(out).ljust(BAR_WIDTH, BAR_GLYPHS[keys[-1]])


def _print_subagent(sub: dict[str, Any], indent: str) -> None:
    """One subagent under the turn that named it"""
    writes = ", ".join(f"{cause} {_tok(written)}"
                       for cause, written in (sub["writes_by_cause"] or {}).items())
    total = sub.get("total_written")
    if total is None:
        # FIRST, ahead of the writes list: an old seal carries neither
        cache = SUB_NOT_SEALED
    elif writes:
        cache = "writes " + writes
    elif total:
        # THE FLOOR, NAMED
        cache = SUB_UNDER_FLOOR.format(total=f"{total:,}",
                                       calls=sub.get("api_call_count", 0),
                                       floor=sub.get("write_floor", 0),
                                       largest=f"{sub.get('max_write', 0):,}")
    else:
        cache = SUB_NO_WRITES
    print(f"{indent}⤷ {sub['label'] or sub['task_id']}  {_mins(sub['wall']):>7}  "
          f"{sub['api_calls']:>3} api calls  peak {_tok(sub['context_peak']):>5}  "
          f"{cache}")
    census = sub.get("rewrite") or {}
    if census.get("boundaries"):
        parts = [f"{census['boundaries']} boundar"
                 f"{'ies' if census['boundaries'] != 1 else 'y'}",
                 f"re-write {_tok(census['tokens'])}"]
        if census.get("undecided_boundaries"):
            parts.append(SUB_UNCLASSIFIED)
        else:
            parts.append(f"ttl {_tok(census['ttl_write'])}, "
                         f"prefix-miss {_tok(census['prefix_write'])}")
        print(f"{indent}  {' · '.join(parts)}")
    elif total:
        print(f"{indent}  {SUB_NO_BOUNDARY.format(written=f'{total:,}')}")
    if sub["deeper"]:
        # Never imply completeness (spec §4b): one directory level is globbed
        noun = "subagent" if sub["deeper"] == 1 else "subagents"
        print(f"{indent}  {sub['deeper']} deeper {noun} NOT read — "
              f"depth read {sub['depth_read']}")


def _print_params(turn: dict[str, Any], caps: dict[str, int]) -> None:
    """Every span of one turn with its reported parameters, behind `--params`"""
    if not turn["spans"]:
        return
    print(f"           parameters — capped at {caps['per_value']:,} chars per value, "
          f"{caps['per_span']:,} per span, {caps['per_turn']:,} per turn")
    for span in turn["spans"]:
        print(f"           · {span['name']}  {_mins(span['seconds'])}")
        for key, value in span["params"].items():
            print(f"               {key} = {value}")
        notes = []
        if span["params_truncated"]:
            notes.append("shortened: " + ", ".join(span["params_truncated"]))
        if span["params_dropped"]:
            notes.append("not printed, the cap was reached: "
                         + ", ".join(span["params_dropped"]))
        for note in notes:
            print(f"               ({note})")


def _print_autopsy_provenance(provenance: dict[str, Any] | None) -> None:
    """Which reading answered — spec §4 of 2026-09-27-order-autopsy-durability.md"""
    if not provenance:
        return
    print(f"  reading: {provenance['note']}")
    for sentence in (provenance["level_note"], provenance["params_note"],
                     provenance["floor_note"]):
        if sentence:
            print(f"           {sentence}")


def _print_anatomy(unit: dict[str, Any], write_floor: int, *,
                   params: bool = False) -> None:
    """One session taken apart, in the order the questions get asked"""
    from . import inspection

    head = f"{unit['wo_id']} — {unit['title']}"
    print(f"{head}\n{'-' * min(len(head), RULE_WIDTH)}")
    # Before the transcript check: an OS-side input was measured when the call was made and does not... (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    biggest = unit.get("largest_os_input")
    if biggest:
        print(f"  biggest input jarvis sent — {biggest['kind']} "
              f"{biggest['label']}: {biggest['prompt_chars']:,} characters of prompt, "
              f"{biggest['system_prompt_chars']:,} of system prompt "
              f"({biggest['model'] or 'model not recorded'})")
    # ABOVE the transcript check, in `largest_os_input`'s position and for its reason: which reading...
    _print_autopsy_provenance(unit.get("provenance"))
    if not unit["found"]:
        # The same answer `jarvis cost` gives
        print("  no transcript — nothing to measure")
        return

    _print_partition(unit)
    ttl = unit["cache_ttl"]
    # §1 step 1 of the method: the re-write tax and the TTL split are read BEFORE the clock is...
    print(f"  re-written {_tok(unit['rewrite_excess'])} tokens · cache writes bought at "
          f"5m {_tok(ttl['cache_5m'])}, 1h {_tok(ttl['cache_1h'])}"
          + (f", unknown {_tok(ttl['unknown'])}" if ttl["unknown"] else ""))
    # A `--json`-only field nobody can see is half a fix (spec of 2026-09-29 §3)
    rewrite = unit.get("rewrite") or {}
    if rewrite.get("boundaries"):
        if rewrite.get("undecided_boundaries"):
            split = (f"{rewrite['compact_boundaries']} compacted, "
                     f"{rewrite['undecided_boundaries']} unclassified "
                     "(no os.cold_prefix_floor)")
        else:
            prefix = (rewrite["boundaries"] - rewrite["ttl_boundaries"]
                      - rewrite["compact_boundaries"])
            split = (f"{prefix} prefix, "
                     f"{rewrite['ttl_boundaries']} expired, "
                     f"{rewrite['compact_boundaries']} compacted")
        print(f"  boundaries {rewrite['boundaries']} — {split} · re-written "
              f"{_tok(rewrite['tokens'])} of {_tok(rewrite['cache_write'])} written")

    print()
    # The legend for the bars below, keyed off the same dict they are drawn from
    print("  " + "  ".join(f"{g} {PART_SHORT[k]}" for k, g in BAR_GLYPHS.items()))
    causes = unit.get("hold_causes") or {}
    for turn in unit["turns"]:
        reasons = ", ".join(t["kind"] for t in turn["triggers"]) or "no prompt recorded"
        s = turn["share"]
        split = "  ".join(f"{PART_SHORT[k]} {s[k] * 100:>3.0f}%" for k in PART_SHORT)
        flag = AWAITING_FLAG if turn.get("awaiting") else (
            "" if turn["observed"] else NO_CALL_FLAG)
        # The bar is drawn FROM `share` and the percentages are printed from the same dict
        label = (f"turn {turn['seq']:>2}" if turn["seq"]
                 else inspection.turn_name(turn["seq"]))
        if turn.get("part", 1) > 1:
            label += " (continued)"
        print(f"  {label}  {_mins(turn['wall']):>7}  "
              f"{flag:<{FLAG_WIDTH}}  {_bar(s)}  {split}  "
              f"{turn['api_calls']:>3} calls  peak {_tok(turn['context_peak']):>5}  "
              f"{reasons}")
        # The second clock only where the two differ, on its own line and naming the cause
        held_by = turn.get("held_by") or {}
        if held_by:
            named = ", ".join(f"{_mins(sec)} by {causes.get(c, c)}"
                              for c, sec in held_by.items())
            print(f"           · {_mins(turn['active'])} active — held {named}")
        for trigger in turn["triggers"]:
            print(f"           ↳ {trigger['quote']}")
        if turn["subagents"]:
            # A PARTITION of the turn above, drawn out of it and never added to it (spec §4b, kn-7a2180ba)
            print("           subagents — a partition of this turn, not an addition:")
            for sub in turn["subagents"]:
                _print_subagent(sub, "           ")
        if params:
            _print_params(turn, unit["param_caps"])

    for seq in unit.get("unmatched_os_turns") or []:
        # Reported, never dropped (spec 2026-09-27 §3): the OS minted that turn
        print(f"\n  turn {seq} left no prompt row in the transcript (compaction "
              f"rewrites it; its triggers are on turn {seq + 1})")

    if unit["unattached_subagents"]:
        # No timestamp fallback upstream
        print(f"\n  subagents no span named — depth read "
              f"{unit['subagent_depth_read']}:")
        for sub in unit["unattached_subagents"]:
            _print_subagent(sub, "    ")

    if unit["joins"]:
        print("\n  blocked on:")
        for span in unit["joins"]:
            print(f"    {_mins(span['seconds']):>8}  {span['detail'] or span['tool_id']}")

    print(f"\n  cache writes over {write_floor:,} tokens:")
    if not unit["writes"]:
        print("    none — nothing was re-sent at that size")
    for write in unit["writes"]:
        gap = "—" if write["cause"] == "cold-start" else _mins(write["gap"])
        print(f"    {_tok(write['written']):>7} written, {_tok(write['read']):>7} read, "
              f"{gap:>7} since the last call   {write['cause']}")
        print(f"            {write['note']}")

    if unit["tools"]:
        print("\n  tools:")
        for row in unit["tools"]:
            plural = "call " if row["calls"] == 1 else "calls"
            print(f"    {row['name']:<14}{row['calls']:>4} {plural}  "
                  f"{_mins(row['seconds']):>8} total  {_mins(row['mean']):>7} mean")
        # §3: the sentence comes from the payload (`inspection.nav_line`), which the debug page renders too
        if unit.get("nav_line"):
            print(f"    {unit['nav_line']}")

    # One reader, two surfaces (q1216): the same payload `jarvis navigation` prints
    _print_navigation(unit.get("navigation"), indent="  ")


#: The two sides, spelled for the eye
NAV_SIDE_LABELS = {"lead": "lead", "subagent": "subagent"}


def _print_navigation(payload: dict[str, Any] | None, indent: str = "") -> None:
    """How this scope navigated code, by side"""
    if not payload:
        return
    window = payload.get("window_days")
    scope = (f"{indent}navigation"
             + (f" — last {window} days" if window else ""))
    print(f"\n{scope}:")
    if not payload.get("found"):
        # Absent is never zero (issue #227): no transcript is a different answer from no navigation
        print(f"{indent}  no transcript — nothing to measure")
        return
    for side in ("lead", "subagent"):
        row = payload["sides"][side]
        share = row["code_nav_share"]
        # None and never 0.0: an unmeasured share is not a zero one
        share_text = ("not measured — no tool results"
                      if share is None else f"{share * 100:.1f}%")
        # Neo q1242: branch off the payload's flag — the per-order surface carries no call counts
        calls = (f"  {row['symbol_calls']:>4} symbol  "
                 f"{row['text_search_calls']:>4} text-search  "
                 f"{row['nav_bash_calls']:>4} bash-nav "
                 f"({row['code_nav_bash_calls']} on code)  "
                 f"{row['read_tool_calls']:>4} Read"
                 if payload.get("calls_reported", True) else "")
        # docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md §3.2: CALL counters behind the...
        doc_calls = (f"  {row['whole_file_read_calls']:>4} whole-file Read  "
                     f"{row['doc_dump_bash_calls']:>4} doc dumps"
                     if payload.get("calls_reported", True) else "")
        print(f"{indent}  {NAV_SIDE_LABELS[side]:<9}"
              f"{row['transcripts']:>4} transcripts{calls}{doc_calls}")
        print(f"{indent}           code reads via bash: {share_text} of "
              f"{_tok(row['result_bytes'])} bytes of tool results"
              + (f" · {_tok(row['unattributed_bytes'])} bytes unattributed"
                 if row["unattributed_bytes"] else ""))
        print(f"{indent}           docs: {_tok(row['doc_read_bytes'])} bytes via Read · "
              f"{_tok(row['doc_dump_bash_bytes'])} bytes dumped via bash · "
              f"{_tok(row['read_tool_bytes'])} bytes of Read results")
    print(f"{indent}  {payload['before']}")
    print(f"{indent}  {payload['doc_before']}")


def cmd_navigation(args: argparse.Namespace) -> int:
    from . import ops

    res = ops.navigation_report(args.target, args.project,
                                fleet=args.fleet, days=args.days)
    if args.json:
        _print(res, True)
        return 0
    print(f"{res['scope']}")
    _print_navigation(res)
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from . import ops

    # `with_navigation`: this renderer PRINTS that section
    res = ops.inspect_report(args.target, args.project,
                             write_floor=args.writes_over, join_floor=args.joins_over,
                             with_navigation=True)
    if args.json:
        _print(res, True)
        return 0
    # A feature order is many units and needs a line saying which feature they are
    if len(res["units"]) != 1 or res["units"][0]["wo_id"] != res["scope"]:
        print(f"{res['scope']} — {res['title']}\n")
    for index, unit in enumerate(res["units"]):
        if index:
            print()
        _print_anatomy(unit, res["write_floor"], params=args.params)
    return 0


def _print_live(payload: dict[str, Any]) -> None:
    """One frame, in the order §3 asks the eye to read it"""
    print(f"{payload['wo_id']} · {payload['project']} · {payload['state']}")
    print("=" * RULE_WIDTH)
    if not payload["found"]:
        # No counters at all, not zeroed ones: absent is never zero (issue #227)
        print(f"  {payload['note']}")
        return

    # THE LARGEST THING ON THE SCREEN is what it is doing and for how long
    live_now = payload["now"]
    if live_now:
        print(f"\n  {live_now['tool'].upper()}   {live_now['elapsed']:.0f}s")
        if live_now["detail"]:
            print(f"  {live_now['detail']}")
    else:
        print(f"\n  {payload['note']}")

    turn = payload["turn"]
    if turn:
        why = ", ".join(t["kind"] for t in turn["triggers"]) or "no prompt recorded"
        print(f"\n  turn {turn['seq']} · {_mins(turn['elapsed'])} · {why}")
    tok = payload["tokens"]
    print(f"  in {_tok(tok['input'])} · out {_tok(tok['output'])} · "
          f"cache write {_tok(tok['cache_write'])} · read {_tok(tok['cache_read'])} · "
          f"context {_tok(tok['context'])}")
    write = payload["last_write"]
    if write:
        print(f"  last cache write {_tok(write['written'])} — {write['cause']}: "
              f"{write['note']}")

    if payload["recent"]:
        print("\n  just finished:")
        for span in payload["recent"]:
            print(f"    {span['tool']:<12}{span['elapsed']:>7.1f}s  "
                  f"{span['detail']}")
    if payload["holds"]:
        print("\n  held:")
        for hold in payload["holds"]:
            print(f"    {_mins(hold['seconds']):>8}  {hold['phrase']}")
    print(f"\n  parameters shown to {payload['params_cap']} characters")


def cmd_watch(args: argparse.Namespace) -> int:
    from . import live, ops

    # ONE reader for the whole loop (Neo q678 (1)): it carries the resolved transcript path and the rows...
    reader = live.Reader()
    try:
        while True:
            payload = ops.live_report(args.target, args.project, reader=reader)
            if args.json:
                _print(payload, True)
            else:
                # Repaint rather than scroll, and only where there is a terminal to repaint: piped or redirected...
                if not args.once and sys.stdout.isatty():
                    print("\033[H\033[2J", end="")
                _print_live(payload)
            # Redirected stdout is block-buffered
            sys.stdout.flush()
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        # The normal way this command ends
        print()
        return 0


#: The `alarms` subcommands
ALARMS_SUBCOMMANDS = ("list", "show", "review")
ISSUES_SUBCOMMANDS = ("list", "start")


def _normalise_alarms(argv: list[str]) -> list[str]:
    """Insert the implicit `list` so `jarvis alarms [project]` keeps working verbatim"""
    if not argv or argv[0] != "alarms":
        return argv
    # `-h` is the one word that must NOT be read as a list argument: the reader asking for `jarvis alarms...
    if len(argv) > 1 and argv[1] in (*ALARMS_SUBCOMMANDS, "-h", "--help"):
        return argv
    return [argv[0], "list", *argv[1:]]


def _normalise_issues(argv: list[str]) -> list[str]:
    """`_normalise_alarms` for `jarvis issues [project]` — see there for the why"""
    if not argv or argv[0] != "issues":
        return argv
    if len(argv) > 1 and argv[1] in (*ISSUES_SUBCOMMANDS, "-h", "--help"):
        return argv
    return [argv[0], "list", *argv[1:]]


#: The `investigate` sub-verbs — a CLOSED set
INVESTIGATE_SUBCOMMANDS = ("create", "list", "show", "cancel", "verdict", "budget")


def _normalise_investigate(argv: list[str]) -> list[str]:
    """Insert the implicit `create` so `jarvis investigate <subject> --why …` parses"""
    if not argv or argv[0] != "investigate":
        return argv
    if len(argv) > 1 and argv[1] in (*INVESTIGATE_SUBCOMMANDS, "-h", "--help"):
        return argv
    return [argv[0], "create", *argv[1:]]


def cmd_issues_start(args: argparse.Namespace) -> int:
    """Start work on a tracker issue"""
    from . import issues as issues_mod

    out = issues_mod.start_work(args.issue, args.project)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"{out['wo_id']}  {out['project']}  {out['issue_url']}"
          + (f"  `{out['priority']}`" if out["priority"] else ""))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """`jarvis search` — the find-it-again verb, over every record the OS keeps"""
    from . import search as search_mod

    hits = search_mod.search(args.query, project=args.project,
                             kinds=tuple(args.kind) if args.kind else None,
                             limit=args.limit)
    if args.json:
        _print(hits, True)
        return 0
    if not hits:
        where = f" in {args.project}" if args.project else ""
        print(f"nothing matches {args.query!r}{where}")
        return 0
    print("  ".join(f"{kind} {n}" for kind, n in search_mod.counts(hits)))
    for hit in hits:
        status = f" [{hit['status']}]" if hit["status"] else ""
        archived = ", archived" if hit["archived"] else ""
        print(f"\n{hit['kind']}  {hit['id']}  ({hit['project']}{archived}){status}  "
              f"{_age(hit['ts'])}")
        print(f"  {hit['title']}")
        if hit["snippet"]:
            print(f"  {hit['snippet']}")
        print(f"  {hit['ref']}")
    return 0


def cmd_spec(args: argparse.Namespace) -> int:
    """`jarvis spec toc | section | search` — §4.2 of the navigate-specs-like-code spec"""
    from . import ops

    if args.spec_cmd == "toc":
        out = ops.spec_toc(args.path, args.project)
        if args.json:
            _print(out, True)
            return 0
        print(f"{out['path']} — {out['tokens_estimate']} tokens (estimate)")
        for row in out["sections"]:
            print(f"  {'  ' * (row['level'] - 1)}{row['name']}  "
                  f"line {row['line']}  ~{row['tokens_estimate']} tokens (estimate)")
            print(f"  {'  ' * (row['level'] - 1)}  {row['command']}")
        return 0

    if args.spec_cmd == "section":
        out = ops.spec_section(args.path, args.which, args.project)
        if args.json:
            _print(out, True)
            return 0
        print(f"{out['path']} — {out['which']} — {out['tokens_estimate']} tokens "
              f"(estimate)")
        print(out["content"])
        return 0

    out = ops.spec_search(args.words, args.project, limit=args.limit)
    if args.json:
        _print(out, True)
        return 0
    if not out["hits"]:
        print(f"nothing in {out['project']} matches {out['words']!r}")
        return 0
    print(f"{out['count']} section(s) in {out['project']}"
          + (f" — capped at --limit {out['limit']}" if out["truncated"] else ""))
    for hit in out["hits"]:
        print(f"\n{hit['path']}  line {hit['line']}")
        print(f"  {hit['context']}")
        print(f"  {hit['command']}")
    return 0


def cmd_issues(args: argparse.Namespace) -> int:
    """Which tracker issues the fleet keeps running into — the priority signal"""
    from . import ops

    rows = ops.issue_board_across(args.project, limit=args.limit)
    if args.json:
        _print(rows, True)
        return 0
    if not rows:
        print("no tracker issue is linked to any work order yet")
        return 0
    for row in rows:
        title = row["title"] or row["issue_url"]
        where = "internal" if row.get("internal") else f"#{row['number']}"
        print(f"{row['refs']:>3}  {where}  [{row['project']}] {title}")
        # An internal follow-up is one the panel raised on a repository the OS could not establish is...
        print(f"     {row['issue_url']}" if row["issue_url"]
              else "     kept on the internal record — no tracker issue was opened")
        who = ", ".join(f"{u['unit_id']} ({u['kind']})" for u in row["units"])
        print(f"     {who}")
    return 0


def cmd_steward(args: argparse.Namespace) -> int:
    """`jarvis steward …` — read-only views of the steward's ledger and decisions"""
    from dataclasses import asdict

    from . import ops, steward
    from .central_store import CentralStore

    central = CentralStore()
    try:
        if args.steward_cmd == "digest":
            print(steward.render_digest(steward.digest(ops.resolve_catalog(), central)))
            return 0
        if args.steward_cmd == "ledger":
            catalog = ops.resolve_catalog()
            cfg = catalog.os.evolution
            book = steward.ledger(catalog, central)
            if args.json:
                print(json.dumps({"mode": cfg.mode, **asdict(book),
                                  "weekly_usd": cfg.weekly_usd,
                                  "critical_weekly_usd": cfg.critical_weekly_usd,
                                  "yield_above_usd": cfg.yield_above_usd}))
                return 0
            print(f"mode {cfg.mode}; self_dispatch "
                  f"{'on' if catalog.os.autonomy.self_dispatch else 'off'}")
            print(f"evolution  ${book.evolution_usd:8.2f} of ${cfg.weekly_usd:.2f}")
            print(f"critical   ${book.critical_usd:8.2f} of ${cfg.critical_weekly_usd:.2f}")
            print(f"fleet      ${book.fleet_usd:8.2f} (new self-work waits above "
                  f"${cfg.yield_above_usd:.2f})")
            return 0
        rows = (ops.steward_why(args.wo_id) if args.steward_cmd == "why"
                else central.steward_decisions(limit=args.limit))
        if args.json:
            print(json.dumps(rows))
            return 0
        if not rows:
            print("no steward decisions" + (f" about {args.wo_id}"
                                            if args.steward_cmd == "why" else ""))
            return 0
        queue = central.steward_queue_counts()
        if args.steward_cmd == "decisions" and any(queue.values()):
            print(steward.queue_line(queue))
        for r in rows:
            applied = "applied" if r["applied"] else r["mode"]
            to = f" -> {r['wo_id']}" if r["wo_id"] else ""
            print(f"#{r['id']} {_age(r['ts'])} ago [{r['outcome']}/{applied}] "
                  f"{r['origin_kind']} {r['severity']}"
                  + (f" (claimed {r['claimed_severity']})"
                     if r["claimed_severity"] and r["claimed_severity"] != r["severity"]
                     else "")
                  + f" gen {r['generation']}: {r['subject'][:80]}{to}")
            print(f"    {r['rule']}: {r['reason']}")
            if r.get("queue_state"):
                # Queued means deferred: say whether it still waits, and what happened last
                note = f": {r['queue_note']}" if r.get("queue_note") else ""
                print(f"    queue {r['queue_state']}{note}")
        return 0
    finally:
        central.close()


def cmd_alarms(args: argparse.Namespace) -> int:
    """The dashboard's `/alarms` page in the terminal — the CLI is the OS"""
    from . import ops

    if args.alarms_cmd == "show":
        return cmd_alarms_show(args)
    if args.alarms_cmd == "review":
        return cmd_alarms_review(args)
    # `alarm_feed`, not `list_cost_alarms`: the probe's title and the remedy are not on the frozen dict
    rows = ops.alarm_feed(args.project, limit=args.limit, wo_id=args.wo,
                          fo_id=args.fo,
                          sources=(args.source,) if args.source else None)
    if args.json:
        _print(rows, True)
        return 0
    if not rows:
        subject = args.wo or args.fo
        print(f"no cost alarm has ever been raised against {subject}" if subject
              else "no cost alarm has ever been raised")
        return 0
    live = [r for r in rows if r["live"]]
    print(f"{len(live)} asking for you · {len(rows) - len(live)} on the record\n")
    for row in rows:
        mark = "!" if row["live"] else " "
        # The id only on the reads that asked for one subject: §1 of... (docs/superpowers/specs/2026-08-31-the-supervisor.md)
        who = (f"{row['id']}  " if (args.wo or args.fo)
               else f"{row['subject_id']}  {row['project']}  ")
        print(f"{mark} {who}{row['kind_label']}  "
              f"{ops.turn_label(row['seq'])}  {_age(row['ts'])} ago")
        print(f"    {row['reason']}")
        if row["remedy"]:
            print(f"    remedy: {ops.remedy_line(row)}")
    if live:
        print("\nack one with: jarvis wo ack <wo-id>")
    return 0


def cmd_stuck(args: argparse.Namespace) -> int:
    """The dashboard's `/stuck` page in the terminal. §7, and `cmd_alarms` in shape"""
    from . import ops

    rows = ops.stuck_report(args.project)
    if args.json:
        _print(rows, True)
        return 0
    if not rows:
        print("no open order is being judged — `fleet_health.enabled` is off, or there "
              "is nothing open")
        return 0
    over = [r for r in rows if r["stuck"]]
    print(f"{len(over)} past its threshold · {len(rows) - len(over)} on the record\n")
    for row in rows[:args.limit]:
        mark = "!" if row["stuck"] else " "
        print(f"{mark} {row['id']}  {row['project']}  {row['status_label']}")
        print(f"    {_hours(row['seconds_in_status'])} in status · "
              f"{_hours(row['seconds_since_activity'])} since activity · "
              f"threshold {_hours(row['threshold_seconds'])} on the "
              f"{row['clock']} clock")
        print(f"    {row['reason']}")
        # Parity with `ui/templates/stuck.html`, worded the same (§7)
        print(f"    what the record says blocks it: {row['blocker']}")
        inv = row["investigation"]
        if inv:
            print(f"    {inv['id']}  {inv['status']}"
                  f"{'  ' + inv['classification'] if inv['classification'] else ''}")
    if over:
        print("\nread one with: jarvis investigate show <inv-id>")
    # TEXT, not a link: the Evolution view is fo-69ba1cc4's and has no route yet (§7)
    print("the gap classes behind these: jarvis rules list")
    return 0


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def cmd_alarms_show(args: argparse.Namespace) -> int:
    """One alarm in full, the terminal's half of `/alarms/<project>/<al-id>`"""
    from . import ops

    a = ops.alarm_detail(args.alarm_id, project_name=args.project)
    if args.json:
        _print(a, True)
        return 0
    print(f"{a['id']}  {a['project']}  {a['subject_id']}  {a['title']}")
    print(f"  {a['kind_label']}  {ops.turn_label(a['seq'])}  {_age(a['ts'])} ago"
          f"{'  ! still asking' if a['live'] else ''}")
    print(f"  what fired: {a['reason']}")
    if a["verdict"]:
        decided = f"  {_age(a['decided_at'])} ago" if a["decided_at"] else ""
        print(f"\n  supervisor: {a['verdict']}{decided}")
        print(f"    why:  {a['verdict_reason'] or '—'}")
        print(f"    note: {a['note'] or '—'}")
    else:
        print(f"\n  supervisor: {a['alarm_status']} — not decided")
    if a["remedy"]:
        print(f"    remedy: {ops.remedy_line(a)}")
    if a["neo_question_id"]:
        print(f"  neo (#{a['neo_question_id']}): {a['neo_advice'] or 'not answered yet'}")
    # The evidence packet, on the terminal's half of the page that carries it: it is stored on the gate...
    if a["gate"] and a["gate"]["evidence"]:
        print(f"\n  what it was looking at:\n"
              + "\n".join(f"    {line}" for line in a["gate"]["evidence"].splitlines()))
    if a["alarm_status"] == "proposed":
        gate = a["remedy_approval_id"] or "<id>"
        print(f"\nit is asking permission to act — answer it with: "
              f"jarvis gate approve {gate} | jarvis gate deny {gate} --reason \"…\"")
    elif a["review_status"] == "unreviewed":
        if a["verdict"]:
            print(f"\nreview it: jarvis alarms review {a['id']} "
                  f"[--reject --feedback \"…\"]")
    else:
        print(f"\n  you {a['review_status']} this"
              f"{': ' + a['review_feedback'] if a['review_feedback'] else ''}")
    return 0


def cmd_alarms_review(args: argparse.Namespace) -> int:
    """Approve or correct the supervisor"""
    from . import ops

    res = ops.review_alarm(args.alarm_id, approved=not args.reject,
                           feedback=args.feedback, project_name=args.project)
    if args.json:
        _print(res, True)
        return 0
    print(f"{res['alarm_id']}: {res['review']}")
    if res["neo_question_closed"]:
        print("  the Neo question it escalated is closed — you have just answered it")
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    """`jarvis supervisor probes [project]` — the symptom catalogue, resolved"""
    from . import ops

    rows = ops.supervisor_probes(args.project, catalog_path=args.catalog)
    if args.json:
        _print(rows, True)
        return 0

    scope = f"project {args.project}" if args.project else "the fleet"
    print(f"health probes for {scope} ({len(rows)}, "
          f"{sum(1 for r in rows if r['enabled'])} enabled)\n")
    for row in rows:
        mark = "·" if row["enabled"] else "✗"
        print(f"{mark} {row['id']:<20} {row['source']:<17} "
              f"{'' if row['enabled'] else 'disabled  '}"
              f"{'+'.join(row['subjects'])}")
        print(f"    {row['title']} — {_one_line(row['prompt'], 110)}")
    if not rows:
        print("no probes are configured — this project is watched for nothing")
    return 0


def _one_line(text: str, limit: int) -> str:
    """A paragraph as a single clipped line, for a listing"""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def _when(text: str) -> float:
    """A `--since`/`--until` value, parsed the one way the OS parses a window bound"""
    from .compaction_payoff import parse_when

    try:
        return parse_when(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date or ISO datetime: {e}")


def _cost_meter(picked: dict, project: str | None) -> dict | None:
    """The meter subtree, or None"""
    import logging

    from . import ops

    try:
        return ops.cost_meter(resolved=picked, project=project)["meter"]
    except Exception as e:                              # noqa: BLE001
        logging.getLogger(__name__).warning("cost meter unavailable: %s", e)
        return None


def cmd_cost(args: argparse.Namespace) -> int:
    from . import ops
    # One argument, three kinds of thing (docs/superpowers/specs/2026-09-23-improvement-orders.md)
    from .project_store import is_feature_order_id

    target = args.target
    is_id = bool(target) and (target.startswith("wo-") or is_feature_order_id(target))
    scope = args.project or (None if is_id else target) or None
    # THE ZONE, THEN THE WINDOW, ONCE at the top and handed to every payload builder below (kn-9ccc429b:...
    zone = ops.cost_zone(args.tz, scope)
    picked = ops.cost_window(project=scope, window=args.window, offset=args.offset,
                             tz=zone, since=args.since, until=args.until)
    # The distribution BRANCHES FIRST and the listing below is untouched: `--fleet` adds a section
    if getattr(args, "fleet", False):
        payload = ops.fleet_cost(project=scope, resolved=picked)
        meter = _cost_meter(picked, scope)
        if args.json:
            _print({**payload, **({"meter": meter} if meter else {})}, True)
            return 0
        _print_fleet(payload["fleet"])
        if meter:
            print(f"\n{meter['sentence']}")
        return 0
    if is_id:
        # One order asks "where did MY tokens go", which is the bill's question
        bill = ops.bill(target)
        if args.json:
            _print(bill, True)
            return 0
        _print_bill(bill)
        return 0
    res = ops.cost_report(project=scope, limit=args.limit, window=picked)
    meter = _cost_meter(picked, scope)
    if args.json:
        _print({**res, **({"meter": meter} if meter else {})}, True)
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
    # VERBATIM from the payload: the page prints the same string
    if meter:
        print(f"{meter['sentence']}\n")
    # `$` is the whole bill — the worker's conversation
    print(f"{'$':>7} {'jarvis':>7} {'subproc':>8} {'subagent':>9} {'turns':>5} "
          f"{'output':>7} {'re-write':>9}  work order")
    for u in units:
        os_cost = f"{u['os_cost_usd']:.2f}" if u["os_calls"] else "—"
        sub_cost = f"{u['subproc_cost_usd']:.2f}" if u.get("subproc_calls") else "—"
        agent_cost = (f"{u['subagent_cost_usd']:.2f}"
                      if u.get("subagent_count") else "—")
        if not u["found"]:
            print(f"{'—':>7} {os_cost:>7} {sub_cost:>8} {agent_cost:>9} {'—':>5} "
                  f"{'—':>7} {'—':>9}  {u['id']}  {u['title'][:44]}")
            continue
        print(f"{u['total_cost_usd']:>7.2f} {os_cost:>7} {sub_cost:>8} "
              f"{agent_cost:>9} {u['turns']:>5} "
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
        # In full, never abbreviated: this is the number a reader quotes when asking why a call was that big... (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        if totals.get("os_max_input_chars"):
            print(f"                largest input "
                  f"{totals['os_max_input_chars']:,} characters "
                  f"(prompt + system prompt, of one call)")
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
        cause = _rewrite_cause(totals.get("rewrite_ttl_share"),
                               totals.get("boundaries_ttl") or 0,
                               totals["resume_boundaries"])
        if cause:
            print(f"               {cause.strip()}")
    if totals["subagent_cost_usd"]:
        print(f"  subagents     ~${totals['subagent_cost_usd']:.2f}")
    # Silence when zero: nothing was excluded (docs/superpowers/specs/2026-10-07-cost-window-selector.md)
    if totals.get("undated_messages"):
        print(f"  {totals['undated_messages']} transcript messages carried no readable "
              f"timestamp and are excluded from this window")
    _print_write_ttl(totals)
    unattributed = res.get("os_unattributed") or {}
    if unattributed.get("os_calls"):
        print(f"  OS overhead   ~${unattributed['os_cost_usd']:.2f} — "
              f"{unattributed['os_calls']} calls no work order caused "
              f"(already in the total above)")
    # Both said every time rather than in the help text
    if res.get("floor_reason"):
        print(f"\nEvery figure above is {res['floor_reason']}.")
    print("List prices, as a common unit for comparing token kinds — not a bill.")
    # Money is all this command has ever shown (docs/superpowers/specs/2026-09-24-order-observability.md)
    print("Where the time went: `jarvis inspect <id>` · what a turn is doing right now: "
          "`jarvis watch <id>` · why one is not moving: `jarvis wo why <id>`.")
    return 0


def _fleet_value(value: float | None, unit: str) -> str:
    """One figure, formatted by its UNIT — the renderer never guesses the metric"""
    if value is None:
        return "—"
    if unit == "usd":
        return f"${value:,.2f}"
    if unit == "tokens":
        return _tok(int(value))
    if unit == "seconds":
        return f"{value:,.0f}s"
    if unit == "share":
        return f"{100 * value:.1f}%"
    return f"{value:,.2f}"


def _tool_label(name: str, width: int = 28) -> str:
    """A tool name at display width, middle-elided"""
    if len(name) <= width:
        return name
    head = (width - 1) // 2
    return f"{name[:head]}…{name[head + 1 - width:]}"


def _main_sub(cost: dict) -> str:
    """The carried-dollar split as whole percents, or `—` where nothing was carried"""
    callers = cost.get("by_caller") or {}
    main = (callers.get("main") or {}).get("carried_usd", 0.0)
    sub = (callers.get("subagent") or {}).get("carried_usd", 0.0)
    if main + sub <= 0:
        return "—"
    return f"{round(100 * main / (main + sub))}% / {round(100 * sub / (main + sub))}%"


def _print_tool_cost(tools: dict) -> None:
    """§10.7: one compact table, the fleet's dearest TOOL habits, dearest first"""
    totals = tools.get("totals") or {}
    if not totals.get("calls"):
        return
    print(f"\ntool cost — {totals['calls']:,} calls · "
          f"{_tok(totals['result_tokens'])} result tok · "
          f"{_tok(totals['carried_tokens'])} carried tok · "
          f"${totals['carried_usd']:,.2f} carried")
    print(f"{'tool':<30} {'calls':>7} {'result tok':>11} {'carried tok':>12} "
          f"{'carried $':>10}  main/sub")
    from jarvis import fleetcost

    rows = fleetcost.tool_table(tools)
    limit = tools.get("row_limit") or len(rows)
    for label, cost in rows[:limit]:
        print(f"{_tool_label(label):<30} {cost['calls']:>7,} "
              f"{_tok(cost['result_tokens']):>11} "
              f"{_tok(cost['carried_tokens']):>12} "
              f"{cost['carried_usd']:>10,.2f}  {_main_sub(cost)}")
    rest = rows[limit:]
    if rest:
        print(f"({len(rest)} more tool{'s' if len(rest) != 1 else ''}, "
              f"{_tok(sum(c['result_tokens'] for _, c in rest))} result tok, "
              f"${sum(c['carried_usd'] for _, c in rest):,.2f} carried)")
    # The three money fields are never summed without their names
    ttl = totals["carried_rewrite_ttl_usd"]
    print(f"  carried $ by cause: cache read ${totals['carried_read_usd']:,.2f} · "
          f"ttl-expiry re-write "
          f"{'undecided' if ttl is None else f'${ttl:,.2f}'} · "
          f"prefix-miss re-write ${totals['carried_rewrite_prefix_usd']:,.2f}")
    basis = totals["token_basis"]
    excluded = tools["excluded"]
    walked = excluded["sessions_walked"]
    # Silent when zero: nothing was hidden, so there is nothing to disclose
    late = excluded["outside_window"]
    print(f"  {basis['context_delta']:,} result sizes measured exactly, "
          f"{basis['chars']:,} estimated from characters · "
          f"{walked:,} transcript{'s' if walked != 1 else ''} read · "
          f"{excluded['unmatched_calls']:,} calls with no result, excluded"
          + (f" · {late:,} tool results outside the window, excluded" if late else ""))


def _print_fleet(fleet: dict) -> None:
    """The distribution: one line per metric, then the OS's spend, then the payoff"""
    orders = fleet["orders"]
    print(f"{fleet['scope']} — {fleet['window']['label']} "
          f"({fleet['window']['source']})")
    # Both clocks: UTC is what `agent_calls.ts` is comparable to
    if fleet["window"].get("local_label"):
        print(f"  {fleet['window']['local_label']}")
    print(f"{orders['n']} order{'s' if orders['n'] != 1 else ''} with a turn in the "
          f"window · {orders['live']} live · {orders['truncated']} truncated by it · "
          f"{orders['excluded_no_turns']} with no turn in it\n")
    if not orders["n"]:
        print("nothing ran in that window — try --since/--until")
        return
    print(f"{'metric':<38} {'n':>4} {'avg':>10} {'p90':>10} {'max':>10}  basis")
    for name, m in fleet["metrics"].items():
        unit = m["unit"]
        top = m["max"] or {}
        basis = m["provenance"]
        if m["cost_basis"] and m["cost_basis"]["transcript"] and basis == "envelope":
            # Both sub-populations exist, so the reader is told so beside the figure
            basis = (f"envelope ({m['cost_basis']['envelope']} turns; "
                     f"{m['cost_basis']['transcript']} floored, reported separately)")
        print(f"{name:<38} {m['n']:>4} {_fleet_value(m['avg'], unit):>10} "
              f"{_fleet_value(m['p90'], unit):>10} "
              f"{_fleet_value(top.get('value'), unit):>10}  {basis}"
              f"{'  ' + top['wo_id'] if top.get('wo_id') else ''}")

    kinds = fleet["os_cost_by_kind"]
    if kinds:
        print("\nwhat jarvis itself spent, by kind (dearest first, $0 kinds included)")
        for row in kinds:
            print(f"  {row['kind']:<22} {row['calls']:>6} "
                  f"call{'s' if row['calls'] != 1 else ' '}  "
                  f"${row['cost_usd']:>10,.2f}")
        un = fleet["os_unattributed"]
        if un["calls"]:
            print(f"  {'(no work order)':<22} {un['calls']:>6} "
                  f"call{'s' if un['calls'] != 1 else ' '}  "
                  f"${un['cost_usd']:>10,.2f}")

    _print_tool_cost(fleet.get("tools") or {})

    payoff = fleet["compaction_payoff"]
    if payoff["count"]:
        print(f"\ncompactions {payoff['count']} · "
              f"cost ${payoff['total_cost_usd']:,.2f} · "
              f"saved ${payoff['total_savings_usd']:,.2f} · "
              f"net ${payoff['net_usd']:,.2f}")
        if payoff["pct_paid_off"] is not None:
            line = (f"  {payoff['pct_paid_off']}% of {payoff['closed']} closed one"
                    f"{'s' if payoff['closed'] != 1 else ''} paid off")
            if payoff["break_even_turn_median"] is not None:
                line += (f"; break-even at {payoff['break_even_turn_median']} turns "
                         f"(median)")
            print(line)
    print()
    notes = list(fleet["notes"])
    tools = fleet.get("tools") or {}
    if (tools.get("totals") or {}).get("calls"):
        notes += tools["notes"]
    for note in notes:
        print(f"{note[0].upper()}{note[1:]}.")
    print(f"\nEvery figure above is {fleet['floor_reason']}.")


def _print_orphans(orphans: list[dict]) -> None:
    """Leftover background agents from the pre-headless-turn transport"""
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


def cmd_offboard(args: argparse.Namespace) -> int:
    from . import ops
    if args.purge and not args.yes:
        raise SystemExit(
            f"refusing to purge {args.name}: purging deletes <project>/.jarvis/ and with "
            "it every work order, event and turn the project ever had, plus every row "
            "the OS keeps for it. Re-run with --purge --yes, or drop --purge to archive "
            "it and keep the history.")
    res = ops.offboard_project(args.name, purge=args.purge, force=args.force,
                               dry_run=args.dry_run, catalog_path=args.catalog,
                               reason=args.reason)
    if args.json:
        _print(res, True)
        return 0
    verb = "would" if res["dry_run"] else "did"
    purged = sum(n for counts in res["purged"].values() for n in counts.values())
    print(f"{res['project']}: {res['mode']}{' (dry run — nothing changed)' if res['dry_run'] else ''}")
    print(f"  orders: {len(res['open'])} open, {len(res['cancelled'])} cancelled, "
          f"{res['sessions_stopped']} session(s) stopped")
    print(f"  repo: {len(res['strip']['actions'])} action(s), "
          f"{len(res['strip']['warnings'])} file(s) refused")
    print(f"  registry: {res['registry']}" + (f" ({purged} row(s) deleted)" if purged else ""))
    cat = res["catalog"]
    print("  catalog: " + (f"{verb} remove its entry from {cat['path']}"
                            if cat["in_catalog"] else f"no entry in {cat['path']}")
          + (f" (version {cat['version']})" if cat["version"] else ""))
    for o in res["open"]:
        print(f"    {o['id']}  {o['status']}  {o['title']}")
    for a in res["strip"]["actions"]:
        print(f"    {a}")
    for w in res["strip"]["warnings"]:
        print(f"    warning: {w}")
    return 0


def _parse_budget_amount(raw: str) -> float:
    """A dollar amount off the command line, refused at the surface rather than deeper"""
    from . import budget, ops

    try:
        return budget.parse_amount(raw)
    except ValueError as e:
        raise ops.OpsError(str(e)) from e


def _budget_arg(args: argparse.Namespace) -> float | None:
    """`--budget` as a number, or None for "no ceiling" — which is every order today"""
    raw = getattr(args, "budget", None)
    return _parse_budget_amount(raw) if raw else None


def _print_context(res: dict[str, Any]) -> None:
    """`jarvis wo context` for a human (docs/superpowers/specs/2026-09-24-order-observability.md §5)"""
    print(f"{res['wo_id']}  {res['project']}  {res['title']}")
    # Above the early return, `_print_anatomy`'s rule: a reader whose ledger is empty is the one who most...
    _print_autopsy_provenance(res.get("provenance"))
    if not res["recorded"]:
        print(f"\n  {res['note']}")
        return
    for turn in res["turns"]:
        print(f"\nturn {turn['seq']} ({turn['kind']})")
        if not turn["recorded"]:
            print(f"  {turn['note']}")
            continue
        print("  ingredient            bytes      tokens (all token figures are "
              "estimates)")
        for row in turn["ingredients"]:
            size = f"{row['bytes']:,}" if row["bytes"] is not None else "—"
            tokens = f"{row['tokens']:,}" if row["tokens"] is not None else "—"
            mark = "  (estimate)" if row["estimated"] and not row["absent"] else ""
            print(f"  {row['name']:<20}{size:>10}{tokens:>12}{mark}")
            if row["note"]:
                print(f"      {row['note']}")
        residual = turn["residual"]
        if residual is None:
            print(f"  residual: {turn['residual_note']}")
        else:
            tokens = (f"{residual['tokens']:,}" if residual["tokens"] is not None
                      else "—")
            print(f"  {residual['name']:<20}{'—':>10}{tokens:>12}  (INFERRED, not "
                  f"measured)")
            print(f"      {residual['note']}")
        delta = turn["delta"]
        if delta:
            print(f"  since turn {delta['against_seq']}:")
            for row in delta["rows"]:
                if row["kind"] == "absent" or (row["kind"] == "change"
                                               and not row["bytes_delta"]):
                    continue
                change = (f"{row['bytes_delta']:+,} bytes" if row["bytes_delta"]
                          is not None else row["kind"])
                print(f"    {row['name']:<20}{change}"
                      + (f"   {row['note']}" if row["note"] else ""))
        break_ = turn["prefix_break"]
        if break_:
            print(f"  {break_['cause']}")
            print(f"      {break_['authority']}")
            print(f"      {break_['note']}")


def cmd_wo(args: argparse.Namespace) -> int:
    from . import invariants, ops, specs
    from .project_store import OPEN_STATUSES, ProjectStore
    from .timeline import build_conversation, build_timeline

    if args.wo_cmd == "create":
        deps = [d.strip() for d in args.depends_on.split(",") if d.strip()]
        holds = list(getattr(args, "blocked_by", None) or [])
        parent = (args.parent or "").strip()
        wo = ops.create_work_order(
            args.project, args.title, description=args.description, origin=args.origin,
            model=args.model, effort=args.effort, permission_mode=args.permission_mode,
            append_system_prompt=args.append_system_prompt, depends_on=deps,
            # Through unchanged, on the four self-evolution flags' shape — §2.3
            blocked_by=holds,
            parent_id=parent or None, budget_usd=_budget_arg(args),
            observability=getattr(args, "observability", None),
            # Verbatim from the flags — §7.1's "set structurally and never inferred from prose"
            gap_class=(getattr(args, "gap_class", "") or "").strip() or None,
            detector_id=(getattr(args, "detector_id", "") or "").strip() or None,
            remedy_rule_id=(getattr(args, "remedy_rule_id", "") or "").strip() or None,
            trigger_wo_id=(getattr(args, "trigger_wo_id", "") or "").strip() or None,
        )
        _print({"created": wo["id"], "project": args.project, "status": wo["status"],
                "depends_on": deps,
                **({"parent": parent} if parent else {}),
                **({"gap_class": wo["gap_class"]} if wo.get("gap_class") else {}),
                **({"budget_usd": wo["budget_usd"]} if wo.get("budget_usd") else {}),
                # A HELD ORDER IS NOT DISPATCHED SHORTLY — §2.3
                **({"blocked_by": holds} if holds else {}),
                "note": (f"nothing is dispatched until {', '.join(holds)} clears "
                         f"(`jarvis wo unblock {wo['id']}` cuts the hold)" if holds
                         else f"jarvisd will dispatch it once {', '.join(deps)} completes"
                         if deps else "jarvisd will dispatch it shortly")}, args.json)

    elif args.wo_cmd == "budget":
        if args.amount is None and not args.clear:
            _print(ops.work_order_budget(args.wo_id, args.project), args.json)
        else:
            amount = None if args.clear else _parse_budget_amount(args.amount)
            _print(ops.set_work_order_budget(args.wo_id, amount, args.project),
                   args.json)

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
                # Derived inside the store's lifetime: the label reads the dependencies' rows (#714)
                account = ops.fleet_if_held(wos)
                labels = {wo["id"]: invariants.status_label(store, wo, account)
                          for wo in wos}
            finally:
                store.close()
            for wo in wos:
                out.append({"project": name, **{k: wo[k] for k in (
                    "id", "title", "status", "origin", "needs_attention",
                    "attention_reason", "created_at", "hidden", "pr_url")},
                    # WHY IT IS SITTING THERE, once the flag is down
                    "seen_blockers": invariants.acknowledged(wo),
                    "status_label": labels[wo["id"]]})
        # Running first, then the PRs waiting to be merged
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
                # Only for an OPEN order with the flag already down: a closed one's old dismissals are history
                seen = "".join(f"\n    ↩ already seen: {b}" for b in wo["seen_blockers"]) \
                    if not wo["needs_attention"] and wo["status"] in OPEN_STATUSES else ""
                print(f"{icon} {wo['id']} [{wo['project']}] [{badge}] "
                      f"{wo['title']} ({wo['status_label']}, "
                      f"{_age(wo['created_at'])}){att}{hid}{pr}{seen}")
            if not out:
                print("no work orders")

    elif args.wo_cmd == "show":
        name, path, wo = ops.find_work_order(args.wo_id, args.project)
        store = ProjectStore(path)
        try:
            # THE NEWEST PAGE, and the cut said out loud (io-12b7d873)
            events = store.list_events(args.wo_id, limit=args.events or None,
                                       newest=True)
            hidden = store.count_events(args.wo_id) - len(events)
            messages = store.list_messages(args.wo_id)
            detail = {
                "project": name, **wo,
                "status_label": invariants.status_label(store, wo,
                                                        ops.fleet_if_held([wo])),
                "blocked_by": store.unfinished_dependencies(args.wo_id),
                # The other kind of hold, always present and empty when there are none (`validation_rounds`'... (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)
                "preconditions": ops.open_preconditions(store, wo),
                # What has already been dismissed on this order, decoded
                "seen_blockers": invariants.acknowledged(wo),
                # Always present under `--json`: a quiet order and a cut one must read differently
                "events_shown": len(events),
                "events_hidden": hidden,
                "timeline": build_timeline(wo, events, messages,
                                           include_debug=args.debug),
                # What was said, in order, whoever spoke
                "conversation": build_conversation(events, messages),
                # Every assumption, each with its `n`, its `status` and what the OS did with it (`os_ruling`) (#712)
                "assumptions": ops.assumptions_with_rulings(store, args.wo_id),
                # What this work order was allowed (or refused) permission to ship
                "gates": [
                    {k: a[k] for k in ("id", "kind", "command", "status", "escalated",
                                       "contested", "closed_as", "decided_by",
                                       "decision_reason")}
                    for a in store.list_approvals(args.wo_id)
                ],
                # Additive, and always present under `--json` even when empty: a key that comes and goes is one every...
                "validation_rounds": ops.validation_rounds(store, wo_id=args.wo_id),
                # EVERY FOLLOW-UP THIS ORDER RAISED, across every round, and every other issue it points at
                "issues": ops.issue_index(store, args.wo_id),
                # WHAT THE OS READ OFF DISK when this order's latest turn died without writing a result (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)
                **({"harvest": h} if (h := ops.harvest_state(store, wo)) else {}),
                # Whether the OS merged this pull request, is waiting for permission to, or is holding — and why
                **({"auto_merge": state}
                   if (state := ops.automerge_state(store, wo)) else {}),
                # Whether that pull request landed, from the timeline and never from `pr_state`
                **({"merge_state": merged}
                   if (merged := ops.merge_state(store, wo)) else {}),
                # And the same for the assumption review
                **({"auto_review": review}
                   if (review := ops.autoreview_state(store, wo)) else {}),
                # THE DECISION THIS ORDER OWES THE USER, worded exactly as the dashboard's buttons word it
                **({"review": owed}
                   if (owed := ops.review_state(store, wo)) else {}),
                # WHERE THE SPEC CAN BE READ — the feature's document
                **({"spec": link} if (link := specs.spec_link(store, wo)) else {}),
                # The rows themselves, on the same always-present rule: this order's own alarms
                "alarms": store.alarms_of(args.wo_id),
                # HOW LONG IT HAS BEEN WHERE IT IS, and how long it spent in each status before that
                "time_in_state": ops.state_durations(store,
                                                     wo_id=args.wo_id).as_dict(),
                # THE CEILING AND WHAT HAS GONE AGAINST IT
            }
        finally:
            store.close()
        detail["budget"] = ops.work_order_budget(args.wo_id, name)
        # WHAT A FIX ORDER CARRIES (§7.1)
        if (evolution := ops.self_evolution_state(wo)):
            detail["self_evolution"] = evolution
        _print(_readable_spec(_readable_config(_readable_review(_readable_autoreview(
            _readable_automerge(_readable_harvest(_readable_alarms(
                _readable_time_in_state(_readable_rounds(_readable_issues(
                    _readable_self_evolution(_readable_preconditions(
                        _readable_events(_readable_conversation(detail))))))))))))))
               if not args.json else detail, args.json)

    elif args.wo_cmd == "send":
        # `relay=True` marks a surface the human reaches
        _print(ops.send_message(args.wo_id, args.message, source=args.source,
                                project_name=args.project, relay=True), args.json)
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
                          evidence=args.evidence, abandon=args.abandon,
                          superseded_by=args.superseded_by,
                          needs_user=args.needs_user), args.json)
    elif args.wo_cmd == "review":
        _print(ops.review_work_order(args.wo_id, accept=not args.reject,
                                     feedback=args.feedback,
                                     false_alarm=args.false_alarm), args.json)
    elif args.wo_cmd == "cancel":
        _print(ops.cancel(args.wo_id), args.json)
    elif args.wo_cmd == "done":
        _print(ops.mark_done(args.wo_id, project_name=args.project), args.json)
    elif args.wo_cmd == "pr":
        _print(ops.record_pull_request(args.wo_id, args.url,
                                       project_name=args.project), args.json)
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
    elif args.wo_cmd == "context":
        res = ops.context_report(args.wo_id, args.project, turn=args.turn)
        if args.json:
            _print(res, True)
        else:
            _print_context(res)
    elif args.wo_cmd == "resume-auto":
        _print(ops.resume_in_auto(args.wo_id, project_name=args.project,
                                  force=args.force), args.json)
    elif args.wo_cmd == "retry":
        # `relay=True` as `send` does: `ops.user_authorship` decides whether this process is a surface the...
        _print(ops.retry(args.wo_id, message=args.message, model=args.model,
                         project_name=args.project, relay=True), args.json)
    elif args.wo_cmd == "respawn":
        _print(ops.respawn_worker(args.wo_id, project_name=args.project), args.json)
    elif args.wo_cmd == "why":
        diagnosis = ops.diagnose(args.wo_id, project_name=args.project)
        if args.json:
            _print(diagnosis, True)
        else:
            _print_diagnosis(diagnosis)
    elif args.wo_cmd == "fix":
        proposal = ops.fix(args.wo_id, project_name=args.project, remedy=args.remedy,
                           argument=args.argument, confirm=args.confirm)
        if args.json:
            _print(proposal, True)
        else:
            _print_fix(proposal)
    return 0


def _print_diagnosis(d: dict[str, Any]) -> None:
    """`jarvis wo why` for a person — A RENDERER AND NOTHING ELSE"""
    print(f"{d['wo_id']} — {d['title']}  ({d['project']})")
    print(f"  {d['status_label']}")
    print(f"\nwaiting on: {d['blocker']['what']} — {d['blocker']['detail']}")
    # §8 of docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md
    if d["actor"]:
        print(f"  who will act: {d['actor']['pass_name']} · "
              f"{d['actor']['predicate']} — {d['actor']['why']}")
    else:
        print("  who will act: no daemon pass will act on this order")
    for blocker in d["needs_you"]:
        print(f"  needs you: {blocker}")
    for note in (d["notes"]["parked"], d["notes"]["pause"], d["notes"]["fleet_hold"]):
        if note:
            print(f"  {note}")
    for sentence in d["disagreements"]:
        print(f"  ⚠ {sentence}")

    clock = d["clock"]
    print("\nthe OS's record:")
    if clock["last_status"]:
        print(f"  last status    {clock['last_status']['status']} · "
              f"{clock['last_status']['ago']} ago")
    if clock["last_turn"]:
        turn = clock["last_turn"]
        print(f"  last turn      {turn['label']} {turn['state']} · "
              f"{turn['ago']} ago")
    if clock["last_event"]:
        print(f"  last event     {clock['last_event']['kind']} · "
              f"{clock['last_event']['ago']} ago")
    if clock["note"]:
        print(f"  {clock['note']}")
    print(f"  {clock['live_note']}")

    held = d["holds"]
    if held["episodes"]:
        print("\nheld by the OS:")
        for episode in held["episodes"]:
            mark = " (still held)" if episode["open"] else ""
            print(f"  {episode['phrase']} — {episode['seconds_human']}{mark}")
    # Where `_diagnose_holds`' reading surfaces, so its provenance belongs here
    _print_autopsy_provenance(held.get("provenance"))
    residual = held["unexplained"]
    if residual["seconds"] is None:
        print(f"  unexplained: none measurable — {residual['note']}")
    elif residual["seconds"]:
        print(f"  unexplained: {residual['seconds_human']} — "
              f"{residual['note']}")

    calls = d["os_calls"]
    if calls["calls"]:
        print(f"\nthe OS's own calls for this order: {calls['calls']}, "
              f"{calls['failed']} unreachable")
        # Newest first, which is the order `_os_calls_detail` returns
        for row in calls["rows"][:calls["shown_limit"]]:
            print(f"  {row['outcome']:>11}  {row['kind']} · {row['label']}")
        if calls["more"]:
            print(f"  … and {calls['more']} older call(s) — `jarvis wo why "
                  f"{d['wo_id']} --json` has them all")
        if calls["failed"] or calls["capped"]:
            print(f"  {calls['note']}")

    print(f"\n{d['way_through']['note']}")
    for command in d["commands"]:
        print(f"  {command['command']}\n      {command['why']}")
    for refusal in d["refusals"]:
        print(f"  not offered: {refusal}")


def _print_fix(f: dict[str, Any]) -> None:
    """`jarvis wo fix` for a person — A RENDERER AND NOTHING ELSE, `_print_diagnosis`'s rule (#227)"""
    print(f"{f['wo_id']} — {f['title']}  ({f['project']})")
    print(f"  {f['status']}")
    print(f"\nwaiting on: {f['blocker']['what']} — {f['blocker']['detail']}")
    print(f"\n{f['note']}")

    proposal = f["proposal"]
    if proposal:
        print(f"\nthe `{proposal['remedy']}` remedy on {proposal['subject']}:")
        print(f"  what it does: {proposal['headline']}")
        print(f"  what it touches, and what it cannot undo: {proposal['blast']}")
        print(f"  what it is asked to do: {proposal['argument']}")
        print(f"  {proposal['approving']}")

    move = f["your_move"]
    if move:
        print(f"\n  {move['detail']}\n      {move['note']}")

    filed = f["filed"]
    if filed:
        print(f"\n{filed['note']}")
        if filed["question"]:
            print(f"  Neo question {filed['question']} carries it to a reviewer")
        if filed["unreachable"]:
            print(f"  {filed['reason']}")


FO_ICON = {"pending": "⏳", "planning": "🧭", "plan_review": "👀", "executing": "🟢",
           "validating": "🔎", "budget_exhausted": "💸", "completed": "✅",
           "failed": "❌", "cancelled": "🚫"}


def cmd_fo(args: argparse.Namespace) -> int:
    import json as _json

    from . import ops

    if args.fo_cmd == "create":
        fo = ops.create_feature_order(args.project, args.title,
                                      description=args.description, origin=args.origin,
                                      max_parallel=args.max_parallel,
                                      budget_usd=_budget_arg(args))
        _print({"created": fo["id"], "project": args.project, "status": fo["status"],
                **({"max_parallel": fo["max_parallel"]} if fo["max_parallel"] else {}),
                **({"budget_usd": fo["budget_usd"]} if fo.get("budget_usd") else {}),
                "note": "jarvisd will open a planner for it shortly; the plan comes "
                        "back for review before any work order is created"}, args.json)

    elif args.fo_cmd == "budget":
        if args.amount is None and not args.clear:
            _print(ops.feature_order_budget(args.fo_id, args.project), args.json)
        else:
            amount = None if args.clear else _parse_budget_amount(args.amount)
            _print(ops.set_feature_budget(args.fo_id, amount, args.project), args.json)

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
        # Always present, even as nulls, on `jarvis wo show`'s rule: the family budget is a number the user...
        detail["budget"] = ops.feature_order_budget(args.fo_id, detail["project"])
        if args.json:
            _print(detail, True)
        else:
            detail = _readable_issues(detail)
            print(f"{FO_ICON.get(detail['status'], '•')} {detail['id']} "
                  f"[{detail['project']}] {detail['title']} ({detail['status']})")
            print(f"\n{detail['description']}\n")
            if detail["attention_reason"]:
                print(f"⚠ {detail['attention_reason']}\n")
            if detail["budget"]["budget_usd"]:
                b = detail["budget"]
                print(f"budget: ${b['spent_usd']:.2f} of ${b['budget_usd']:.2f} "
                      f"(${b['unreserved_usd']:.2f} unreserved)\n")
            if detail["planner"]:
                p = detail["planner"]
                print(f"planner: {p['id']} ({p['status']})")
            # Next to the planner, because they are the same question: who holds this feature
            if detail["manager"]:
                m = detail["manager"]
                print(f"manager: {m['id']} ({m['status']})")
            if detail["validation_rounds"]:
                print("\nvalidation:")
                for rnd in detail["validation_rounds"]:
                    print(f"  {ops.round_line(rnd)}")
            for heading, key in (("follow-ups raised", "follow_ups_raised"),
                                 ("follow-ups kept internally", "follow_ups_kept"),
                                 ("issues referenced", "issues_referenced")):
                if detail.get(key):
                    print(f"\n{heading}:")
                    for line in detail[key]:
                        print(f"  {line}")
            # `jarvis wo show`'s line, from the same formatter and in the same words
            if detail["alarms"]:
                print(f"\nalarms: {ops.alarm_standing_line(detail['alarms'])}")
            # The same block as `jarvis wo show`, from the same formatter — spec §6
            states = time_in_state_lines(detail["time_in_state"])
            if states:
                print("\ntime in state:")
                for line in states:
                    print(f"  {line}")
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
                    # Superseded rather than hidden: the row is still the feature's history
                    sup = " — superseded" if c.get("superseded") else ""
                    print(f"  {icon} {c['id']} {c['title']} "
                          f"({c['status_label']}{sup}){att}")

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

    elif args.fo_cmd == "agent":
        _print(ops.rebuild_feature_agent(args.fo_id, args.project), args.json)

    elif args.fo_cmd == "spec":
        res = ops.feature_spec(args.fo_id, args.project,
                               section=args.section or None)
        if args.json:
            _print(res, True)
        else:
            # One header line — the document and the revision it came from — then the markdown raw
            head = res["repo_path"]
            print(f"{head} — {res['source']}" if res["source"] else
                  f"{head} — the revision this text came from was not recorded")
            print(res["content"])

    elif args.fo_cmd == "resume":
        out = ops.resume_feature_order(args.fo_id, fix=args.fix,
                                       project_name=args.project)
        _print({**out, "note": (
            f"filed {out['fix_wo_id']} — jarvisd dispatches it shortly"
            if out["fix_wo_id"] else
            "no work filed; the feature settles on what its children already say"
        )}, args.json)

    return 0


#: `jarvis io list`/`show` reuse the feature icons (docs/superpowers/specs/2026-09-23-improvement-orders.md)
def cmd_io(args: argparse.Namespace) -> int:
    from . import findings, ops

    if args.io_cmd == "create":
        io = ops.create_improvement_order(args.project, args.title,
                                          description=args.description,
                                          refs=args.refs, origin=args.origin,
                                          budget_usd=_budget_arg(args))
        _print({"created": io["id"], "project": args.project, "status": io["status"],
                **({"budget_usd": io["budget_usd"]} if io.get("budget_usd") else {}),
                "note": "nothing is filed from an improvement order until you decide "
                        "on its findings"}, args.json)

    elif args.io_cmd == "list":
        rows = ops.list_improvement_orders(args.project, include_settled=args.all)
        if args.json:
            _print(rows, True)
        elif not rows:
            print("no improvement orders")
        else:
            for io in rows:
                icon = FO_ICON.get(io["status"], "•")
                att = " ⚠" if io["needs_attention"] else ""
                print(f"{icon} {io['id']} [{io['project']}] {io['title']} "
                      f"({io['status_label']}, {_age(io['created_at'])}){att}")

    elif args.io_cmd == "show":
        detail = ops.show_improvement_order(args.io_id, args.project)
        detail["budget"] = ops.feature_order_budget(args.io_id, detail["project"])
        if args.json:
            _print(detail, True)
        else:
            # COUNTS FIRST — §2.6
            d = detail["by_decision"]
            print(f"{FO_ICON.get(detail['status'], '•')} {detail['id']} "
                  f"[{detail['project']}] {detail['title']} ({detail['status_label']})")
            neo = detail.get("with_neo") or 0
            print(f"\n{detail['findings']} findings: {d['accepted']} accepted, "
                  f"{d['rejected']} rejected, {d['pending'] - neo} awaiting you"
                  + (f", {neo} with Neo" if neo else ""))
            observation = detail["observation"]
            if not (args.full or args.finding):
                # The brief is the filer's; the findings are what the reader acts on
                observation = findings.clip(observation, 300)
            print(f"\n{observation}\n")
            if detail["attention_reason"]:
                print(f"⚠ {detail['attention_reason']}\n")
            if detail["evidence_refs"]:
                print("evidence:")
                for ref in detail["evidence_refs"]:
                    print(f"  {ref}")
            if detail["budget"]["budget_usd"]:
                b = detail["budget"]
                print(f"\nbudget: ${b['spent_usd']:.2f} of ${b['budget_usd']:.2f}")
            if detail["analyst"]:
                a = detail["analyst"]
                print(f"\nanalyst: {a['id']} ({a['status']})")
            if detail["alarms"]:
                print(f"\nalarms: {ops.alarm_standing_line(detail['alarms'])}")
            report = detail["report"]
            if args.finding:
                keep = [f for f in report.get("findings") or []
                        if f.get("key") == args.finding]
                if not keep:
                    raise ops.OpsError(
                        f"no finding {args.finding!r} on {detail['id']}; it has: "
                        f"{', '.join(f.get('key', '') for f in report.get('findings') or [])}")
                report = {"findings": keep}
            if report.get("findings"):
                # §4.4's renderer, with each accepted finding's filed orders and their LIVE status resolved once by...
                print()
                render = (findings.render_report if args.full or args.finding
                          else findings.render_digest)
                for line in render(report, detail["filed_orders"]):
                    print(line)
                if not (args.full or args.finding):
                    print(f"\none finding in full: jarvis io show {detail['id']} "
                          f"--finding <key>   (all: --full)")

    elif args.io_cmd == "review":
        _print(ops.review_findings(args.io_id, accept=args.accept,
                                   reject={k: args.feedback for k in args.reject},
                                   feedback=args.feedback,
                                   accept_all=args.accept_all,
                                   project_name=args.project), args.json)

    elif args.io_cmd == "ask-neo":
        _print(ops.ask_neo_about_findings(args.io_id, args.project), args.json)

    elif args.io_cmd == "report":
        path = Path(args.from_file)
        if not path.is_file():
            raise ops.OpsError(f"no such report file: {path}")
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ops.OpsError(f"{path} is not valid JSON: {e}") from e
        _print(ops.submit_findings(args.io_id, doc, project_name=args.project),
               args.json)

    elif args.io_cmd == "cancel":
        _print(ops.cancel_improvement_order(args.io_id, args.project), args.json)

    elif args.io_cmd == "budget":
        # The family here is the order plus its analyst
        if args.amount is None and not args.clear:
            _print(ops.improvement_order_budget(args.io_id, args.project), args.json)
        else:
            amount = None if args.clear else _parse_budget_amount(args.amount)
            _print(ops.set_improvement_budget(args.io_id, amount, args.project),
                   args.json)

    return 0


MORPHEUS_ICON = {"queued": "…", "monitoring": "◉", "validated": "✓", "failed": "✗",
                 "timedout": "⌛"}


def _morpheus_line(item: dict) -> str:
    action = f" → {item['action']}" if item.get("action") else ""
    return (f"{MORPHEUS_ICON.get(item['state'], '•')} {item['id']} "
            f"[{item.get('project') or '?'}] PR #{item['pr_number']} "
            f"{item['state']}{action} — {item['pr_title'][:70]}")


def _fix_lines(fix: dict | None, dispatch: str) -> list[str]:
    """The fix order on a filed GAP and how far it got (#1344)"""
    lines = [dispatch] if dispatch else []
    if fix:
        lines.append(f"fix {fix['wo_id']} [{fix['status']}]"
                     + (f" {fix['pr_url']}" if fix["pr_url"] else "")
                     + (f" (carried by {fix['carrier']})"
                        if fix["carrier"] != fix["wo_id"] else ""))
        landed = fix["production"]
        lines.append(f"in production: PR #{landed['pr_number']}"
                     + (f" in {landed['release_tag']}" if landed["release_tag"] else "")
                     + f", its own validation {landed['item']} {landed['state']}"
                     if landed else "not in production yet")
    return lines


def _chain_lines(chain: dict, inv_id: str) -> list[str]:
    """validation -> investigation -> issue -> fix -> production -> re-validation (#1344)"""
    v = chain["validation"]
    lines = []
    if v:
        lines.append(f"validation {v['id']} (PR #{v['pr_number']})")
    lines.append(f"investigation {inv_id}"
                 + (f" (cycle {chain['cycle']} of {chain['max_cycles']})"
                    if chain["cycle"] else ""))
    if chain["issue_url"]:
        lines.append(f"issue {chain['issue_url']}")
    lines += _fix_lines(chain["fix"], chain["dispatch"])
    if v:
        lines.append(f"re-validation: {v['id']} is {v['state']}"
                     + (f" — {v['resolution']}" if v["resolution"] else ""))
    for nxt in chain["next_investigations"]:
        lines.append(f"failed again -> {nxt}")
    return lines


def cmd_morpheus(args: argparse.Namespace) -> int:
    """`jarvis morpheus …` — a thin wrapper over `ops.morpheus_*`"""
    from . import ops

    if args.morpheus_cmd == "status":
        rep = ops.morpheus_report()
        if not args.all:
            rep["items"] = [i for i in rep["items"]
                            if i["state"] in ("queued", "monitoring") or
                            (i["state"] in ("failed", "timedout"))]
        if args.json:
            _print(rep, True)
            return 0
        c = rep["counts"]
        sess = rep["session"]
        print(" · ".join(f"{n} {s}" for s, n in c.items()) +
              (f" · last release {rep['last_release']}" if rep["last_release"] else ""))
        share = sess.get("context_share")
        print(f"session: {sess.get('session_id') or 'not started'}"
              f" · {sess.get('turns') or 0} turn(s)"
              f" · {sess.get('compactions') or 0} compaction(s)"
              + (f" · context {share:.0%}" if share is not None else "")
              + f" · {_morpheus_pause_text(rep['pause'])}"
              + f" · {_morpheus_activity_text(rep['activity'])}"
              + (f" · last error: {sess['last_error']}" if sess.get("last_error") else ""))
        for item in rep["items"]:
            print(_morpheus_line(item))
        return 0
    if args.morpheus_cmd == "show":
        detail = ops.morpheus_detail(args.item_id)
        item = detail["item"]
        if args.json:
            _print(item, True)
            return 0
        print(_morpheus_line(item))
        print(f"{item['pr_url']}  first release with this PR {item['release_tag'] or '—'}"
              f"  work order {item['wo_id'] or '—'}")
        if item["last_check_ts"]:
            print(f"last check {_stamp(item['last_check_ts'])}"
                  f" on {item.get('last_check_version') or 'an unknown version'}")
        print(f"checks {item['checks']} · last exit {item['last_exit']}"
              f" · deadline {_stamp(item['deadline_ts'])}"
              + (f" · {item['auto_requeues']} automatic requeue(s)"
                 if item.get("auto_requeues") else ""))
        if item["resolution"]:
            print(f"\n{item['resolution']}")
        print(f"\nhints:\n{item['hints'] or '(none)'}")
        print(f"\nscript ({item['script_source'] or 'none yet'}):\n{item['script'] or '—'}")
        if item["last_output"]:
            print(f"\nlast output:\n{item['last_output']}")
        print("\nruns:")
        for r in detail["runs"][:10]:
            print(f"  {_stamp(r['ts'])}  {r['kind']:<5}  exit {r['exit']}  {r['state_after']}")
        if not detail["runs"]:
            print("  (none recorded" + (" yet — history starts with this release)"
                                        if item["checks"] else ")"))
        if detail["questions"]:
            print("\nquestions:")
            for q in detail["questions"]:
                print(f"  Q{q['id']} [{q['status']}] {q['question']}"
                      + (f" — {q['answer']}" if q["answer"] else ""))
        if detail["investigations"]:
            print("\ninvestigations (oldest first):")
            for inv in detail["investigations"]:
                print(f"  {inv['id']} [{inv['status']}] {inv['classification'] or 'no verdict'}"
                      + (f" -> {inv['issue_url']}" if inv["issue_url"] else ""))
                for line in _fix_lines(inv["fix"], inv["dispatch"]):
                    print(f"    {line}")
        return 0
    if args.morpheus_cmd == "enqueue-release":
        _print(ops.morpheus_enqueue_release(args.tag, args.from_ref, args.to_ref,
                                            args.repo, args.project), args.json)
        return 0
    if args.morpheus_cmd == "add":
        item = ops.morpheus_add(args.pr, args.repo, args.release, args.project)
        _print(item if args.json else {"queued": item["id"], "state": item["state"]},
               args.json)
        return 0
    if args.morpheus_cmd == "script":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
        item = ops.morpheus_set_script(args.item_id, text, args.source)
        _print({"id": item["id"], "state": item["state"],
                "note": "the daemon runs it every check_every_minutes from production"},
               args.json)
        return 0
    if args.morpheus_cmd == "check":
        item = ops.morpheus_check(args.item_id)
        _print(item if args.json else {
            "id": item["id"], "state": item["state"],
            "exit": item.get("probe_exit", item["last_exit"]),
            "output": item.get("probe_output", item["last_output"])}, args.json)
        return 0
    if args.morpheus_cmd == "waive":
        item = ops.morpheus_waive(args.item_id, args.reason)
        _print({"id": item["id"], "state": item["state"]}, args.json)
        return 0
    if args.morpheus_cmd == "requeue":
        item = ops.morpheus_requeue(args.item_id)
        _print({"id": item["id"], "state": item["state"]}, args.json)
        return 0
    if args.morpheus_cmd == "rewrite":
        item = ops.morpheus_rewrite(args.item_id, args.why)
        _print({"id": item["id"], "state": item["state"]}, args.json)
        return 0
    if args.morpheus_cmd == "confirm":
        item = ops.morpheus_confirm(args.item_id, args.fix)
        _print({"id": item["id"], "state": item["state"], "fix_order": item["fix_order"]},
               args.json)
        return 0
    if args.morpheus_cmd in ("pause", "unpause", "follow-fleet"):
        mode = {"pause": "paused", "unpause": "running"}.get(args.morpheus_cmd)
        state = ops.morpheus_pause(mode, getattr(args, "reason", ""))
        if args.json:
            _print(state, True)
        else:
            print(_morpheus_pause_text(state))
        return 0
    return 1


def _morpheus_pause_text(pause: dict) -> str:
    fleet = "fleet paused" if pause["fleet_paused"] else "fleet running"
    if pause["override"] == "paused":
        return f"PAUSED by `morpheus pause` ({fleet}): {pause['reason']}"
    if pause["override"] == "running":
        return f"RUNNING by `morpheus unpause` ({fleet}): {pause['reason']}"
    return ("PAUSED with the fleet (no session turns)" if pause["paused"]
            else "unpaused (follows the fleet)")


def _morpheus_activity_text(act: dict) -> str:
    """The pause mode says what is ALLOWED; this says what the session is doing (issue #1162)"""
    if act["turn_in_flight"]:
        return (f"{act['turn_kind'] or 'turn'} in flight since "
                f"{_stamp(act['turn_started_ts'])}")
    if act.get("abandoned_turn_ts"):
        parts = [f"idle (the {act['turn_kind'] or 'turn'} started "
                 f"{_stamp(act['abandoned_turn_ts'])} never finished)"]
    else:
        parts = [f"idle · {act['due']} queued due now" if act["due"] else "idle"]
    if act.get("usage_hold_until"):
        parts.append(f"held until {_stamp(act['usage_hold_until'])} (usage limit)")
    if act.get("kill_hold_until"):
        parts.append(f"held until {_stamp(act['kill_hold_until'])} or the next release "
                     f"({act['kills']} killed turns in a row)")
    for h in act["held"]:
        why = {"error": " after a failed turn", "offer": " after an offer"}.get(h["cause"], "")
        parts.append(f"{h['id']} offered {h['offers']} of {act['max_offers']}: "
                     + ("no offer left, it will time out"
                        if h["exhausted"]
                        else f"held back{why} until {_stamp(h['next_offer_ts'])}"))
    return " · ".join(parts)


def cmd_investigate(args: argparse.Namespace) -> int:
    """`jarvis investigate …` — A THIN WRAPPER HOLDING NO LOGIC (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    from . import ops, verdicts

    if args.inv_cmd == "create":
        inv = ops.create_investigation_order(args.project, args.subject, args.why,
                                             budget_usd=_budget_arg(args),
                                             origin=args.origin)
        _print({"created": inv["id"], "subject": args.subject,
                "status": inv["status"],
                **({"budget_usd": inv["budget_usd"]} if inv.get("budget_usd") else {}),
                "note": "it reads the record and settles itself — a GAP files an "
                        "expedited bug after a duplicate check"}, args.json)

    elif args.inv_cmd == "list":
        rows = ops.list_investigation_orders(args.project, include_settled=args.all)
        if args.json:
            _print(rows, True)
        elif not rows:
            print("no investigations")
        else:
            for row in rows:
                icon = FO_ICON.get(row["status"], "•")
                att = " ⚠" if row["needs_attention"] else ""
                print(f"{icon} {row['id']} [{row['project']}] {row['title']} "
                      f"({row['status_label']}, {_age(row['created_at'])}){att}")

    elif args.inv_cmd == "show":
        detail = ops.show_investigation_order(args.inv_id, args.project)
        detail["budget"] = ops.feature_order_budget(args.inv_id, detail["project"])
        if args.json:
            _print(detail, True)
        else:
            # THE CLASSIFICATION AND THE SUBJECT FIRST (§2.9), then the argument
            print(f"{FO_ICON.get(detail['status'], '•')} {detail['id']} "
                  f"[{detail['project']}] {detail['status_label']}")
            print(f"\n{detail['classification'] or 'no verdict yet'} — "
                  f"{detail['subject']}")
            print(f"\n{detail['why']}\n")
            if detail["attention_reason"]:
                print(f"⚠ {detail['attention_reason']}\n")
            if detail["budget"]["budget_usd"]:
                b = detail["budget"]
                print(f"budget: ${b['spent_usd']:.2f} of ${b['budget_usd']:.2f}")
            if detail["investigator"]:
                i = detail["investigator"]
                print(f"investigator: {i['id']} ({i['status']})")
            if detail["alarms"]:
                print(f"alarms: {ops.alarm_standing_line(detail['alarms'])}")
            if detail["verdict"]:
                print()
                for line in verdicts.render_verdict(detail["verdict"]):
                    print(line)
            chain = detail["chain"]
            if chain["validation"] or chain["fix"]:
                print("\nchain:")
                for line in _chain_lines(chain, detail["id"]):
                    print(f"  {line}")
            if detail["assumptions"]:
                print("\nthe investigator's calls (on record, for nobody's review):")
                for a in detail["assumptions"]:
                    print(f"  {a['n']}. {a['content']}")

    elif args.inv_cmd == "cancel":
        _print(ops.cancel_investigation_order(args.inv_id, args.project), args.json)

    elif args.inv_cmd == "budget":
        # THE SECOND STEP OF THE TOP-UP, and the command a parked child's note names
        if args.amount is None and not args.clear:
            _print(ops.investigation_order_budget(args.inv_id, args.project), args.json)
        else:
            amount = None if args.clear else _parse_budget_amount(args.amount)
            _print(ops.set_investigation_budget(args.inv_id, amount, args.project),
                   args.json)

    elif args.inv_cmd == "verdict":
        path = Path(args.from_file)
        if not path.is_file():
            raise ops.OpsError(f"no such verdict file: {path}")
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise ops.OpsError(f"{path} is not valid JSON: {e}") from e
        _print(ops.submit_verdict(args.inv_id, doc, project_name=args.project),
               args.json)

    return 0


GATE_ICON = {"awaiting_case": "✎", "pending": "⏸", "approved": "✅",
             "denied": "⛔", "dismissed": "⊘",
             "expired": "⌛",
             # Keyed on the DISPLAY state, not the status: a spent grant is `expired` in the database and an...
             "spent": "☑"}


def _gate_display(row: dict) -> str:
    """The word this gate is rendered under"""
    if row["status"] == "expired" and row["closed_as"] in ("spent", "abandoned"):
        return row["closed_as"]
    return row["status"]


_ROLE_ICON = {"match": "✋", "exempt": "✓", "canary": "🔒"}


def _print_gate_rules(data: dict) -> None:
    """The rule base, grouped by role"""
    rules = data["rules"]
    if not rules:
        print("no gate rules")
        _print_classifier_stats(data.get("classifier") or {})
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
    _print_classifier_stats(data.get("classifier") or {})


def _print_classifier_stats(stats: dict) -> None:
    """How wrong the recogniser has been, and how often nobody stayed to say so"""
    if not stats or not stats.get("requests"):
        return
    print(f"\nthe recogniser's own record, over {stats['requests']} request(s): "
          f"{stats['dismissed']} dismissed as false positives, "
          f"{stats['abandoned']} abandoned unargued")
    if stats["abandoned"]:
        print("    an abandoned request was never reviewed by anyone — on the evidence "
              "so far these are mostly false positives a worker routed around, so the "
              "dismissed count understates the real error rate by roughly this much.")


def cmd_gate(args: argparse.Namespace) -> int:
    from . import gates, ops

    # A worker exits its OWN block; `JARVIS_WO_ID` is what says which — spec §8
    caller = os.environ.get("JARVIS_WO_ID") or None

    if args.ga_cmd == "request":
        wo_id, command = ops.resolve_gate_target(args.wo_id, args.command, args.project,
                                                 caller)
        _print(ops.request_gate_approval(
            wo_id, command, why=args.why, evidence=args.evidence,
            project_name=args.project), args.json)
    elif args.ga_cmd == "contest":
        # `require_caller` — see `ops.CONTEST_NEEDS_AN_OWNER`
        wo_id, command = ops.resolve_gate_target(args.wo_id, args.command, args.project,
                                                 caller, require_caller=True)
        _print(ops.contest_gate_match(wo_id, command, why=args.why,
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
                icon = GATE_ICON.get(_gate_display(r), "•")
                where = "you" if r["escalated"] else "neo"
                if r["status"] == "awaiting_case":
                    # Never "pending": nobody is holding it
                    state = "awaiting the worker's case — no reviewer sees it yet"
                elif r["status"] == "pending":
                    state = (f"contested, pending (with {where})" if r["contested"]
                             else f"pending (with {where})")
                elif r["status"] == "dismissed":
                    state = f"dismissed by {r['decided_by'] or '?'} — not a gated action"
                elif r["closed_as"] == "abandoned":
                    # Not a verdict, and it must not print as one: nobody reviewed it
                    state = "abandoned — no case was ever made, nothing was decided"
                elif r["closed_as"] == "spent":
                    # The opposite of a lapse: this grant was used
                    state = (f"spent — approved by {r['decided_by'] or '?'}, then used "
                             f"{r['uses']}/{r['max_uses']}")
                else:
                    state = f"{r['status']} by {r['decided_by'] or '?'}"
                print(f"{icon} {r['id']} [{r['project']}] {r['kind']} · {state} "
                      f"· {r['wo_id']} · {_age(r['ts'])} ago")
                print(f"    {r['command']}")
                if r["status"] == "awaiting_case":
                    # By request number, as the block told the worker — spec §8
                    ask = gates.request_command(r["wo_id"], r["command"], r["kind"],
                                                why="...", evidence="...",
                                                approval_id=r["id"])
                    contest = gates.contest_command(r["wo_id"], r["command"], why="...",
                                                    approval_id=r["id"])
                    print(f"    ↳ {ask}   (the worker's move)")
                    print(f"    ↳ {contest}"
                          f"   (…or this, if the gate matched it by mistake)")
                elif r["status"] == "pending" and r["escalated"]:
                    print(f"    ↳ Neo escalated: {r['escalation_reason']}")
                    print(f"    ↳ jarvis gate approve {r['id']} --reason \"...\"  |  "
                          f"jarvis gate deny {r['id']} --reason \"...\"  |  "
                          f"jarvis gate dismiss {r['id']} --reason \"...\"")
                elif r["decision_reason"]:
                    print(f"    ↳ {r['decision_reason']}")
            # The classifier's own error rate, kept in front of whoever reads this list
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
                    f"could NOT be generalised — {data['why_unlearnable']}"))
    elif args.ga_cmd in ("approve", "deny", "dismiss"):
        verdict = {"approve": "approved", "deny": "denied",
                   "dismiss": "dismissed"}[args.ga_cmd]
        _print(ops.decide_gate(args.approval_id, verdict=verdict,
                               reason=args.reason, project_name=args.project), args.json)
    return 0


def _print_rules_list(data: dict) -> None:
    """The registry, counts first"""
    c = data["counts"]
    noun = "rule" if c["total"] == 1 else "rules"
    print(f"{c['total']} {noun}: {c['armed']} armed, {c['dry_run']} in dry run, "
          f"{c['retracted']} retracted")
    # SAID ON ITS OWN LINE, above the rules rather than in the closing note
    if data.get("enabled") is False:
        print("  the evaluation pass is OFF — nothing evaluates these rules")
    for r in data["rules"]:
        scope = r["project"] or "fleet-wide"
        retired = " ⊘ retracted" if r["retired_at"] else ""
        print(f"  {r['id']} [{r['gap_class']}] {scope} · {r['status']}{retired}")
        if r["summary"]:
            print(f"      {r['summary']}")
        if not r["condition_prose"]:
            print("      UNREADABLE: " + "; ".join(r["condition_problems"]))
        for rem in r["remedies"]:
            print(f"      → {rem['primitive']} {rem['params'] or ''}".rstrip())
        if r["hit_rate"] is None:
            print(f"      {r['hit_rate_note']}")
        else:
            hits, total = r["fires"]["hits"], r["fires"]["total"]
            print(f"      {hits} {'hit' if hits == 1 else 'hits'} of "
                  f"{total} {'fire' if total == 1 else 'fires'}")
        print(f"      cost: {r['cost']['note']}")
    print(f"\n{data['note']}")
    print(data["cost_unattributed"])


def _print_rules_show(data: dict) -> None:
    d = data["detector"]
    print(f"{d['id']} [{d['gap_class']}] {d['status']} · "
          f"{d['project'] or 'fleet-wide'}")
    if d["summary"]:
        print(f"  {d['summary']}")
    print(f"  condition: {data['condition_prose'] or 'UNREADABLE'}")
    for p in data["condition_problems"]:
        print(f"    ⚠ {p}")
    for rem in data["remedies"] + data["retired_remedies"]:
        retired = " ⊘ retracted" if rem["retired_at"] else ""
        print(f"  remedy {rem['id']}: {rem['primitive']}{retired}")
        if rem["params"]:
            print(f"    parameters: {json.dumps(rem['params'], sort_keys=True)}")
        if rem["argument"]:
            print(f"    argument: {rem['argument']}")
        if rem["retired_at"]:
            print(f"    ↳ {rem['retired_reason']}")
    prov = data["provenance"]
    print("  provenance: " + ", ".join(f"{k}={v}" for k, v in prov.items() if v))
    if data["hit_rate"] is None:
        print(f"  {data['hit_rate_note']}")
    cost = data["cost"]
    print(f"  cost: {cost['note']}")
    for rm, usd in sorted(cost["by_remedy"].items()):
        print(f"    {rm or 'no remedy rule'}: ${usd:,.2f}")
    print(f"    {data['cost_unattributed']}")
    for f in data["fires"]:
        print(f"  fire {f['id']} {f['outcome']} · {f['order_id']} · {f['mode']}"
              # A fire is one line: the stored detail stays at rules.FACTS_CHARS
              + (f" · {_one_line(f['detail'], 110)}" if f["detail"] else ""))
    for rec in data["recurrences"]:
        # The verdict, the order it recurred on, and what happened on the tracker
        print(f"  recurrence {rec['id']} {rec['verdict']} · {rec['order_id']}"
              + (f" · {_one_line(rec['filed_note'], 110)}" if rec["filed_note"] else ""))


def _print_rules_arm(data: dict) -> None:
    d = data["detector"]
    print(f"✓ {d['id']} [{d['gap_class']}] is ARMED by {d['armed_by']}")
    print(f"  {data['note']}")


def _print_rules_false_positive(data: dict) -> None:
    d, f = data["detector"], data["fire"]
    print(f"✓ fire {f['id']} on {f['order_id']} marked a false positive "
          f"({d['false_positives']} on {d['id']})")
    if data["demoted"]:
        print(f"  ⚠ {d['id']} is back in dry run — {data['note']}")
    else:
        print(f"  {data['note']}")


def _print_rules_dry_run(data: dict) -> None:
    print(f"{data['detector']['id']} [{data['detector']['gap_class']}]")
    if not data["readable"]:
        print("  UNREADABLE — nothing was evaluated and nothing was decided")
        for p in data["problems"]:
            print(f"    ⚠ {p}")
        print(f"\n{data['note']}")
        return
    print(f"  condition: {data['condition_prose']}")
    print(f"  reads fields: {', '.join(data['fields'])}")
    print(f"  from: {', '.join(data['sources'])}")
    if data["evaluated"]:
        print(f"  {data['explanation']}")
    print(f"\n{data['note']}")


def _print_rules_summary(data: dict) -> None:
    """The evolution report: what the OS learned, and whether it is compounding (§9)"""
    window, share = data["window"], data["mechanical_share"]
    scope = data["project"] or "fleet-wide"

    # THE HEADLINE FIRST, `_print_rules_list`'s ordering rule: the mechanical share is the line a reader...
    if share["value"] is None:
        print(share["note"])
    else:
        print(f"{'%.0f' % (share['value'] * 100)}% of stuck orders in this window "
              f"resolved mechanically — {share['numerator']} of {share['denominator']}")
    print(f"  {scope} · last {window['days']} days · weeks in {window['week_zone']}")
    if window["week_zone_note"]:
        print(f"  ⚠ {window['week_zone_note']}")
    for name in share["unreadable_projects"]:
        print(f"  ⚠ {name} could not be read, so it is in no number above")

    # One point per week WITH DATA (§9): a week absent from the series is a week nobody measured
    if share["series"]:
        print("\nweek by week")
        for point in share["series"]:
            counts = f"{point['numerator']} of {point['denominator']}"
            if point["value"] is None:
                print(f"  {point['week']}  {counts}")
            else:
                print(f"  {point['week']}  {'%.0f' % (point['value'] * 100)}% ({counts})")

    if data["by_gap_class"]:
        print("\nfires by gap class")
        for row in data["by_gap_class"]:
            total = row["total"]
            print(f"  {row['gap_class']}: {total} {'fire' if total == 1 else 'fires'}")
            for week in row["weeks"]:
                print(f"      {week['week']}  {week['count']}")

    # Does ARMING a rule shorten how long the gap stands?
    if data["stuck_resolution"]:
        print(f"\nhow long gaps stand (median, {data['min_samples']} samples minimum)")
        for row in data["stuck_resolution"]:
            print(f"  {row['gap_class']}")
            if row["note"]:
                print(f"      {row['note']}")
                continue
            # The sign is the dict's: negative delta is the improvement
            delta = row["delta"]
            sign = "-" if delta < 0 else "+"
            print(f"      dry run {_dur(row['dry_run_median'])} "
                  f"· armed {_dur(row['armed_median'])} "
                  f"· {sign}{_dur(abs(delta))}")

    if data["per_rule"]:
        print("\nrule by rule")
        for rule_row in data["per_rule"]:
            print(f"  {rule_row['id']} [{rule_row['gap_class']}] {rule_row['status']}")
            if rule_row["summary"]:
                print(f"      {_one_line(rule_row['summary'], 110)}")
            if not rule_row["readable"]:
                print("      UNREADABLE: " + "; ".join(rule_row["condition_problems"]))
            if rule_row["hit_rate"] is None:
                print(f"      {rule_row['hit_rate_note']}")
            else:
                print(f"      {'%.0f' % (rule_row['hit_rate'] * 100)}% hit rate "
                      f"· {rule_row['hits']} hits "
                      f"· {rule_row['false_positives']} false positives "
                      f"· {rule_row['recurrences']} recurrences")
            if rule_row["median_cleared_seconds"] is None:
                print(f"      {rule_row['median_cleared_seconds_note']}")
            else:
                print("      median time to clear "
                      f"{_dur(rule_row['median_cleared_seconds'])}")
            if rule_row["last_fired"]:
                print(f"      last fired {_stamp(rule_row['last_fired'])}"
                      + (f" · last cleared {_stamp(rule_row['last_cleared'])}"
                         if rule_row["last_cleared"] else ""))
            print(f"      cost: {rule_row['cost']['note']}")
        print(f"  {data['cost_unattributed']}")

    # The WEAKER HALF is printed as `ops`' own sentence and in no other form
    rec = data["recurrences"]
    half = rec["weaker_half"]
    print(f"\n{half['note']}")
    # §9, ABSENT IS NEVER ZERO: with no recurrence row the sentence is the whole section
    if rec["rows"]:
        counts = ", ".join(f"{n} {verdict}"
                           for verdict, n in rec["verdict_counts"].items())
        if counts:
            print(f"  {counts}")
        # The SAME facts in the SAME order as `evolution.html`: the note
        if half["excluded_note"]:
            excluded = ", ".join(f"{n} {verdict}"
                                 for verdict, n in half["excluded"].items())
            print(f"  {half['excluded_note']}: {excluded}" if excluded
                  else f"  {half['excluded_note']}")

    # LAST: the longest block and the least actionable one
    if data["timeline"]:
        print("\nwhat happened, newest first")
        for event in data["timeline"]:
            print(f"  {_stamp(event['ts'])} {event['kind']} {event['id']}")
            print(f"      {_one_line(event['headline'], 110)}")
            # A MISSING link is absent from the dict (§9)
            links = " · ".join(f"{k}: {v}" for k, v in event["links"].items())
            if links:
                print(f"      {links}")

    for kind, names in data["unreadable"].items():
        if names:
            print(f"\nunreadable {kind}: {', '.join(str(n) for n in names)}")


def cmd_rules(args) -> int:
    from . import ops

    if args.rules_cmd == "list":
        data = ops.rules_list(project=args.project, status=args.status or "",
                              gap_class=args.gap_class, days=args.days)
        _print(data, True) if args.json else _print_rules_list(data)
    elif args.rules_cmd == "show":
        data = ops.rules_show(args.detector_id, days=args.days)
        _print(data, True) if args.json else _print_rules_show(data)
    elif args.rules_cmd == "retract":
        data = ops.rules_retract(args.rule_id, args.reason)
        if args.json:
            _print(data, True)
        else:
            print(f"✓ {data['rule']['id']} retracted — {data['note']}")
    elif args.rules_cmd == "arm":
        data = ops.rules_arm(args.detector_id, args.reason)
        _print(data, True) if args.json else _print_rules_arm(data)
    elif args.rules_cmd == "false-positive":
        data = ops.rules_false_positive(args.fire_id, args.reason)
        _print(data, True) if args.json else _print_rules_false_positive(data)
    elif args.rules_cmd == "dry-run":
        data = ops.rules_dry_run(args.detector_id, args.order_id)
        _print(data, True) if args.json else _print_rules_dry_run(data)
    elif args.rules_cmd == "summary":
        data = ops.evolution_report(args.project or None, days=args.days)
        _print(data, True) if args.json else _print_rules_summary(data)
    return 0


def _cfg_value(value: Any) -> str:
    """A setting as JSON, so `"true"` never reads as `true`"""
    return json.dumps(value, ensure_ascii=False)


def _cfg_change(change: dict) -> str:
    from . import ops

    # No arrow for a document-only write: there is nothing on either side of it
    if change["kind"] in ops.DOCUMENT_ONLY_KINDS:
        where = ("the catalog now says so" if change["kind"] == "pinned"
                 else "back on its default")
        return (f"= {change['path']} = {_cfg_value(change['new'])} "
                f"(unchanged — {where})")
    sign = {"added": "+", "removed": "-", "changed": "~"}[change["kind"]]
    if change["kind"] == "added":
        return f"{sign} {change['path']} = {_cfg_value(change['new'])}"
    if change["kind"] == "removed":
        return f"{sign} {change['path']} (was {_cfg_value(change['old'])})"
    return (f"{sign} {change['path']}: {_cfg_value(change['old'])} → "
            f"{_cfg_value(change['new'])}")


def _print_config_write(data: dict) -> None:
    from . import ops

    change = data["change"]
    print(f"✓ {_cfg_change(change)}")
    if data["safety"] and change["kind"] not in ops.DOCUMENT_ONLY_KINDS:
        print("  ⚠ SAFETY SETTING — this changes what a worker is allowed to do")
    print(f"  {data['apply']} — {data['note']}")
    print(f"  {data['version']['id']}"
          + ("" if data.get("changed", True) else "  (the document was already this — "
                                                  "no new version)"))


def _print_config_versions(rows: list[dict]) -> None:
    for row in rows:
        head = "●" if row.get("head") else " "
        reason = f" · {row['reason']}" if row["reason"] else ""
        print(f"{head} {row['id']}  {row['actor']} · {_age(row['ts'])} ago{reason}")
        # Changed and removed first: truncation hid them behind a release's defaults (1235)
        shown = sorted(row["changes"], key=lambda c: c.get("kind") == "added")
        for change in shown[:4]:
            print(f"      {_cfg_change(change)}")
        if len(row["changes"]) > 4:
            print(f"      … and {len(row['changes']) - 4} more")


WIRING_KIND_TITLES = {"mcp": "MCP servers", "skill": "Skills", "plugin": "Plugins"}


def _print_wiring(data: dict[str, Any]) -> None:
    """`jarvis config wiring` — what the user has, and what this scope wires of it"""
    scope = data["project"] or "the fleet (os.wiring — every project inherits it)"
    print(f"wiring for {scope}")
    for err in data["errors"]:
        print(f"  ⚠ {err}")
    for kind, title in WIRING_KIND_TITLES.items():
        items = [i for i in data["items"] if i.kind == kind]
        if not items:
            continue
        print(f"\n{title}")
        for item in items:
            mark = "●" if item.wired else "○"
            state = "wired" if item.wired else "NOT wired"
            print(f"  {mark} {item.name}  ({state}; {item.source})")
            note = item.note if not item.lever else ""
            for line in (item.detail, note):
                if line:
                    print(f"      {line}")
    print("\nnothing here is written to your Claude configuration — this is which of it "
          "\nJarvis wires into a worker at dispatch. Change it on /config.")


def _cmd_onboard_read(args: argparse.Namespace) -> int:
    """`jarvis onboard questions | explain` — §4 of the onboarding-interview spec"""
    from . import ops

    if args.ob_cmd == "questions":
        data = ops.onboard_questions(args.area)
        if args.json:
            _print(data, True)
            return 0
        for area in data["areas"]:
            star = "*" if area["must_have"] else " "
            print(f"{star} {area['area']:<14} {area['title']}")
            if area["postures"]:
                print(f"    postures: {', '.join(area['postures'])}")
            print(f"    asked: {', '.join(k['tail'] for k in area['knobs']['asked']) or '—'}")
        return 0

    data = ops.onboard_explain(args.target, project=args.project, catalog_path=args.catalog)
    if args.json:
        _print(data, True)
        return 0
    if data["kind"] == "area":
        print(f"{data['title']}  ({data['area']}, scope {data['scope']})")
        print(f"\n{data['summary']}\n\nOn: {data['on_costs']}\nOff: {data['off_costs']}")
        if data["enable"]:
            print(f"\n{data['enable']['path']} = {_cfg_value(data['enable']['value'])}")
        for name, rows in data["postures"].items():
            print(f"\n{name}:")
            for row in rows:
                print(f"  {row['path']} = {_cfg_value(row['value'])} — {row['line']}")
        for ref in data["grounding"]:
            print(f"\n— {ref['path']} § {ref['section']}\n{ref['excerpt'] or '(unreadable)'}")
        return 0
    print(f"{data['path']} = {_cfg_value(data['current'])}   "
          f"(default {_cfg_value(data['default'])}; area {data['area']})")
    if data["line"]:
        print(f"  {data['line']}")
    if data["example"]:
        print(f"  e.g. {data['example']}")
    print(f"  {data['apply']} — {data['apply_note']}"
          + ("  [safety: needs --reason]" if data["safety"] else ""))
    print(f"  change it later: {data['command']}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    from . import ops

    if args.cfg_cmd == "show":
        data = ops.config_show(project=args.project, version=args.version,
                               catalog_path=args.catalog)
        if args.json:
            _print(data, True)
            return 0
        version = data["version"]
        if data["source"] == "version":
            print(f"{version['id']}  {version['actor']} · {_age(version['ts'])} ago"
                  + (f" · {version['reason']}" if version["reason"] else ""))
        else:
            print(f"catalog: {data['catalog']}")
            if data["drift"]:
                print(f"⚠ the file is {data['file_version']}, which is NOT the recorded "
                      f"head — `jarvis config adopt` to record it, or "
                      f"`jarvis config restore <id>` to put the record's version back")
            else:
                print(f"version: {version['id']}  {version['actor']} · "
                      f"{_age(version['ts'])} ago")
        for path, value in sorted(data["resolved"].items()):
            print(f"  {path} = {_cfg_value(value)}")
    elif args.cfg_cmd == "get":
        data = ops.config_get(args.path, project=args.project,
                              catalog_path=args.catalog)
        if args.json:
            _print(data, True)
        else:
            source = "set in the catalog" if data["written"] else "the shipped default"
            print(f"{data['path']} = {_cfg_value(data['value'])}   ({source})")
            print(f"  {data['apply']} — {data['note']}")
    elif args.cfg_cmd == "set":
        data = ops.set_config(args.path, ops.parse_config_value(args.value),
                              project=args.project, reason=args.reason,
                              catalog_path=args.catalog)
        _print(data, True) if args.json else _print_config_write(data)
    elif args.cfg_cmd == "unset":
        data = ops.unset_config(args.path, project=args.project, reason=args.reason,
                                catalog_path=args.catalog)
        _print(data, True) if args.json else _print_config_write(data)
    elif args.cfg_cmd == "wiring":
        data = ops.wiring_show(project=args.project, refresh=args.refresh,
                               catalog_path=args.catalog)
        if args.json:
            _print({"project": data["project"], "unwired": data["unwired"],
                    "errors": data["errors"],
                    "items": [vars(i) for i in data["items"]]}, True)
        else:
            _print_wiring(data)
    elif args.cfg_cmd == "history":
        rows = ops.config_history(project=args.project, limit=args.limit)
        if args.json:
            _print(rows, True)
        elif not rows:
            print("no configuration versions recorded yet — "
                  "`jarvis config adopt` records the file as the first")
        else:
            _print_config_versions(rows)
    elif args.cfg_cmd == "diff":
        data = ops.config_diff(args.a, args.b)
        if args.json:
            _print(data, True)
            return 0
        print(f"{data['a']['id']} ({data['a']['actor']}) → "
              f"{data['b']['id']} ({data['b']['actor']})")
        for change in data["changes"]:
            print(f"  {_cfg_change(change)}")
        if not data["changes"]:
            print("  (identical)")
    elif args.cfg_cmd == "restore":
        data = ops.restore_config(args.version_id, reason=args.reason,
                                  catalog_path=args.catalog)
        if args.json:
            _print(data, True)
            return 0
        print(f"✓ restored {data['restored']} — now the head")
        for change in data["changes"]:
            print(f"  {_cfg_change(change)}")
        if "restart" in data["classes"]:
            print("  ⚠ some of these need `jarvis start` to take effect")
    elif args.cfg_cmd == "adopt":
        data = ops.adopt_config(reason=args.reason, catalog_path=args.catalog)
        if args.json:
            _print(data, True)
            return 0
        print(("✓ " if data["adopted"] else "· ") + data["note"])
        print(f"  {data['version']['id']}")
        for change in data["changes"][:20]:
            print(f"  {_cfg_change(change)}")
    return 0


def cmd_onboard_start(args: argparse.Namespace) -> int:
    """`jarvis onboard start` — §5 of the onboarding-interview spec"""
    from . import ops

    data = ops.onboard_session(args.path, args.name)
    if args.print_prompt:
        print(f"persona root: {data['persona_root']}\n\n{data['prompt']}")
        return 0
    os.chdir(data["cwd"])
    os.execvp(data["argv"][0], data["argv"])
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    from . import ops

    if args.ob_cmd == "start":
        return cmd_onboard_start(args)
    if args.ob_cmd in ("questions", "explain"):
        return _cmd_onboard_read(args)
    if args.ob_cmd != "apply":
        return 0
    data = ops.onboard_apply(args.draft, catalog_path=args.catalog,
                             dry_run=args.dry_run)
    if args.json:
        _print(data, True)
        return 0

    head = "would onboard" if args.dry_run else "✓ onboarded"
    print(f"{head} {data['project']}: {len(data['changes'])} setting(s)")
    for change in data["changes"]:
        print(f"  {_cfg_change(change)}")
    if not data["changes"]:
        print("  (every answer was the shipped default — the document stays silent)")
    if args.dry_run:
        print("\n(dry run — nothing was written)")
        return 0
    print(f"  {data['version']['id']}"
          + ("" if data["changed"] else "  (the document was already this — "
                                        "no new version)"))
    for warning in data["bootstrap"]["warnings"]:
        print(f"  ⚠ {warning}")
    if data["restart_needed"]:
        print(f"\nnext: jarvis start --catalog {data['catalog']}")
        print(f"  {data['restart_why']}")
    return 0


def _backlog_origin(item: dict) -> str:
    """One line saying where a deferred item came from, or "" for one somebody typed"""
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
            items = ops.list_backlog(project=args.project,
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
            try:
                item = ops.show_backlog(args.item_id)
            except OpsError:
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


def _print_knowledge_usage(res: dict[str, Any], as_json: bool) -> None:
    """`jarvis learn stats` for a person: cost, use, and non-use, in that order"""
    if as_json:
        _print(res, True)
        return
    size, reads = res["size"], res["reads"]
    scope = res["project"] or "fleet"
    window = f", last {res['days']}d" if res["days"] else ""
    print(f"{scope}{window} — {res['entries']} entries, "
          f"{size['total_chars'] // 1000}k chars of body text "
          f"(median {size['median_chars']}, largest {size['max_chars']})\n")

    print("WHAT IT COSTS A PROMPT — the index ships, the entries do not")
    for c in res["prompt_cost"]:
        print(f"  {c['project']:<16} {c['index_chars']:>6} chars "
              f"({c['share_of_prompt'] * 100:.0f}% of a dispatch prompt) · "
              f"{c['indexed']} indexed, {c['pinned']} pinned, {c['overflow']} overflow · "
              f"{c['body_chars'] // 1000}k chars never sent")
    print(f"  {size['truncated_headlines']}/{res['entries']} entries reach the index as a "
          f"TRUNCATED first line")

    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    print(f"\nWHO READS IT — {plural(reads['reads'], 'read')}, {reads['by_workers']} by "
          f"workers across {plural(reads['orders'], 'order')}")
    if reads["by_verb"]:
        print("  " + " · ".join(f"{v}: {n}" for v, n in reads["by_verb"].items()))
    print(f"  {reads['chars'] // 1000}k chars fetched in total, "
          f"~{res['read_chars_per_order']} per order that read at all")
    if reads["misses"]:
        print(f"  {plural(reads['misses'], 'read')} came back EMPTY — the base was asked "
              f"and had nothing")
    for u in reads["unanswered"][:5]:
        print(f"    {u['wo_id'] or 'you':<14} {u['verb']} {u['term']!r}")

    print(f"\nWHAT IS NEVER READ — {res['never_read_count']}/{res['entries']} entries")
    for e in res["never_read"][:5]:
        print(f"  {e['id']}  {e['chars']:>5} chars  {e['headline'][:70]}")
    if res["top_entries"] and res["top_entries"][0]["reads"]:
        print("  most read:")
        for e in res["top_entries"][:5]:
            if e["reads"]:
                print(f"  {e['id']}  {e['reads']:>3} × {e['headline'][:70]}")

    if res["observed_from"] is None:
        print("\nORDERS THAT NEVER LOOKED — nothing recorded yet: the read log starts at "
              "the first `jarvis learn` a worker runs, and orders that finished before "
              "it were never observed")
        return
    since = _age(res["observed_from"])
    print(f"\nORDERS THAT NEVER LOOKED — {res['silent_order_count']} completed without "
          f"one read, of those started in the {since} since the log began")
    # A title match is a hint, not a verdict: it is the same LIKE search a worker would have run
    print(f"  {res['could_have_read_count']} of them had an entry matching their own "
          f"title, recorded before they started:")
    for m in res["could_have_read"][:5]:
        print(f"  {m['wo_id']}  {m['title'][:52]}")
        for e in m["entries"][:2]:
            print(f"      → {e['id']}  {e['headline'][:62]}")


#: What an absent figure prints
NOT_RECORDED = "not recorded"


def _pct(rate: float | None) -> str:
    """A rate as a percentage, or "not recorded" — NEVER `0%` for an absent one (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
    return NOT_RECORDED if rate is None else f"{rate * 100:.0f}%"


def _ms(value: int | None) -> str:
    return NOT_RECORDED if value is None else f"{value}ms"


def _objection_line(counts: dict[str, Any]) -> str:
    """What the OS's objections did, in one clause — `ops.neo_stats_report`'s figures"""
    return (f"{counts['sent']} sent · {counts['delivered']} delivered · "
            f"{counts.get('acted_on', 0)} acted on · "
            f"{counts['withdrawn']} withdrawn ({counts['stopped']} the order stopped, "
            f"{counts['handed_back']} handed to you, {counts['other']} other) · "
            f"withdrawal rate {_pct(counts.get('withdrawal_rate'))}")


def _print_assumption_rows(a: dict[str, Any]) -> None:
    """THE HEADLINE: what happened to each assumption ROW (io-51469add)"""
    rates = a["rates"]
    print(f"ASSUMPTIONS — {a['rows']} row{'' if a['rows'] == 1 else 's'}, "
          f"{a['settled']} settled; the rate your review list shows")
    print(f"  decided by Neo      {a['neo_decided']:>4}  {_pct(rates['neo_decided'])}")
    print(f"  escalated to you    {a['neo_escalated']:>4}  "
          f"{_pct(rates['neo_escalated'])}  (Neo saw it and it came to you)")
    print(f"  never reached Neo   {a['never_reached_neo']:>4}  "
          f"{_pct(rates['never_reached_neo'])}  (held before any model call)")
    for code, n in sorted(a["never_reached_by_hold"].items(), key=lambda kv: -kv[1]):
        print(f"    {code:<22} {n:>4}")
    u = a["unsettled"]
    age = ("" if u["median_age_days"] is None else
           f", median age {u['median_age_days']}d, oldest {u['max_age_days']}d")
    print(f"  still unsettled     {u['count']:>4}  ({u['reached_neo']} reached Neo, "
          f"{u['never_reached_neo']} never did{age}) — out of the rates")
    d, net = a["dropped_rulings"], a["stakes_net"]
    print(f"  dropped rulings     {d['total']:>4}  ({d['re_asked']} re-asked; the rest "
          f"came to you)")
    print(f"  high-stakes net     {net['held']:>4} held · {net['false_alarms']} you "
          f"called a false alarm · {_pct(net['false_alarm_rate'])}")


def _print_neo_stats(res: dict[str, Any], as_json: bool) -> None:
    """`jarvis neo stats` for a person"""
    if as_json:
        _print(res, True)
        return
    q = res["questions"]
    window = f", last {res['days']}d" if res["days"] else ", all time"
    print(f"{res['scope']}{window} — {res['assumptions']['rows']} assumption row"
          f"{'' if res['assumptions']['rows'] == 1 else 's'}, {q['asked']} question"
          f"{'' if q['asked'] == 1 else 's'} asked\n")
    _print_assumption_rows(res["assumptions"])
    print(f"  objections          {_objection_line(res['objections'])}\n")

    print("QUESTIONS — plumbing telemetry")
    print(f"  note: {res['questions_note']}")
    print(f"  answered by Neo   {q['answered']}")
    print(f"  escalated         {q['escalated']}")
    print(f"  unreachable       {q['failed']}  (Neo was never reached — NOT judged)")
    print(f"  still open        {q['open']}")
    print(f"  superseded        {q['superseded']}  (decided elsewhere, out of the rate)")
    print(f"  refined           {q['refined']}  (returned to the asker to split, out of the rate)")
    print(f"  escalation rate   {_pct(q['escalation_rate'])}  "
          f"(over settled questions only)")
    # Its own figure, never blended into the escalation rate: a crash is not a decision
    print(f"  unreachable rate  {_pct(q['unreachable_rate'])}  "
          f"(same denominator — NOT escalations)")

    if res["by_day"]:
        print("\nTHE TREND — one line per day, newest last")
        for d in res["by_day"]:
            print(f"  {d['day']}  asked {d['asked']:>3} · answered {d['answered']:>3} · "
                  f"escalated {d['escalated']:>3} · rate {_pct(d['escalation_rate'])} · "
                  f"never reached {d['failed']:>3} · unreachable "
                  f"{_pct(d['unreachable_rate'])}")

    causes = res["causes"]
    print("\nWHAT IT ESCALATES FOR — three classes, because WHO DECIDED has three "
          "answers")
    # Three labelled groups, each with the sentence that says what the class means (docs/superpowers/specs/2026-10-01-neo-observability.md)
    groups = (("chosen", "Neo chose to hand it back", "Neo chose this label"),
              ("overridden", "Neo answered and the OS overrode it",
               "the OS derived it from Neo's answer"),
              ("failed", "Neo never answered",
               "the OS derived it — no judgement at all"))
    for key, heading, per_row in groups:
        bucket = causes[key]
        print(f"  {heading}")
        if not bucket:
            print(f"    {NOT_RECORDED} — none in this window")
            continue
        for cause, n in sorted(bucket.items(), key=lambda kv: -kv[1]):
            print(f"    {cause:<24} {n:>4}   ({per_row})")
    if causes["not_recorded"]:
        # NOT printed as a cause: nobody may read the backlog as a finding about Neo
        print(f"  {causes['not_recorded']} escalation"
              f"{'' if causes['not_recorded'] == 1 else 's'} predate cause recording")
    # The class that is in NO count above, said as what the reader cannot see here
    print(f"  note: {res['causes_note']}")

    print("\nBY KIND")
    for kind, k in sorted(res["by_kind"].items(), key=lambda kv: -kv[1]["asked"]):
        print(f"  {kind:<12} asked {k['asked']:>4} · answered {k['answered']:>4} · "
              f"escalated {k['escalated']:>4} · rate {_pct(k['escalation_rate'])} · "
              f"never reached {k['failed']:>4} · unreachable "
              f"{_pct(k['unreachable_rate'])}")
    if res["by_kind"]:
        print(f"  note: {res['by_kind_note']}")

    per = res["per_order"]
    print("\nPER ORDER — triage questions excluded (they have no order behind them)")
    wo_avg = per["questions_per_wo"]
    fo_avg = per["questions_per_fo"]
    print(f"  {per['work_orders']} work order"
          f"{'' if per['work_orders'] == 1 else 's'}, "
          f"{NOT_RECORDED if wo_avg is None else f'{wo_avg} questions each'}")
    print(f"  {per['feature_orders']} feature order"
          f"{'' if per['feature_orders'] == 1 else 's'}, "
          f"{NOT_RECORDED if fo_avg is None else f'{fo_avg} questions each'}")

    spend, totals = res["spend"], res["spend"]["totals"]
    print(f"\nWHAT NEO COST — {totals['calls']} call"
          f"{'' if totals['calls'] == 1 else 's'}, "
          f"${totals['recorded_cost_usd']:.2f} recorded / "
          f"~${totals['list_cost_usd']:.2f} at list prices")
    for kind, s in sorted(spend["by_kind"].items(), key=lambda kv: -kv[1]["calls"]):
        print(f"  {kind:<18} {s['calls']:>4} calls · {s['input']:>8} in · "
              f"{s['cache_write']:>9} cache write · {s['cache_read']:>10} cache read · "
              f"{s['output']:>7} out · ${s['recorded_cost_usd']:.2f}")

    lat = res["latency"]
    print("\nHOW LONG IT TOOK")
    for kind, entry in sorted(lat["by_kind"].items(), key=lambda kv: -kv[1]["calls"]):
        print(f"  {kind:<18} {entry['measured']}/{entry['calls']} timed · "
              f"p50 {_ms(entry['p50_ms'])} · p90 {_ms(entry['p90_ms'])} · "
              f"max {_ms(entry['max_ms'])}")
    if lat["unmeasured"]:
        print(f"  {lat['unmeasured']} call"
              f"{'' if lat['unmeasured'] == 1 else 's'} in this window were never timed")
    if not lat["by_kind"]:
        print(f"  {NOT_RECORDED} — no call in this window")
    print(f"\nThe spend and latency figures are {res['floor_reason']}")


def cmd_learn(args: argparse.Namespace) -> int:
    import os

    from . import ops
    from .central_store import PINNED_TAG, CentralStore, has_tag, headline, split_tags
    from .ops import OpsError

    # Which work order is at this terminal — attributing BOTH the reads and the WRITES below (#200)
    acting_wo = os.environ.get("JARVIS_WO_ID", "")
    reader_project = (os.environ.get("JARVIS_PROJECT", "")
                      or getattr(args, "project", "") or "")

    def digested(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Index form: enough to decide whether to fetch the entry, not the entry"""
        out = []
        for r in rows:
            row = {"id": r["id"], "project": r["project"] or "global", "topic": r["topic"],
                   "pinned": has_tag(r["tags"], PINNED_TAG),
                   "headline": headline(r["content"])}
            if r.get("retired_at"):
                row["retired"] = r["retired_reason"] or "(no reason recorded)"
            out.append(row)
        return out

    def excerpted(rows: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
        """`search`'s index form: `digested` plus what a reader needs to choose"""
        words = [w.lower() for w in (term or "").split() if any(c.isalnum() for c in w)]
        out = []
        for row, r in zip(digested(rows), rows, strict=True):
            row["chars"] = len(r["content"] or "")
            for line in (r["content"] or "").split("\n")[1:]:
                low = line.lower()
                if any(w in low for w in words) and headline(line):
                    row["excerpt"] = headline(line)
                    break
            out.append(row)
        return out

    central = CentralStore()
    try:
        if args.kn_cmd == "add":
            tags = split_tags(args.tags)
            if args.pin and PINNED_TAG not in tags:
                tags.append(PINNED_TAG)
            # Through `ops`, not the store: the write is attributed to the work order that made it and lands on... (#200)
            _print(ops.learn_add(args.content, project=args.project, topic=args.topic,
                                 tags=",".join(tags), wo_id=acting_wo), args.json)
        elif args.kn_cmd == "list":
            # An audit surface, so retired entries are listed too
            rows = central.search_knowledge("", limit=args.limit, project=args.project,
                                            topic=args.topic)
            central.record_knowledge_read(
                "list", rows, term=args.topic or "", project=reader_project,
                wo_id=acting_wo,
                chars=sum(len(r["content"] if args.full else headline(r["content"]))
                          for r in rows))
            _print(rows if args.full else digested(rows), args.json)
        elif args.kn_cmd == "search":
            rows = central.search_knowledge(args.term, limit=args.limit,
                                            project=args.project, topic=args.topic)
            index = excerpted(rows, args.term)
            # Charged for what was PRINTED, like the `list` branch above: the index rows (docs/superpowers/specs/2026-10-02-learn-search-returns-an-index.md)
            central.record_knowledge_read(
                "search", rows, term=args.term, project=reader_project, wo_id=acting_wo,
                chars=sum(len(r["headline"]) + len(r.get("excerpt", "")) for r in index))
            if not rows and not args.json:
                print(f"no knowledge matching {args.term!r} — "
                      f"try `jarvis learn topics` for what is recorded")
            elif not args.json:
                print("index only — `excerpt` is ONE quoted matching line, never the "
                      "entry; `jarvis learn show <id>` for a body "
                      "(`chars` prices it first)")
            _print(index, args.json)
        elif args.kn_cmd == "show":
            _print(ops.show_knowledge(args.ids, project=reader_project, wo_id=acting_wo),
                   args.json)
        elif args.kn_cmd == "graph":
            import sqlite3

            from . import kgraph
            from .paths import neo_db_path

            neo_path = neo_db_path()
            neo = (sqlite3.connect(f"file:{neo_path}?mode=ro", uri=True)
                   if neo_path.exists() else None)
            nodes, edges = kgraph.graph_nodes_edges(
                central.conn, neo, reads=central.knowledge_hit_counts())
            Path(args.output).write_bytes(kgraph.to_gexf(nodes, edges))
            print(f"{len(nodes)} nodes, {len(edges)} edges -> {args.output} "
                  f"(open it in Gephi Lite: gephi.org/gephi-lite)")
        elif args.kn_cmd == "topics":
            topics = central.knowledge_topics(args.project)
            central.record_knowledge_read("topics", [{"topic": t} for t, _ in topics],
                                          project=reader_project, wo_id=acting_wo)
            _print([{"topic": t or "(no topic)", "entries": n} for t, n in topics],
                   args.json)
        elif args.kn_cmd == "stats":
            _print_knowledge_usage(ops.knowledge_usage_report(
                project=args.project, days=args.days), args.json)
        elif args.kn_cmd in ("pin", "unpin"):
            _print(ops.pin_knowledge(args.kn_id, pinned=args.kn_cmd == "pin"), args.json)
        elif args.kn_cmd == "retract":
            try:
                row = ops.learn_retract(args.knowledge_id, args.reason,
                                        wo_id=acting_wo)
            except (KeyError, ValueError) as exc:
                # `.args[0]`, not `str(exc)`: KeyError stringifies to its repr
                print(f"error: {exc.args[0]}", file=sys.stderr)
                return 1
            _print({"retracted": row["id"], "reason": row["retired_reason"],
                    "content": row["content"]}, args.json)
    finally:
        central.close()
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """`jarvis brief <section> [--wo <wo-id>]` — one full briefing section, on demand"""
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
        # Resolve the project from the work order when the OS knows it
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
                                      gates_enabled=gates_enabled,
                                      serena=os.environ.get("JARVIS_SERENA") != "0",
                                      tool_search=os.environ.get(
                                          "JARVIS_TOOL_SEARCH", "cli"),
                                      transport=os.environ.get(
                                          "JARVIS_TURN_TRANSPORT", "background")))
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
            # `answered_by == "neo"` matches NeoStore.counts
            qs = [q for q in qs
                  if q["status"] in ("queued", "answering", "escalated", "failed")
                  or (q["status"] == "answered" and q["review_status"] == "unreviewed"
                      and q["answered_by"] == "neo")]
        if args.json:
            _print(qs, True)
        else:
            icon = {"queued": "⏳", "answering": "🤔", "answered": "💬",
                    "escalated": "🙋", "failed": "❌"}
            # `failed` RENDERS AS `unreachable`
            label = {"failed": "unreachable — NOT judged"}
            for q in qs:
                review = f" [{q['review_status']}]" if q["status"] == "answered" else ""
                held = (" · retrying" if q["status"] == "queued"
                        and (q.get("attempts") or 0) else "")
                print(f"{icon.get(q['status'], '•')} #{q['id']} [{q['project']}] "
                      f"{q['wo_id']} ({label.get(q['status'], q['status'])}{review}"
                      f"{held}, {_age(q['ts'])}) {q['question'][:80]}")
            if not qs:
                print("nothing pending for Neo ✨")
    elif args.neo_cmd == "show":
        neo = NeoStore()
        try:
            q = neo.get(args.question_id)
            # Read the deliberation only when it is asked for
            opinions = neo.opinions(args.question_id) if q and args.panel else []
        finally:
            neo.close()
        if q is None:
            print(f"error: neo question {args.question_id} not found", file=sys.stderr)
            return 1
        # `--json` gets the row exactly as it is stored
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
        # The seat scope is additive and the default is NARROW: no `--seat` shows the global rows alone
        ops.validate_seat(args.seat or "")
        neo = NeoStore()
        try:
            # An audit surface: retired learnings are listed, marked, and shown with the reason they were retired
            rows = neo.learnings(args.project, limit=200, seat=args.seat or "",
                                 include_retired=True)
        finally:
            neo.close()
        if args.json:
            _print(rows, True)
        else:
            from . import neo as neo_mod
            for r in rows:
                scope = r["project"] or "global"
                retired = (f" [retired: {r['retired_reason']}]"
                           if r["retired_at"] else "")
                bullet = "⊘" if r["retired_at"] else "•"
                seat = f" · {r['seat']} seat" if r["seat"] else ""
                origin = neo_mod.learning_origin(r)
                print(f"{bullet} #{r['id']} {_stamp(r['ts'])} [{scope}{seat}] "
                      f"({r['source']}{f' from {origin}' if origin else ''}) "
                      f"{r['content']}{retired}")
            if not rows:
                print("Neo has no learnings yet — review its answers to teach it")
    elif args.neo_cmd == "stats":
        _print_neo_stats(ops.neo_stats_report(project=args.project or None,
                                              days=args.days), args.json)
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


def cmd_validation_force(args: argparse.Namespace) -> int:
    """`jarvis validation force <wo-id> --reason` — a person opening a fresh round"""
    from . import ops

    result = ops.force_validation(args.wo_id, reason=args.reason,
                                  project_name=args.project)
    if args.json:
        _print(result, True)
        return 0
    first, second = ops.forced_round_lines(result)
    print(first)
    print(f"  {second}. `jarvis validation show {result['wo_id']}` for the verdict")
    return 0


def cmd_validation(args: argparse.Namespace) -> int:
    """`jarvis validation show <id>` — the panel's whole deliberation on one unit"""
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
              f"(`os.validation.enabled`, or this project's `validation.enabled`)")
    for rnd in view["rounds"]:
        print(f"\n{ops.round_line(rnd)}")
        if rnd["evidence"]:
            print(f"  evidence: {rnd['evidence']}")
        # THE ISSUE BESIDE THE FINDING THAT CAUSED IT
        filed = {i["title"]: i
                 for i in (rnd.get("follow_ups") or {}).get("filed") or []}
        for o in rnd["opinions"]:
            print(f"  {o['seat']:<10} {o['status']:<9} "
                  f"verdict={o['verdict'] or '—':<7} "
                  f"{o['latency_ms']}ms  {o['model'] or '—'}")
            for line in _finding_lines(o, filed):
                print(f"      {line}")
            for line in (o["reply"] or "").splitlines():
                print(f"      {line}")
        if not rnd["opinions"]:
            print("  no seat opined on this round")
        follow_ups = rnd.get("follow_ups") or {}
        if dropped := follow_ups.get("dropped"):
            print(f"  {dropped} further follow-up(s) went over the per-round cap and "
                  f"were not filed; a later round may raise them again")
        for item in follow_ups.get("withheld") or []:
            print(f"  kept internally: {item.get('title') or ''} — no issue was opened "
                  f"({follow_ups.get('reason') or 'the tracker may be public'})")
        if failed := follow_ups.get("failed"):
            why = follow_ups.get("reason") or "the tracker refused or was unreachable"
            print(f"  {failed} follow-up(s) could not be filed as issues — {why}")
    # Spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §5
    if view.get("review"):
        print()
        for line in ops.review_lines(view["review"]):
            print(line)
    # The bus, on the same page as the rounds it carried
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
    """Write to the project outbox (daemon routes it)"""
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
    from .bugreport import pickup_note, report_bug
    result = report_bug(title=args.title, description=args.description,
                        expected=args.expected, actual=args.actual, steps=args.steps,
                        priority=args.priority, expedite=args.expedite,
                        project=args.project, wo_id=args.wo_id)
    if result.get("note"):
        print(f"note: {result['note']}", file=sys.stderr)
    if args.json:
        _print(result, True)
    else:
        # The nested `pickup` flattened to the one line a reporting agent needs: whether anything is going to...
        _print({k: v for k, v in result.items() if k != "pickup"}, False)
        if result.get("duplicate_of"):
            print(f"already filed as {result['duplicate_of']} — your evidence is a "
                  f"comment there, and no second issue was opened")
        else:
            print(pickup_note(result["pickup"]))
    return 0


#: What each of the two lists IS and which command clears it
ATTENTION_LIST_NOTE = ("derived from each order's state every tick; clears when the "
                       "condition goes, or with `jarvis wo ack <id>`")
INBOX_LIST_NOTE = ("a log of notifications; `jarvis inbox ack` marks them read — one "
                   "raised with an attention flag lowers that flag too, unless a "
                   "decision is pending")


def cmd_inbox(args: argparse.Namespace) -> int:
    from . import ops
    if args.ib_cmd == "ack":
        _print(ops.ack_inbox(args.inbox_id), args.json)
        return 0
    items = ops.select_inbox(ops.unacked_inbox(), level=args.level,
                             project=args.project, q=args.search, sort=args.sort)
    if args.json:
        _print(items, True)
        return 0
    for it in items:
        print(f"[{it['id']}] {it['level'].upper()} [{it['project']}] "
              f"{it['title']} ({_age(it['ts'])})"
              + (f" — {it['body']}" if it["body"] else ""))
    if not items:
        print("nothing matches these filters" if args.level or args.project
              or args.search else "inbox empty ✨")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from . import ops
    port = args.port
    if port is None:
        try:
            port = ops.resolve_catalog().os.ui_port
        except Exception:  # noqa: BLE001
            port = 8787
    return _serve_dashboard(args.host, port)


def _serve_dashboard(host: str, port: int, open_path: str | None = None) -> int:
    """The dashboard, in the foreground; `open_path` is opened in a browser once it listens"""
    try:
        import uvicorn
    except ImportError:
        print("the web UI needs extras: pip install 'jarvis-os[ui]'", file=sys.stderr)
        return 1
    from .ui.app import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(), host=host, port=port,
                                           log_level="warning"))
    if open_path is not None:
        import threading
        import time
        import webbrowser

        def open_when_up() -> None:
            for _ in range(100):  # 10s: the app factory is quick, the browser is not ours
                if server.started:
                    webbrowser.open(f"http://{host}:{port}{open_path}")
                    return
                time.sleep(0.1)
        threading.Thread(target=open_when_up, daemon=True).start()
    server.run()
    return 0


def _free_port(host: str) -> int:
    import socket
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def cmd_learn_review(args: argparse.Namespace) -> int:
    """`jarvis learn review`: the dashboard's knowledge-review page, on its own port"""
    port = args.port or _free_port(args.host)
    print(f"knowledge review: http://{args.host}:{port}/knowledge/review  (Ctrl-C to stop)",
          flush=True)
    return _serve_dashboard(args.host, port,
                            open_path=None if args.no_browser else "/knowledge/review")


def main(argv: list[str] | None = None) -> int:
    argv = _normalise_investigate(_normalise_issues(
        _normalise_alarms(list(sys.argv[1:] if argv is None else argv))))
    # accept --json anywhere, not only before the subcommand
    as_json = "--json" in argv
    args = build_parser().parse_args([a for a in argv if a != "--json"])
    args.json = as_json
    if args.cmd == "_hook":
        from .hooks import main_hook
        return main_hook()
    from .bugreport import BugReportError
    from .catalog import CatalogError
    # A refusal a worker can act on, not a crash (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    from .neo_store import QuestionTooLargeError
    from .ops import OpsError
    try:
        fill_prose_from_files(args)
        if args.cmd == "start":
            return cmd_start(args)
        if args.cmd == "stop":
            return cmd_stop(args)
        if args.cmd == "pause":
            return cmd_pause(args)
        if args.cmd == "resume":
            return cmd_resume(args)
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "doctor":
            return cmd_doctor(args)
        if args.cmd == "cost":
            return cmd_cost(args)
        if args.cmd == "inspect":
            return cmd_inspect(args)
        if args.cmd == "navigation":
            return cmd_navigation(args)
        if args.cmd == "watch":
            return cmd_watch(args)
        if args.cmd == "alarms":
            return cmd_alarms(args)
        if args.cmd == "steward":
            return cmd_steward(args)
        if args.cmd == "stuck":
            return cmd_stuck(args)
        if args.cmd == "search":
            return cmd_search(args)
        if args.cmd == "spec":
            return cmd_spec(args)
        if args.cmd == "issues":
            return (cmd_issues_start(args) if args.issues_cmd == "start"
                    else cmd_issues(args))
        if args.cmd == "supervisor":
            return cmd_supervisor(args)
        if args.cmd == "adopt":
            return cmd_adopt(args)
        if args.cmd == "offboard":
            return cmd_offboard(args)
        if args.cmd == "wo":
            return cmd_wo(args)
        if args.cmd == "fo":
            return cmd_fo(args)
        if args.cmd == "io":
            return cmd_io(args)
        if args.cmd == "investigate":
            return cmd_investigate(args)
        if args.cmd == "morpheus":
            return cmd_morpheus(args)
        if args.cmd == "gate":
            return cmd_gate(args)
        if args.cmd == "rules":
            return cmd_rules(args)
        if args.cmd == "config":
            return cmd_config(args)
        if args.cmd == "onboard":
            return cmd_onboard(args)
        if args.cmd == "backlog":
            return cmd_backlog(args)
        if args.cmd == "brief":
            return cmd_brief(args)
        if args.cmd == "learn":
            if args.kn_cmd == "review":
                return cmd_learn_review(args)
            return cmd_learn(args)
        if args.cmd == "neo":
            return cmd_neo(args)
        if args.cmd == "validation":
            if args.validation_cmd == "force":
                return cmd_validation_force(args)
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
    except (OpsError, CatalogError, BugReportError, QuestionTooLargeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
