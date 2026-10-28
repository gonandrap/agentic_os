"""The verdict an investigator submits, and the structural checks it must survive (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from . import findings, gaps


if TYPE_CHECKING:  # pragma: no cover
    from .project_store import ProjectStore

#: The four decisions an investigation can reach about its subject
CLASSIFICATIONS = ("GAP", "WAITING_ON_USER", "TRANSIENT", "ALREADY_TRACKED")

#: Shared with `findings`, never re-declared: two floors for one rule drift
MIN_QUOTE_CHARS = findings.MIN_QUOTE_CHARS

#: Shortest a prose field can be and still be a diagnosis rather than a label
MIN_FIELD_CHARS = findings.MIN_FIELD_CHARS

#: How big the whole submitted document may be (#797)
MAX_VERDICT_CHARS = 20000

#: Required beyond the common four, per classification
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "GAP": ("proposed_fix",),
    "WAITING_ON_USER": ("user_owes",),
    "TRANSIENT": ("unsticks",),
    "ALREADY_TRACKED": ("duplicate_of",),
}

#: Forbidden per classification — the other half of the same rule
FORBIDDEN_FIELDS: dict[str, tuple[str, ...]] = {
    "GAP": ("user_owes", "unsticks", "duplicate_of"),
    "WAITING_ON_USER": ("proposed_fix", "unsticks"),
    "TRANSIENT": ("proposed_fix", "user_owes"),
    "ALREADY_TRACKED": ("proposed_fix",),
}

#: Every field of a `proposed_fix`
PROPOSED_FIX_FIELDS = ("title", "description", "expected", "actual", "priority",
                       "detector", "remedy")

#: What `duplicate_of` may name: a tracker issue (`#n` or a URL) or an order id
_DUPLICATE_RE = re.compile(r"^(?:#\d+|https?://\S+|(?:wo|fo|io|inv)-[0-9a-f]+)$")


class VerdictError(ValueError):
    """A submitted verdict that cannot be accepted"""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def parse_verdict(raw: Any, subject: str = "") -> dict[str, Any]:
    """Validate a submitted verdict and return it normalised"""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise VerdictError(
            [f"a verdict must be a JSON object, got {type(raw).__name__}"])

    size = len(json.dumps(raw, default=str))
    if size > MAX_VERDICT_CHARS:
        raise VerdictError([
            f"this verdict is {size} characters, over the {MAX_VERDICT_CHARS} cap. Never "
            f"paste a diff, a transcript or a log into a verdict: cite the command and "
            f"quote the decisive line."
        ])

    got_subject = str(raw.get("subject") or "").strip()
    if not got_subject:
        problems.append("`subject` is required — the id this verdict is about")
    elif subject and got_subject != subject:
        problems.append(
            f"`subject` is {got_subject!r}, but this investigation was opened on "
            f"{subject!r}. A verdict about something else is not a verdict about this "
            f"order."
        )

    classification = str(raw.get("classification") or "").strip()
    if classification not in CLASSIFICATIONS:
        problems.append(
            f"`classification` must be one of {', '.join(CLASSIFICATIONS)}, got "
            f"{classification!r}"
        )

    root_cause = str(raw.get("root_cause") or "").strip()
    if not root_cause:
        problems.append("`root_cause` is required — one paragraph, in mechanism terms")
    elif len(root_cause) < MIN_FIELD_CHARS:
        problems.append(
            f"`root_cause` is {len(root_cause)} characters, under the {MIN_FIELD_CHARS} "
            f"it takes to state a mechanism rather than restate the symptom"
        )

    gap_class = _gap_class(raw, problems)

    evidence = findings.parse_evidence(got_subject or "?", raw.get("evidence"), problems)

    out: dict[str, Any] = {
        "subject": got_subject,
        "classification": classification,
        "gap_class": gap_class,
        "root_cause": root_cause,
        "evidence": evidence,
    }
    if classification in CLASSIFICATIONS:
        out.update(_payload(classification, raw, problems))

    if problems:
        raise VerdictError(problems)
    return out


def _gap_class(raw: dict[str, Any], problems: list[str]) -> str:
    """The class slug, required on ALL FOUR classifications — Appendix A.3"""
    raw_value = raw.get("gap_class")
    if not str(raw_value or "").strip():
        problems.append(
            "`gap_class` is required on every classification — the slug naming the "
            "MECHANISM, so two investigations of one mechanism on different subjects are "
            "comparable. Reuse one of "
            f"{', '.join(gaps.SHIPPED_GAP_CLASSES)} when the mechanism matches, or coin "
            f"a new one of two to five lowercase hyphenated words")
        return ""
    try:
        return gaps.checked_slug(str(raw_value))
    except gaps.GapClassError as exc:
        problems.append(f"`gap_class`: {exc}")
        return ""


def _payload(classification: str, raw: dict[str, Any],
             problems: list[str]) -> dict[str, Any]:
    """The classification-dependent half, required and forbidden together"""
    out: dict[str, Any] = {}
    for name in FORBIDDEN_FIELDS[classification]:
        if raw.get(name):
            problems.append(
                f"a {classification} verdict must not carry `{name}` — "
                f"{_why_forbidden(classification, name)}"
            )
    for name in REQUIRED_FIELDS[classification]:
        value = raw.get(name)
        if name == "proposed_fix":
            out["proposed_fix"] = _parse_fix(value, problems)
        elif name == "unsticks":
            out["unsticks"] = _parse_unsticks(value, problems)
        elif name == "duplicate_of":
            ref = str(value or "").strip()
            if not _DUPLICATE_RE.match(ref):
                problems.append(
                    f"`duplicate_of` must name a tracker issue (`#790` or its URL) or an "
                    f"order id (wo-/fo-/io-/inv-), got {ref!r} — a sentence is not a "
                    f"link the user can follow"
                )
            out["duplicate_of"] = ref
        else:
            value = str(value or "").strip()
            if not value:
                problems.append(
                    f"a {classification} verdict must carry `{name}`, naming what the "
                    f"user owes BY ID — the assumption, gate or decision"
                )
            elif len(value) < MIN_FIELD_CHARS:
                problems.append(
                    f"`{name}` is {len(value)} characters, under the {MIN_FIELD_CHARS} "
                    f"it takes to say which decision is owed and where it is"
                )
            out[name] = value
    return out


def _why_forbidden(classification: str, name: str) -> str:
    if name == "proposed_fix":
        return (f"a {classification} verdict files nothing, and a fix nobody files is a "
                f"suggestion the record carries as though it were work")
    if name == "duplicate_of":
        return "ops sets it, after the duplicate search a GAP verdict triggers (§2.5)"
    if name == "unsticks":
        return "nothing unsticks by itself here, or the classification is TRANSIENT"
    return "nothing is owed by the user here, or the classification is WAITING_ON_USER"


def _parse_fix(raw: Any, problems: list[str]) -> dict[str, str]:
    """The bug report a GAP verdict names"""
    if not isinstance(raw, dict):
        problems.append(
            f"a GAP verdict must carry `proposed_fix`, an object with "
            f"{', '.join(PROPOSED_FIX_FIELDS)} — ops files it as a bug, and "
            f"nothing else in the verdict says what to file"
        )
        return {}
    out: dict[str, str] = {}
    for name in PROPOSED_FIX_FIELDS:
        value = str(raw.get(name) or "").strip()
        if not value:
            problems.append(
                f"`proposed_fix.{name}` is required — `jarvis bug report` refuses a "
                f"filing without it, and refusing here costs one revision instead of a "
                f"settled order that filed nothing"
            )
        out[name] = value
    if out.get("description") and len(out["description"]) < MIN_FIELD_CHARS:
        problems.append(
            f"`proposed_fix.description` is {len(out['description'])} characters, under "
            f"the {MIN_FIELD_CHARS} it takes to brief a stranger — its reader is a fresh "
            f"session that sees the description and nothing else"
        )
    return out


def _parse_unsticks(raw: Any, problems: list[str]) -> dict[str, str]:
    """Why a TRANSIENT verdict settles silently: the mechanism that clears it, and when"""
    if not isinstance(raw, dict):
        problems.append(
            "a TRANSIENT verdict must carry `unsticks`, an object with `what` (the "
            "mechanism that clears it) and `when`")
        return {}
    out: dict[str, str] = {}
    for name in ("what", "when"):
        value = str(raw.get(name) or "").strip()
        if not value:
            problems.append(
                f"`unsticks.{name}` is required — a TRANSIENT verdict settles the order "
                f"silently, so the record has to say what clears the subject and when")
        out[name] = value
    return out


def verdict_stored(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Is THIS investigator's verdict already filed?"""
    from . import db

    if wo.get("kind") != "investigator" or not wo.get("parent_id"):
        return False
    try:
        fo = store.get_feature_order(str(wo["parent_id"]))
    except KeyError:
        return False
    return bool((db.from_json(fo.get("plan"), {}) or {}).get("classification"))


def verdict_landed(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Did this investigator's verdict land: stored, investigation `completed`, on its timeline?"""
    if not verdict_stored(store, wo):
        return False
    if store.get_feature_order(str(wo["parent_id"]))["status"] != "completed":
        return False
    return bool(store.events_of_kind(wo["id"], "verdict_submitted"))


def settle_headline(inv_id: str, verdict: dict[str, Any]) -> str:
    """The attention line a WAITING_ON_USER verdict raises — §2.5, step 6"""
    return (f"{inv_id}: {verdict.get('subject', '')} is waiting on you — "
            f"{verdict.get('user_owes', '')} (`jarvis investigate show {inv_id}`)")


def render_verdict(verdict: dict[str, Any]) -> list[str]:
    """One verdict as lines a human reads: the decision first, then the argument"""
    lines = [
        f"  classification: {verdict.get('classification', '')}"
        + (f" [{verdict['gap_class']}"
           + ("" if gaps.registered(str(verdict['gap_class']))
              else ", not registered — the fix order owes a detector and a remedy")
           + "]" if verdict.get("gap_class") else "")
        + (f" (ops: submitted as {verdict['submitted_classification']})"
           if verdict.get("classified_by") == "ops"
           and verdict.get("submitted_classification") else ""),
        f"  subject: {verdict.get('subject', '')}",
        f"  root cause: {verdict.get('root_cause', '')}",
    ]
    if verdict.get("user_owes"):
        lines.append(f"  you owe: {verdict['user_owes']}")
    unsticks = verdict.get("unsticks") or {}
    if unsticks:
        lines.append(f"  unsticks: {unsticks.get('what', '')} "
                     f"({unsticks.get('when', '')})")
    if verdict.get("duplicate_of"):
        lines.append(f"  duplicate of: {verdict['duplicate_of']}")
    fix = verdict.get("proposed_fix") or {}
    if fix:
        lines += [f"  proposed fix: {fix.get('title', '')}",
                  f"    expected: {fix.get('expected', '')}",
                  f"    actual: {fix.get('actual', '')}",
                  f"    priority: {fix.get('priority', '')}",
                  f"    detector: {fix.get('detector', '')}",
                  f"    remedy: {fix.get('remedy', '')}"]
    evidence = verdict.get("evidence") or []
    if evidence:
        lines.append("  evidence:")
        lines += [f"    - {item.get('quote', '')!r} ({item.get('source', '')})"
                  for item in evidence]
    filed = verdict.get("filed") or {}
    if filed:
        lines.append(f"  filed: {filed.get('issue_url', '')} "
                     f"({filed.get('wo_id') or 'no work order'})")
    elif verdict.get("filing_error"):
        lines.append(f"  NOT filed: {verdict['filing_error']}")
    return lines
