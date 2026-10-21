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
            "evidence": parse_evidence(key, entry.get("evidence"), problems),
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
    return []


def parse_evidence(key: str, raw: Any, problems: list[str]) -> list[dict[str, str]]:
    """The quoted record behind a finding (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
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
        status = finding.get("status") or "pending"
        lines.append(f"- [{finding.get('key', '')}] {status}")
        lines += render_finding(finding,
                                (filed_orders or {}).get(finding.get("key", "")))
    return lines


def review_headline(io: dict[str, Any], report: dict[str, Any]) -> str:
    """The counts-first line the attention reason uses (§6.2)"""
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
