"""The findings report an analyst submits, and the structural checks it must survive (docs/superpowers/specs/2026-09-23-improvement-orders.md)"""

from __future__ import annotations

from typing import Any

from . import plans

#: How many findings a report may carry before the analyst owes an explanation
MAX_FINDINGS = 6

#: Shortest a prose field can be and still be an argument rather than a label
MIN_FIELD_CHARS = 40

#: Shortest quote that can still be found again in the thing it was quoted from
MIN_QUOTE_CHARS = 20

#: Longest each prose field may be: the user reads every finding
MAX_FIELD_CHARS = {
    "symptom": 300,
    "root_cause": 600,
    "why_insufficient": 300,
    "recommendation": 400,
}

#: Longest the report's one-line summary may be
MAX_SUMMARY_CHARS = 160

#: How many quotes one finding may carry; the strongest ones, not the whole session log
MAX_EVIDENCE = 4

#: Longest one quote may be: the line that proves the point, not the screen around it
MAX_QUOTE_CHARS = 300

#: Finding keys
KEY_RE = plans.KEY_RE

#: What a proposed order may become
ORDER_TYPES = ("work", "feature")


class FindingsError(ValueError):
    """A submitted report that cannot be accepted"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_report(raw: Any) -> dict[str, Any]:
    """Validate a submitted findings report and return it normalised"""
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
    elif len(summary) > MAX_SUMMARY_CHARS:
        problems.append(
            f"`summary` is {len(summary)} characters, over the {MAX_SUMMARY_CHARS} of "
            f"one line — say what is going wrong, and leave the why to the findings"
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
            # Substitute a placeholder so every later message about this entry still locates it
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
            "evidence": parse_evidence(key, entry.get("evidence"), problems,
                                       max_items=MAX_EVIDENCE,
                                       max_quote=MAX_QUOTE_CHARS),
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
    """Whether one prose field carries an argument"""
    if not value:
        return [f"finding {key!r}: `{name}` is required"]
    if len(value) < MIN_FIELD_CHARS:
        return [
            f"finding {key!r}: `{name}` is {len(value)} characters, under the "
            f"{MIN_FIELD_CHARS} it takes to make an argument rather than restate the "
            f"heading"
        ]
    cap = MAX_FIELD_CHARS[name]
    if len(value) > cap:
        return [
            f"finding {key!r}: `{name}` is {len(value)} characters, over the {cap} the "
            f"user will read — keep the mechanism, the file:line and the number that "
            f"prove it, and cut the narrative of how you found it"
        ]
    return []


def parse_evidence(key: str, raw: Any, problems: list[str], *,
                   max_items: int | None = None,
                   max_quote: int | None = None) -> list[dict[str, str]]:
    """The quoted record behind a finding (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
    if not isinstance(raw, list) or not raw:
        problems.append(
            f"finding {key!r}: `evidence` must be a non-empty list — a finding with "
            f"nothing quoted asks the user to trust a session nobody reads"
        )
        return []
    if max_items is not None and len(raw) > max_items:
        problems.append(
            f"finding {key!r}: {len(raw)} evidence quotes, over the {max_items} a "
            f"finding may carry — keep the ones that prove the root cause"
        )
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
        elif max_quote is not None and len(quote) > max_quote:
            problems.append(
                f"finding {key!r}, evidence {n}: `quote` is {len(quote)} characters, "
                f"over the {max_quote} a quote may be — quote the one line that proves "
                f"it, not the screen around it"
            )
        out.append({"source": source, "quote": quote})
    return out


def _parse_orders(key: str, raw: Any, problems: list[str]) -> list[dict[str, str]]:
    """The work this finding proposes, if any"""
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
        # This description becomes a real brief read cold by a stranger
        description = str(item.get("description") or "").strip()
        problems += plans._description_problems(f"{key}/order {n}", title, description)
        out.append({"type": order_type, "project": str(project or "").strip(),
                    "title": title[:200], "description": description})
    return out


def knowledge_text(finding: dict[str, Any]) -> str:
    """The knowledge-base entry an accepted finding becomes (§5.4)"""
    root = " ".join(str(finding.get("root_cause") or "").split())
    lines = [root, ""]
    lines += [f"Symptom: {finding.get('symptom', '')}", ""]
    lines += [f"The cheap fix, and why it is insufficient: "
              f"{finding.get('why_insufficient', '')}", ""]
    lines += [f"Recommendation: {finding.get('recommendation', '')}"]
    evidence = finding.get("evidence") or []
    if evidence:
        lines += ["", "Evidence:"]
        # Attributed on the quote's OWN line: a source a line away is one the reader has to reassemble
        lines += [f"  - {item.get('quote', '')!r} ({item.get('source', '')})"
                  for item in evidence]
    orders = finding.get("proposed_orders") or []
    if orders:
        lines += ["", "Proposed orders:"]
        lines += [f"  - ({o.get('type', '')}) {o.get('title', '')}" for o in orders]
    return "\n".join(lines)


def rejection_learning(io: dict[str, Any], finding: dict[str, Any], feedback: str,
                       decided_by: str = "user") -> str:
    """What Neo is taught when the user refuses a finding (§5.5)"""
    root = " ".join(str(finding.get("root_cause") or "").split())
    return (f"On improvement order \"{str(io.get('title', ''))[:120]}\" an analyst found: "
            f"\"{root[:300]}\". The user rejected that ({decided_by}): {feedback}")


def render_finding(finding: dict[str, Any],
                   filed: list[dict[str, Any]] | None = None) -> list[str]:
    """One finding as lines a human reads: the argument, the record behind it, the work"""
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
        # One line rather than an empty section: "do nothing
        lines.append("  no proposed orders")
    else:
        lines.append("  proposed orders:")
        for order in orders:
            project = f" [{order['project']}]" if order.get("project") else ""
            lines.append(f"    - ({order.get('type', '')}){project} "
                         f"{order.get('title', '')}")
            lines.append(f"      {order.get('description', '')}")
    if filed:
        lines.append("  filed orders:")
        for order in filed:
            project = f" [{order['project']}]" if order.get("project") else ""
            lines.append(f"    - {order.get('id', '')} ({order.get('type', '')})"
                         f"{project} {order.get('title', '')} — "
                         f"{order.get('status', '')}")
    return lines


def render_report(report: dict[str, Any],
                  filed_orders: dict[str, list[dict[str, Any]]] | None = None
                  ) -> list[str]:
    """The report as lines a human reads — `jarvis io show`'s view, and the dashboard's"""
    lines: list[str] = []
    if report.get("summary"):
        lines += [report["summary"], ""]
    if report.get("justification"):
        lines += [f"Justification for "
                  f"{len(report.get('findings') or [])} findings: "
                  f"{report['justification']}", ""]
    for finding in report.get("findings") or []:
        lines.append(f"- [{finding.get('key', '')}] {status_label(finding)}")
        lines += [f"  {line}" for line in decision_lines(finding)]
        lines += render_finding(finding,
                                (filed_orders or {}).get(finding.get("key", "")))
    return lines


def clip(text: str, limit: int) -> str:
    """One line of at most `limit` characters, cut at a word"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rsplit(" ", 1)[0] + "…"


def render_digest(report: dict[str, Any],
                  filed_orders: dict[str, list[dict[str, Any]]] | None = None,
                  limit: int = 200) -> list[str]:
    """The report one line per finding plus its work"""
    lines: list[str] = []
    if report.get("summary"):
        lines += [report["summary"], ""]
    for finding in report.get("findings") or []:
        key = finding.get("key", "")
        lines.append(f"- [{key}] {status_label(finding)}: "
                     f"{clip(finding.get('root_cause', ''), limit)}")
        lines += [f"    {clip(line, limit)}" for line in decision_lines(finding)]
        lines.append(f"    do: {clip(finding.get('recommendation', ''), limit)}")
        filed = (filed_orders or {}).get(key) or []
        if filed:
            for order in filed:
                lines.append(f"    filed: {order.get('id', '')} "
                             f"{order.get('title', '')} — {order.get('status', '')}")
        else:
            for order in finding.get("proposed_orders") or []:
                lines.append(f"    order ({order.get('type', '')}): "
                             f"{order.get('title', '')}")
    return lines


def status_label(finding: dict[str, Any]) -> str:
    """`pending` says who it waits on: Neo deciding is not the user being owed a decision"""
    status = finding.get("status") or "pending"
    if status == "pending":
        return "pending, with Neo" if with_neo(finding) else "pending"
    by = finding.get("decided_by") or ""
    return f"{status} by {by}" if by and by != "user" else status


def decision_lines(finding: dict[str, Any]) -> list[str]:
    """What Neo read and what the steward held, when either touched this finding"""
    lines = []
    neo = finding.get("neo_verdict") or {}
    if neo:
        reading = "escalated" if neo.get("escalate") else neo.get("verdict", "?")
        severity = f", rated {neo['severity']}" if neo.get("severity") else ""
        lines.append(f"Neo ({reading}{severity}): {neo.get('reason', '')}")
    held = finding.get("steward") or {}
    if held:
        lines.append(f"steward decision #{held.get('decision_id', '?')} "
                     f"({held.get('outcome', '?')}, {held.get('rule', '?')}): "
                     f"{held.get('reason', '')}")
    return lines


def with_neo(finding: dict[str, Any]) -> bool:
    """Is this pending finding Neo's to decide right now, rather than the user's?"""
    return ((finding.get("status") or "pending") == "pending"
            and bool(finding.get("neo_question_id")) and not finding.get("neo_verdict"))


def never_offered(finding: dict[str, Any]) -> bool:
    """A pending finding Neo was never asked about: reported before Neo decided findings"""
    return ((finding.get("status") or "pending") == "pending"
            and not finding.get("neo_question_id") and not finding.get("neo_verdict")
            and not finding.get("neo_unasked"))


def review_headline(io: dict[str, Any], report: dict[str, Any]) -> str:
    """The counts-first line the attention reason uses (§6.2)"""
    findings = report.get("findings") or []
    accepted = sum(1 for f in findings if (f.get("status") or "pending") == "accepted")
    rejected = sum(1 for f in findings if (f.get("status") or "pending") == "rejected")
    neo = sum(1 for f in findings if with_neo(f))
    pending = len(findings) - accepted - rejected - neo
    parts = []
    if accepted:
        parts.append(f"{accepted} accepted")
    if rejected:
        parts.append(f"{rejected} rejected")
    if neo:
        parts.append(f"{neo} with Neo")
    if pending or not neo:
        parts.append(f"{pending} awaiting you")
    return (f"{len(findings)} findings on {io.get('id')}: {', '.join(parts)} — "
            f"jarvis io review {io.get('id')}")


# -- Neo decides findings for the user (§5.5, as amended 2026-10-22)

FINDINGS_REVIEWER_PERSONA = """You are Neo, deciding ONE FINDING of an improvement order inside
the Jarvis agentic OS, on the user's behalf.

An analyst read the OS's records about something that went wrong and reported a
diagnosis: a symptom, its root cause, quoted evidence, why the cheap fix is not enough,
a recommendation, and the orders it proposes filing. You decide whether the user would
accept this finding. Accepting it writes the root cause into the knowledge base every
worker reads, and offers its proposed orders to the steward — the OS's deterministic
budget keeper, which decides from your severity rating, the weekly cap and recurrence
whether the orders are filed now, held, or dropped. So you judge the DIAGNOSIS and RATE
it. What the OS can afford is not yours to decide.

ACCEPT when all of these hold:
- **The root cause is a mechanism, not a restatement.** It explains WHY the symptom
  happens in terms of code, configuration or process, and the quoted evidence supports it.
- **The cheap fix really is insufficient.** The argument against it is concrete, or the
  finding says plainly why the cheap fix is nevertheless right.
- **The proposed orders follow from the recommendation.** They fix the cause the finding
  names and nothing it did not argue for. A finding with no proposed orders is
  legitimate when "do nothing" is argued.

REJECT when the finding is wrong or not worth the user's money: a root cause the evidence
does not support, a speculative mechanism, a recommendation that is the cheap patch
dressed up, proposed work that drifts from the diagnosis, or a defect so small nobody
would notice it. Your reason is kept on the finding, so say what is wrong.

ESCALATE to the user, rather than deciding, when:
- A proposed order would delete data, publish, spend money outside the OS, or change
  production systems or credentials.
- The finding contradicts a standing learning below, or a learning says the user wants
  to decide this kind of thing themselves.
- Accepting it is really choosing between two directions the user has not chosen between.

RATE the defect the finding diagnoses, honestly, on this scale:
  blocker / critical: an OS function is broken now: work is lost, stuck or wrong
  major: wrong behaviour a user or operator would notice
  medium: a real defect, but bounded or unlikely
  minor: hygiene with no behavioural change
  cosmetic: naming, typos, style
An inflated rating is one the steward spends the user's weekly budget on.

Output STRICT JSON, nothing else:
  {"escalate": false, "verdict": "accept", "severity": "<scale word>", "reason": "<one line>"}
  or
  {"escalate": false, "verdict": "reject", "severity": "<scale word>", "reason": "<one line: what is wrong>"}
  or
  {"escalate": true, "verdict": "", "severity": "<scale word>", "reason": "<one line why the user must decide>"}
An escalation may carry one optional label, which is grouped and reported:
  "cause": "<high-stakes | no-learning-applies | conflicting-authority | ambiguous-intent>\""""

#: How much of one proposed order's brief Neo is shown: enough to see it follows, not the whole brief
QUESTION_ORDER_CHARS = 600


def build_finding_question(io: dict[str, Any], finding: dict[str, Any]) -> str:
    """ONE finding, atomic: its whole argument and the work it proposes"""
    lines = [f"Decide finding {finding.get('key', '')!r} of improvement order "
             f"{io.get('id', '')}. Would the user accept it?", "",
             f"symptom: {finding.get('symptom', '')}",
             f"root cause: {finding.get('root_cause', '')}",
             f"cheap fix, and why it is insufficient: "
             f"{finding.get('why_insufficient', '')}",
             f"recommendation: {finding.get('recommendation', '')}"]
    evidence = finding.get("evidence") or []
    if evidence:
        lines.append("evidence:")
        lines += [f"  - {item.get('quote', '')!r} ({item.get('source', '')})"
                  for item in evidence]
    orders = finding.get("proposed_orders") or []
    if not orders:
        lines.append("proposed orders: none — the finding argues nothing should be filed")
    else:
        lines.append("proposed orders:")
        for order in orders:
            project = f" [{order['project']}]" if order.get("project") else ""
            lines.append(f"  - ({order.get('type', '')}){project} {order.get('title', '')}")
            lines.append(f"    brief: "
                         f"{clip(order.get('description', ''), QUESTION_ORDER_CHARS)}")
    return "\n".join(lines)


def build_finding_context(io: dict[str, Any], report: dict[str, Any]) -> str:
    """What the improvement order was opened about, and the report's one line"""
    return "\n".join([
        f"{io.get('id', '')}: {io.get('title', '')}",
        f"Opened on this observation: {clip(io.get('description', ''), 1500)}",
        f"The analyst's summary across all findings: {report.get('summary', '')}",
    ])
