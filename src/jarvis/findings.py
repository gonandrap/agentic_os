"""The findings report an analyst submits, and the structural checks it must survive.

An improvement order's analyst finishes by handing back a report: what is going wrong,
why, and what to do about it (`jarvis io report <io-id> --from-file report.json`). This
module owns that document's shape and validates it *before* anything is stored or any
of the user's attention is spent.

Why the validation is mechanical and lives here rather than in a reviewer's judgement:
the same argument that makes `plans.py` load-bearing. This report is a structured
document the user is about to ACT on — finding by finding, each decision spending
worker sessions — so the check on it has to be more trustworthy than the thing it
checks. Pure Python over the submitted document, no LLM, no database, no disk.

The rejections, and what each is guarding:

* **No summary, no findings, a missing/malformed/duplicated `key`.** `key` is how
  `jarvis io review` and the dashboard address one finding; without it a decision has
  nothing to land on.
* **The findings cap.** The ATTENTION cap, the same shape and the same argument as
  `plans.CHILD_CAP`: a report the user will not read changes nothing. Over it the
  analyst must say why, in the submission, or it does not validate.
* **Prose fields under the floor.** `symptom`, `root_cause`, `why_insufficient` and
  `recommendation` each carry an argument. Short prose is the failure mode for
  `why_insufficient` specifically — "it does not fix the root cause" is a restatement,
  not an argument.
* **Evidence that cannot be checked.** A finding with no source or no quote asks the
  user to trust the analyst's session, which nobody reads.
* **Proposed orders that would not brief anyone.** A proposed order becomes a real work
  order whose worker sees its description and nothing else, so the description runs
  through `plans._description_problems` — reused, never re-implemented, and its trap is
  inherited with it: ORDINALS ARE NOT OUTWARD REFERENCES.

§4 of docs/superpowers/specs/2026-09-23-improvement-orders.md.
"""

from __future__ import annotations

from typing import Any

from . import plans

#: How many findings a report may carry before the analyst owes an explanation. This is
#: the ATTENTION cap, not a storage limit: the user reads every finding and decides it,
#: and a report they will not read changes nothing. Same shape and same argument as
#: `plans.CHILD_CAP`.
MAX_FINDINGS = 6

#: Shortest a prose field can be and still be an argument rather than a label. Aimed at
#: `why_insufficient` above all — "it does not fix the root cause" is a restatement of
#: the question. Deliberately a floor against obvious under-specification, not a quality
#: bar; judging whether a real paragraph is GOOD is the user's job at review time.
MIN_FIELD_CHARS = 40

#: Shortest quote that can still be found again in the thing it was quoted from. Under
#: this it is a fragment the user cannot grep for, which defeats the point of quoting.
MIN_QUOTE_CHARS = 20

#: Finding keys. Reused from `plans` rather than declared a second time: a finding key
#: plays exactly the role a plan child key plays — a short, stable, report-local handle
#: the review verb addresses — and two regexes for one rule drift.
KEY_RE = plans.KEY_RE

#: What a proposed order may become. Nothing is defaulted: `type` decides whether the
#: user gets one work order or a whole feature order out of this finding, and picking
#: that on the analyst's behalf is picking what the analyst meant.
ORDER_TYPES = ("work", "feature")


class FindingsError(ValueError):
    """A submitted report that cannot be accepted. Message names every problem found.

    One exception carrying all of them, not the first: an analyst that has to re-submit
    per problem burns a session round trip per line of the error it could have had at
    once. Mirrors `plans.PlanError`.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_report(raw: Any) -> dict[str, Any]:
    """Validate a submitted findings report and return it normalised.

    Raises `FindingsError` carrying EVERY problem, never the first one found. The
    returned document is what gets stored on the improvement order and what the review
    verb and the dashboard read, so normalisation happens here and exactly once: strings
    stripped, absent optional fields filled in, unknown keys dropped. Nothing downstream
    re-derives any of it.

    No `status` or decision field is written here. `ops.submit_findings` sets every
    finding `status='pending'` when it stores, and the review verb writes the decisions;
    keeping that out of the validator is what lets it stay pure.
    """
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise FindingsError(
            [f"a findings report must be a JSON object, got {type(raw).__name__}"])

    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list) or not findings_raw:
        raise FindingsError(["a findings report must carry a non-empty `findings` list"])

    summary = str(raw.get("summary") or "").strip()
    if not summary:
        problems.append(
            "`summary` is required — one line saying what is going wrong across all "
            "findings. It is what the attention item and the dashboard page open on."
        )

    findings: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for i, entry in enumerate(findings_raw):
        where = f"finding {i + 1}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: must be an object, got {type(entry).__name__}")
            continue
        key = str(entry.get("key") or "").strip()
        if not KEY_RE.match(key):
            problems.append(
                f"{where}: `key` must be a short lowercase slug "
                f"([a-z0-9][a-z0-9_-]*), got {key!r}"
            )
            # Substitute a placeholder so every later message about this entry still
            # locates it, exactly as `parse_plan` does.
            key = f"?{i}"
        elif key in seen_keys:
            problems.append(f"{where}: duplicate key {key!r}")
        seen_keys.add(key)

        fields: dict[str, str] = {}
        for name in ("symptom", "root_cause", "why_insufficient", "recommendation"):
            value = str(entry.get(name) or "").strip()
            fields[name] = value
            problems += _field_problems(key, name, value)

        findings.append({
            "key": key,
            **fields,
            "evidence": _parse_evidence(key, entry.get("evidence"), problems),
            "proposed_orders": _parse_orders(key, entry.get("proposed_orders"),
                                             problems),
        })

    justification = str(raw.get("justification") or "").strip()
    if len(findings_raw) > MAX_FINDINGS and not justification:
        problems.append(
            f"{len(findings_raw)} findings is over the cap of {MAX_FINDINGS} and the "
            f"report carries no `justification` — say why the user must read more than "
            f"{MAX_FINDINGS} findings to act on this order, or merge the small ones"
        )

    if problems:
        raise FindingsError(problems)
    return {"summary": summary, "justification": justification, "findings": findings}


def _field_problems(key: str, name: str, value: str) -> list[str]:
    """Whether one prose field carries an argument. Missing and too-short are distinct
    messages: they are different mistakes and have different fixes."""
    if not value:
        return [f"finding {key!r}: `{name}` is required"]
    if len(value) < MIN_FIELD_CHARS:
        return [
            f"finding {key!r}: `{name}` is {len(value)} characters, under the "
            f"{MIN_FIELD_CHARS} it takes to make an argument rather than restate the "
            f"heading"
        ]
    return []


def _parse_evidence(key: str, raw: Any, problems: list[str]) -> list[dict[str, str]]:
    """The quoted record behind a finding. Every message locates the finding by key AND
    the entry by its 1-based index, because that is how the analyst finds it again."""
    if not isinstance(raw, list) or not raw:
        problems.append(
            f"finding {key!r}: `evidence` must be a non-empty list — a finding with "
            f"nothing quoted asks the user to trust a session nobody reads"
        )
        return []
    out: list[dict[str, str]] = []
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            problems.append(f"finding {key!r}, evidence {n}: must be an object, got "
                            f"{type(item).__name__}")
            continue
        source = str(item.get("source") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not source:
            problems.append(
                f"finding {key!r}, evidence {n}: `source` is required — name the "
                f"command or file the quote came from, so the user can check it"
            )
        if not quote:
            problems.append(f"finding {key!r}, evidence {n}: `quote` is required")
        elif len(quote) < MIN_QUOTE_CHARS:
            problems.append(
                f"finding {key!r}, evidence {n}: `quote` is {len(quote)} characters, "
                f"under the {MIN_QUOTE_CHARS} it takes to find the line again in its "
                f"source"
            )
        out.append({"source": source, "quote": quote})
    return out


def _parse_orders(key: str, raw: Any, problems: list[str]) -> list[dict[str, str]]:
    """The work this finding proposes, if any.

    An EMPTY list is accepted and never produces a problem: a finding whose
    recommendation is "do nothing, and here is why" is legitimate and valuable, and
    forcing an order out of it is how an analyst is pushed into inventing work (§4.2).
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        problems.append(f"finding {key!r}: `proposed_orders` must be a list")
        return []
    out: list[dict[str, str]] = []
    for n, item in enumerate(raw, start=1):
        where = f"finding {key!r}, order {n}"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be an object, got {type(item).__name__}")
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            problems.append(f"{where}: `title` is required")
        order_type = str(item.get("type") or "").strip()
        if order_type not in ORDER_TYPES:
            problems.append(
                f"{where}: `type` must be one of {', '.join(ORDER_TYPES)}, got "
                f"{order_type!r}"
            )
        project = item.get("project")
        if project is not None and not isinstance(project, str):
            problems.append(f"{where}: `project` must be a string, got "
                            f"{type(project).__name__}")
            project = ""
        # This description becomes a real brief read cold by a stranger — the worker
        # created from it sees the description and never this report — and there is ONE
        # standard for that in this codebase. Reused, not re-implemented, so its trap is
        # inherited with it: ORDINALS ARE NOT OUTWARD REFERENCES.
        description = str(item.get("description") or "").strip()
        problems += plans._description_problems(f"{key}/order {n}", title, description)
        out.append({"type": order_type, "project": str(project or "").strip(),
                    "title": title[:200], "description": description})
    return out


def render_finding(finding: dict[str, Any]) -> list[str]:
    """One finding as lines a human reads: the argument, the record behind it, the work.

    Every quote is attributed to its source on the same line, because a quote whose
    source is a line away is one the reader has to reassemble.
    """
    lines = [
        f"  symptom: {finding.get('symptom', '')}",
        f"  root cause: {finding.get('root_cause', '')}",
        f"  cheap fix, and why it is wrong: {finding.get('why_insufficient', '')}",
        f"  recommendation: {finding.get('recommendation', '')}",
    ]
    evidence = finding.get("evidence") or []
    if evidence:
        lines.append("  evidence:")
        for item in evidence:
            lines.append(f"    - {item.get('quote', '')!r} ({item.get('source', '')})")
    orders = finding.get("proposed_orders") or []
    if not orders:
        # One line rather than an empty section: "do nothing, and here is why" is a real
        # finding, and a blank heading reads like something failed to render.
        lines.append("  no proposed orders")
    else:
        lines.append("  proposed orders:")
        for order in orders:
            project = f" [{order['project']}]" if order.get("project") else ""
            lines.append(f"    - ({order.get('type', '')}){project} "
                         f"{order.get('title', '')}")
            lines.append(f"      {order.get('description', '')}")
    return lines


def render_report(report: dict[str, Any]) -> list[str]:
    """The report as lines a human reads — `jarvis io show`'s view, and the dashboard's.

    One renderer, so the two surfaces cannot drift: two places rendering a report
    separately is how they come to disagree about what was decided. Mirrors
    `plans.render_plan`.
    """
    lines: list[str] = []
    if report.get("summary"):
        lines += [report["summary"], ""]
    if report.get("justification"):
        lines += [f"Justification for "
                  f"{len(report.get('findings') or [])} findings: "
                  f"{report['justification']}", ""]
    for finding in report.get("findings") or []:
        status = finding.get("status") or "pending"
        lines.append(f"- [{finding.get('key', '')}] {status}")
        lines += render_finding(finding)
    return lines


def review_headline(io: dict[str, Any], report: dict[str, Any]) -> str:
    """The counts-first line the attention reason uses (§6.2).

    It names `jarvis io review <io-id>` because an attention item whose instruction
    names no command is one the user has to guess at. Counts before prose: the one thing
    the user needs from the status line is how many decisions are still owed.
    """
    findings = report.get("findings") or []
    accepted = sum(1 for f in findings if (f.get("status") or "pending") == "accepted")
    rejected = sum(1 for f in findings if (f.get("status") or "pending") == "rejected")
    pending = len(findings) - accepted - rejected
    parts = []
    if accepted:
        parts.append(f"{accepted} accepted")
    if rejected:
        parts.append(f"{rejected} rejected")
    parts.append(f"{pending} awaiting you")
    return (f"{len(findings)} findings on {io.get('id')}: {', '.join(parts)} — "
            f"jarvis io review {io.get('id')}")
