"""The report a decision order's researcher submits: parse (docs/superpowers/specs/2026-10-31-decision-orders.md §1.3)"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import plans

#: Longest the summary may be: one paragraph, numbers first
MAX_SUMMARY_CHARS = 1500

#: Most findings a report may carry
MAX_FINDINGS = 20

#: Most options a report may carry
MAX_OPTIONS = 10

#: Most proposed follow-up orders a report may carry
MAX_PROPOSED_ORDERS = 10

#: What a proposed order may become when the user files it
ORDER_TYPES = ("wo", "fo", "backlog", "experiment")

KEY_RE = plans.KEY_RE

#: Embedded verbatim in the researcher's brief, so brief and parser cannot drift
SCHEMA_TEXT = """{
  "summary": "one paragraph: the answer to the ask, numbers first",
  "findings": [{"key": "f1", "title": "...", "detail": "...", "evidence": ["..."]}],
  "options": [{"key": "a", "title": "...", "pros": ["..."], "cons": ["..."], "cost": "..."}],
  "recommendation": {"option": "a", "why": "..."},
  "proposed_orders": [{"key": "p1", "as": "wo|fo|backlog|experiment (experiment: RUN something for this decision; reports back here, the user starts it)",
                       "project": "", "title": "...", "description": "..."}]
}"""

#: The researcher's `forecast.json`; `experiment_forecast` parses exactly this (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §5)
FORECAST_SCHEMA_TEXT = """State what you know about the batch; the OS computes tokens, dollars and the
share of the 5-hour window. `runs` >= 1 (at most 10000), `model` non-empty, every token
count >= 0, `notes` at most 1000 characters:
{"runs": 24, "model": "claude-…", "per_run": {"input_tokens": 40000, "output_tokens": 3000,
 "minutes": 2.5}, "notes": "4 fixtures x 2 engines x 3 repeats"}"""


def experiment_phase(entry: Mapping[str, Any]) -> str:
    """Where one experiment stands, from its stamps alone (same spec §2)"""
    if entry.get("reported_at") is not None:
        return "reported"
    if entry.get("go_at") is not None:
        return "running"
    if entry.get("forecast") is not None:
        return "awaiting_go"
    return "forecast_due"


def experiment_brief(fo: Mapping[str, Any], entry: Mapping[str, Any],
                     proposal: Mapping[str, Any] | None) -> str:
    """What the researcher is told to run, forecast FIRST (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §4.1)"""
    do_id, n = fo["id"], entry["n"]
    what = (str((proposal or {}).get("description") or "").strip()
            or str(entry.get("brief") or "").strip())
    return "\n\n".join([
        f"You are running experiment {n} of decision order {do_id}, for the user's "
        f"decision: {fo['title']}.",
        f"The experiment:\n{what}",
        f"FIRST, forecast it and submit `jarvis do forecast {do_id} --from-file "
        f"forecast.json`. {FORECAST_SCHEMA_TEXT}\nThat ends your turn. Do NOT start the "
        f"batch: the user says go.",
        f"On go: run it. Keep raw outputs in your temp dir, then `jarvis do attach "
        f"{do_id} <file>…` every raw output the findings rest on.",
        f"Resubmit the WHOLE report with `jarvis do report {do_id} --from-file "
        f"report.json`: the results go in `findings`, each with evidence naming the "
        f"attached files, and an updated `summary` / `recommendation`. Keep the earlier "
        f"findings that still hold.",
        "Never file orders, never decide: the user does both.",
    ])


class DecisionReportError(ValueError):
    """A submitted decision report that cannot be accepted"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def _text(entry: Mapping[str, Any], name: str) -> str:
    return str(entry.get(name) or "").strip()


def _strings(where: str, name: str, raw: Any, problems: list[str]) -> list[str]:
    """A list of free-text items, `[]` when absent"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"{where}: `{name}` must be a list of strings")
        return []
    out = [str(item).strip() for item in raw if str(item).strip()]
    if len(out) != len(raw):
        problems.append(f"{where}: `{name}` holds an empty item")
    return out


def _entries(raw: Any, name: str, cap: int, problems: list[str], *,
             required: bool) -> list[tuple[str, dict[str, Any]]]:
    """`(key, entry)` for each object in one keyed list, every key problem reported"""
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw):
        problems.append(f"`{name}` must be a {'non-empty ' if required else ''}list")
        return []
    if len(raw) > cap:
        problems.append(f"`{name}` has {len(raw)} entries, over the cap of {cap}")
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        where = f"{name} {i + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be an object, got {type(entry).__name__}")
            continue
        key = _text(entry, "key")
        if not KEY_RE.match(key):
            problems.append(f"{where}: `key` must be a short lowercase slug "
                            f"([a-z0-9][a-z0-9_-]*), got {key!r}")
            key = f"?{i}"  # so later messages about this entry still locate it
        elif key in seen:
            problems.append(f"{where}: duplicate key {key!r}")
        seen.add(key)
        out.append((key, entry))
    return out


def parse_report(doc: Any) -> dict[str, Any]:
    """Validate a submitted report and return it normalised; EVERY problem at once"""
    if not isinstance(doc, dict):
        raise DecisionReportError(
            [f"a decision report must be a JSON object, got {type(doc).__name__}"])
    problems: list[str] = []

    summary = _text(doc, "summary")
    if not summary:
        problems.append("`summary` is required: one paragraph, the answer to the ask, "
                        "numbers first")
    elif len(summary) > MAX_SUMMARY_CHARS:
        problems.append(f"`summary` is {len(summary)} characters, over the "
                        f"{MAX_SUMMARY_CHARS} cap — the detail belongs in the findings")

    findings = []
    for key, entry in _entries(doc.get("findings"), "findings", MAX_FINDINGS, problems,
                               required=True):
        where = f"finding {key!r}"
        for name in ("title", "detail"):
            if not _text(entry, name):
                problems.append(f"{where}: `{name}` is required")
        findings.append({"key": key, "title": _text(entry, "title"),
                         "detail": _text(entry, "detail"),
                         "evidence": _strings(where, "evidence", entry.get("evidence"),
                                              problems)})

    options = []
    for key, entry in _entries(doc.get("options"), "options", MAX_OPTIONS, problems,
                               required=False):
        where = f"option {key!r}"
        if not _text(entry, "title"):
            problems.append(f"{where}: `title` is required")
        options.append({"key": key, "title": _text(entry, "title"),
                        "pros": _strings(where, "pros", entry.get("pros"), problems),
                        "cons": _strings(where, "cons", entry.get("cons"), problems),
                        "cost": _text(entry, "cost")})

    raw_rec = doc.get("recommendation")
    recommendation = {"option": "", "why": ""}
    if not isinstance(raw_rec, dict):
        problems.append("`recommendation` is required: an object with `option` and `why`")
    else:
        recommendation = {"option": _text(raw_rec, "option"), "why": _text(raw_rec, "why")}
        if not recommendation["why"]:
            problems.append("`recommendation.why` is required")
        keys = [o["key"] for o in options]
        if options and recommendation["option"] not in keys:
            problems.append(f"`recommendation.option` must name one of the options "
                            f"({', '.join(keys)}), got {recommendation['option']!r}")

    proposed = []
    for key, entry in _entries(doc.get("proposed_orders"), "proposed_orders",
                               MAX_PROPOSED_ORDERS, problems, required=False):
        where = f"proposed order {key!r}"
        as_ = _text(entry, "as")
        if as_ not in ORDER_TYPES:
            problems.append(f"{where}: `as` must be one of {', '.join(ORDER_TYPES)}, "
                            f"got {as_!r}")
        for name in ("title", "description"):
            if not _text(entry, name):
                problems.append(f"{where}: `{name}` is required")
        proposed.append({"key": key, "as": as_, "project": _text(entry, "project"),
                         "title": _text(entry, "title"),
                         "description": _text(entry, "description")})

    if problems:
        raise DecisionReportError(problems)
    return {"summary": summary, "findings": findings, "options": options,
            "recommendation": recommendation, "proposed_orders": proposed}


def _recommended(report: Mapping[str, Any]) -> dict[str, Any] | None:
    rec = report.get("recommendation") or {}
    key = rec.get("option") if isinstance(rec, dict) else None
    return next((o for o in report.get("options") or []
                 if isinstance(o, dict) and o.get("key") == key), None)


def _brief_lines(report: Mapping[str, Any]) -> list[str]:
    lines = [str(report.get("summary") or ""), ""]
    rec = report.get("recommendation") or {}
    chosen = _recommended(report)
    if isinstance(rec, dict):
        head = f"{chosen['key']}: {chosen['title']}" if chosen else (rec.get("option") or "")
        lines += [f"Recommendation: {head or '(no option named)'}",
                  f"  why: {rec.get('why') or ''}", ""]
    return lines


def render_brief(report: Mapping[str, Any]) -> str:
    """The summary and the recommendation only: the inbox body (spec §5.1)"""
    return "\n".join(_brief_lines(report)).rstrip() if report else "no report yet"


def awaiting_reason(fo: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    """The attention reason a delivered report raises, and INV-DECISION-AWAITING-FLAGGED restores"""
    return f"decision awaiting you: {headline(fo, report)} — jarvis do show {fo.get('id')}"


def render_report(report: Mapping[str, Any]) -> str:
    """Plain text for `jarvis do show`"""
    if not report:
        return "no report yet"
    lines = _brief_lines(report)
    chosen = _recommended(report)
    findings = [f for f in report.get("findings") or [] if isinstance(f, dict)]
    if findings:
        lines.append(f"Findings ({len(findings)}):")
        for f in findings:
            lines.append(f"  {f.get('key')}. {f.get('title') or ''}")
            if f.get("detail"):
                lines.append(f"     {f['detail']}")
            for ev in f.get("evidence") or []:
                lines.append(f"     - {ev}")
        lines.append("")
    options = [o for o in report.get("options") or [] if isinstance(o, dict)]
    if options:
        lines.append(f"Options ({len(options)}):")
        for o in options:
            mark = " (recommended)" if chosen and o.get("key") == chosen["key"] else ""
            lines.append(f"  {o.get('key')}. {o.get('title') or ''}{mark}")
            for pro in o.get("pros") or []:
                lines.append(f"     + {pro}")
            for con in o.get("cons") or []:
                lines.append(f"     - {con}")
            if o.get("cost"):
                lines.append(f"     cost: {o['cost']}")
        lines.append("")
    orders = [p for p in report.get("proposed_orders") or [] if isinstance(p, dict)]
    if orders:
        lines.append(f"Proposed follow-up orders ({len(orders)}), yours to file:")
        for p in orders:
            where = f" [{p['project']}]" if p.get("project") else ""
            # §6 keeps `filed` as a list of marks; show the order ids
            filed = ", ".join(str(m.get("id")) for m in p.get("filed") or []
                              if isinstance(m, dict))
            lines.append(f"  {p.get('key')}. ({p.get('as')}){where} {p.get('title') or ''}"
                         + (f" — filed as {filed}" if filed else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


PHASE_LABEL = {"forecast_due": "forecast due", "awaiting_go": "awaiting your go",
               "running": "running", "reported": "reported"}


def render_experiments(do_id: str, experiments: list[Mapping[str, Any]]) -> str:
    """One block per experiment for `jarvis do show` (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §7)"""
    from . import experiment_forecast

    lines: list[str] = []
    for e in experiments:
        phase = e.get("phase") or experiment_phase(e)
        lines.append(f"Experiment {e.get('n')}: {e.get('title') or e.get('key') or 'ad hoc'} "
                     f"({PHASE_LABEL.get(phase, phase)})")
        if e.get("forecast"):
            lines += [f"  {line}" for line in
                      experiment_forecast.render(e["forecast"]).splitlines()]
        for a in e.get("attachments") or []:
            lines.append(f"  - {a.get('abs_path') or a.get('path')} ({a.get('size', 0):,} bytes)")
        if phase == "awaiting_go":
            lines.append(f"  go: jarvis do experiment {do_id} --go")
        lines.append("")
    return "\n".join(lines).rstrip()


def headline(fo: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    """One line: counts plus the recommended option's title"""
    n_f = len(report.get("findings") or [])
    n_o = len(report.get("options") or [])
    counts = f"{n_f} finding{'s' * (n_f != 1)}, {n_o} option{'s' * (n_o != 1)}"
    chosen = _recommended(report)
    rec = (f"recommends {chosen['key']}: {chosen['title']}" if chosen
           else "no option recommended")
    return f"{fo.get('id')} report ready — {counts}; {rec}"
