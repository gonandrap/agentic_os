"""One narrow seat before a GAP settles: is a gap named with no entry? (io-38a4354f, finding coverage-unchecked)"""

from __future__ import annotations

from typing import Any

from . import agent_usage, paths, seats

SEAT = "coverage"
#: Not haiku: measured on stored verdicts, it missed a true aside three runs of three (PR body)
MODEL = "sonnet"

#: Runs inside the investigator's Bash call, whose default limit is 120s
TIMEOUT = 90

SYSTEM = """You check one investigation verdict for one thing. Nothing else.

You get the order's description, then the verdict: root cause, evidence, the one proposed fix
and its list of other gaps.

THE QUESTION: does the text NAME a second defect, or a question the order asked, that has no
entry in <proposed_fix> or <other_gaps>, is not answered in the root cause, and is not
explicitly dismissed as not needing work?

Read every sentence of the root cause to its end: a second defect usually sits in a last
sentence or a parenthesis, in words like these:
- "Unrelated and not filed from here: the search command printed 744KB for one query."
- "(one with a negative balance, which is itself an accounting bug)"
- "a second, separate defect that I cannot file from here"
- "Separately seen while investigating: the review page shows the wrong reason."
- "no order exists to diagnose the meter itself"
A defect named only in such an aside is uncovered, however minor it looks. "Not filed" and
"cannot file" are not dismissals: they are the reason it needs an entry.

NOT a second defect: a step in the causal chain the proposed fix removes, a symptom of the
root cause, a past verdict or a mechanism cited as background. Never add a gap you thought of
yourself, and never judge the quality of the diagnosis or of the fix.

REPLY WITH ONE JSON OBJECT AND NOTHING ELSE:
{"uncovered": [{"gap": "<short name>", "quote": "<the words naming it, copied exactly>"}]}"""


class SeatUnreachable(RuntimeError):
    """No usable answer came back, so nothing was judged (kn-40db1828)"""


def _normal(text: str) -> str:
    return " ".join(text.split()).casefold()


def _fields(item: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in item.items())


def prompt(description: str, verdict: dict[str, Any]) -> str:
    """Plain text, so a quote can be checked against exactly what the seat was shown"""
    evidence = "\n".join(f"- {e.get('quote', '')} ({e.get('source', '')})"
                         for e in verdict.get("evidence") or [])
    other = "\n\n".join(_fields(g) for g in verdict.get("other_gaps") or []) or "(none)"
    return (f"<order_description>\n{description}\n</order_description>\n\n"
            f"<root_cause>\n{verdict.get('root_cause', '')}\n</root_cause>\n\n"
            f"<evidence>\n{evidence}\n</evidence>\n\n"
            f"<proposed_fix>\n{_fields(verdict.get('proposed_fix') or {})}\n</proposed_fix>\n\n"
            f"<other_gaps>\n{other}\n</other_gaps>")


def uncovered(description: str, verdict: dict[str, Any], *, project: str,
              wo_id: str) -> list[dict[str, str]]:
    """The gaps named with no entry, or `SeatUnreachable`"""
    text = prompt(description, verdict)
    opinion, = seats.run_blind({SEAT: (SYSTEM, text)}, models={SEAT: MODEL}, timeout=TIMEOUT,
                               cwd=paths.ensure_home(), tools="", kind=agent_usage.COVERAGE)
    data = opinion.data
    if opinion.usage is not None:
        agent_usage.record(agent_usage.COVERAGE, usage=opinion.usage, project=project,
                           wo_id=wo_id, model=MODEL, ok=isinstance(data, dict))
    items = data.get("uncovered") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SeatUnreachable(f"the gap-coverage seat was UNREACHABLE or gave no usable "
                              f"answer ({opinion.status}: {opinion.raw[:200]})")
    # Blocks only on a gap the text names (kn-fd51ff0b): a quote not in it is the seat's own idea
    shown = _normal(text)
    return [{"gap": str(item.get("gap") or "").strip(), "quote": str(item["quote"]).strip()}
            for item in items
            if isinstance(item, dict) and str(item.get("quote") or "").strip()
            and _normal(str(item["quote"])) in shown]
