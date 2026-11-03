"""Neo decides quarantined backlog proposals (docs/superpowers/specs/2026-11-03-backlog-operability.md §5)"""

from __future__ import annotations

import ast
import logging
import re
from typing import Any, Mapping

from . import structured
from .central_store import BACKLOG_SUGGESTED_KINDS, CentralStore

log = logging.getLogger(__name__)

DECISIONS = ("accept", "reject", "duplicate")
DESCRIPTION_CHARS = 1500
_BL_RE = re.compile(r"\bbl-[0-9a-f]+\b")

QUESTION = """BACKLOG PROPOSAL — the steward quarantined this proposed backlog item; no worker is waiting.
Item: {id} (project {project})
Title: {title}
Type: {item_type}; severity: {severity}; suggested kind: {suggested_kind}
Steward rule: {rule} — {note}
Items the rule names:
{named}
Origins: {origins}
Description:
{description}

Decide it: accept (it becomes an open backlog item), reject (dropped), or duplicate (dropped, its origins folded into the item it restates).
Your `answer` MUST be a string holding exactly one JSON object:{{"decision": "accept"|"reject"|"duplicate", "duplicate_of": "bl-…" (required for duplicate), "kind": "wo"|"fo" (optional), "severity": one of {severities} (optional), "depends_on": ["bl-…"] (optional)}}"""

REVIEW_HINT = ("jarvis backlog review {id} --accept|--reject|--duplicate-of <bl-id> "
               "[--kind wo|fo] [--feedback \"why\"]")


def build_question(central: CentralStore, item: Mapping[str, Any]) -> str:
    from . import ops
    from .steward import SEVERITIES

    named = []
    for bl in dict.fromkeys(_BL_RE.findall(item.get("review_note") or "")):
        other = central.get_backlog(bl)
        named.append(f"- {bl}: \"{other['title']}\" ({other['status']})" if other
                     else f"- {bl}: missing")
    origins = [label for _href, label in map(ops.origin_url, item.get("origin_refs") or [])]
    return QUESTION.format(
        id=item["id"], project=item["project"], title=item["title"],
        item_type=item.get("item_type"), severity=item.get("severity"),
        suggested_kind=item.get("suggested_kind"), rule=item.get("review_rule") or "",
        note=item.get("review_note") or "", named="\n".join(named) or "- none",
        origins="; ".join(origins) or "none",
        description=(item.get("description") or "")[:DESCRIPTION_CHARS] or "(none)",
        severities="|".join(SEVERITIES))


def ask_quarantined(central: CentralStore) -> list[int]:
    """One Neo question per quarantined item that has none yet (§5.1)"""
    from .neo_store import NeoStore

    asked: list[int] = []
    pending = [i for i in central.list_backlog(status="quarantined")
               if not i.get("neo_question_id")]
    if not pending:
        return asked
    neo = NeoStore()
    try:
        for item in pending:
            try:
                q = neo.ask(item["project"], "", build_question(central, item), kind="backlog")
            except Exception:  # noqa: BLE001 — the item stays unasked; next tick retries it
                log.exception("asking Neo about backlog item %s failed", item["id"])
                continue
            if central.set_backlog_question(item["id"], int(q["id"])):
                asked.append(int(q["id"]))
            else:
                # Decided or asked elsewhere meanwhile: this question has no subject
                neo.supersede(int(q["id"]), "MOOT", f"backlog item {item['id']} moved first")
    finally:
        neo.close()
    return asked


def parse_answer(answer: str) -> dict[str, Any] | None:
    """The decision object in `answer`; a verdict whose answer was an object arrives as its repr"""
    data = structured.parse_json_object(answer)
    if data is None and answer.lstrip().startswith("{"):
        try:
            data = ast.literal_eval(answer.strip())
        except (ValueError, SyntaxError):
            return None
    return data if isinstance(data, dict) else None


def check_decision(data: Any) -> dict[str, Any] | None:
    """A validated decision object, or None — never a default (kn-40db1828)"""
    from .steward import SEVERITIES

    if not isinstance(data, dict) or data.get("decision") not in DECISIONS:
        return None
    dup = data.get("duplicate_of")
    kind, severity, deps = data.get("kind"), data.get("severity"), data.get("depends_on")
    if data["decision"] == "duplicate" and not (isinstance(dup, str) and dup.startswith("bl-")):
        return None
    if kind is not None and kind not in BACKLOG_SUGGESTED_KINDS:
        return None
    if severity is not None and severity not in SEVERITIES:
        return None
    if deps is not None and not (isinstance(deps, list)
                                 and all(isinstance(d, str) for d in deps)):
        return None
    return {"decision": data["decision"],
            "duplicate_of": dup if data["decision"] == "duplicate" else None,
            "kind": kind, "severity": severity, "depends_on": deps}


def apply_decision(central: CentralStore, item: Mapping[str, Any], decision: dict[str, Any],
                   *, rule: str, note: str) -> dict[str, Any]:
    """Write one decision through `review_backlog`; raises KeyError/ValueError before any write"""
    from . import steward

    if decision["decision"] == "duplicate":
        return steward.fold_duplicate(central, item, decision["duplicate_of"],
                                      rule=rule, note=note)
    return central.review_backlog(
        item["id"], "open" if decision["decision"] == "accept" else "dropped",
        rule=rule, note=note, suggested_kind=decision.get("kind"),
        severity=decision.get("severity"), depends_on=decision.get("depends_on"))


def record_neo_verdict(central: CentralStore, neo: Any, q: dict[str, Any],
                       verdict: dict[str, Any], question_url: str = "") -> str:
    """Neo ruled on a `backlog` question: apply it, or leave the item quarantined (§5.2)"""
    item = central.backlog_for_question(int(q["id"]))
    if not item or item["status"] != "quarantined":
        log.info("backlog verdict for neo question %s has no quarantined item", q["id"])
        return "moot"
    hint = REVIEW_HINT.format(id=item["id"])
    reason = (verdict.get("reason") or "").strip()
    if verdict.get("escalate"):
        central.add_inbox(
            project=item["project"], level="warning",
            title=f"Neo escalated a quarantined backlog proposal: {item['title'][:120]}",
            body=f"{item['id']} — {item.get('review_note') or ''}\nWhy: {reason[:300]}\n"
                 + (f"{question_url}\n" if question_url else "") + f"Decide it: {hint}")
        return "escalated"
    decision = check_decision(parse_answer(verdict.get("answer") or ""))
    if decision is not None:
        try:
            apply_decision(central, item, decision, rule="neo",
                           note=reason[:300] or f"Neo: {decision['decision']}")
            return decision["decision"]
        except (KeyError, ValueError) as e:
            log.warning("backlog verdict for %s could not be applied: %s", item["id"], e)
            why = f"verdict could not be applied: {e}"
    else:
        why = "answer was not a valid backlog decision object"
    return _not_a_verdict(central, neo, q, item, why, question_url)


def _not_a_verdict(central: CentralStore, neo: Any, q: dict[str, Any],
                   item: Mapping[str, Any], why: str, question_url: str) -> str:
    """Re-ask it; once attempts are spent the user decides (kn-40db1828)"""
    if neo.requeue_unreadable(int(q["id"]), f"backlog: {why}"):
        return "requeued"
    neo.mark(int(q["id"]), "failed", reason=f"backlog: {why} — nobody has judged this",
             cause="unparseable-reply")
    central.add_inbox(
        project=item["project"], level="warning",
        title=f"Neo gave no usable decision on backlog proposal {item['id']}",
        body=f"NOBODY HAS JUDGED THIS: {why}. The item stays quarantined.\n"
             + (f"{question_url}\n" if question_url else "")
             + f"Decide it: {REVIEW_HINT.format(id=item['id'])}")
    return "failed"


def stale_question(central: CentralStore, q: Mapping[str, Any]) -> tuple[str, str] | None:
    """(answer, why) once the item this question decides is no longer quarantined"""
    item = central.backlog_for_question(int(q["id"]))
    if item is None:
        return "MOOT", "no backlog item points at it any more"
    if item["status"] != "quarantined":
        return item["status"].upper(), f"backlog item {item['id']} is already {item['status']}"
    return None
