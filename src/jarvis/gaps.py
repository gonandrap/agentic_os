"""The GAP CLASS registry: the stable name a diagnosis and a detector share (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A class that is deliberately NOT automated: no remedy may run
REMEDY_NONE = "none"

#: The shape of a slug, and the ONLY thing a verdict or a ledger row checks
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){1,4}$")


class GapClassError(ValueError):
    """A slug that is not well formed"""


@dataclass(frozen=True)
class GapClass:
    """One mechanism the OS gets stuck on, named once so every surface agrees"""

    id: str          # the slug
    headline: str    # the mechanism, in the terms a reviewer needs
    symptom: str     # what a stuck order looks like from outside
    detector: str    # the invariant id or doctor check that recognises it, from state
    remedy: str      # a `remedies.REMEDIES` id, or REMEDY_NONE
    issue_url: str   # the tracker issue whose fix registered this class
    since: str       # the jarvis version the detector shipped in


GAP_CLASSES: dict[str, GapClass] = {
    "stale-hold": GapClass(
        id="stale-hold",
        headline="a hold written once and never re-derived keeps an order parked after "
                 "the cause has cleared",
        symptom="the order sits in `validating` or `waiting_pr_merge` with nothing in "
                "flight and no question open",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/786",
        since="",
    ),
    "round-burn": GapClass(
        id="round-burn",
        headline="a validation round is spent on something that is not the submitter's "
                 "work, so the order runs out of rounds without ever being judged",
        symptom="`max_rounds` reached with the panel's feedback about the base branch "
                "rather than the diff",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/806",
        since="",
    ),
    "red-main": GapClass(
        id="red-main",
        headline="`main` is red and nothing in the OS notices, so every order built on "
                 "it inherits a failure it did not cause",
        symptom="a pull request whose checks fail for a reason absent from its own diff",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/793",
        since="",
    ),
    "oversized-input": GapClass(
        id="oversized-input",
        headline="a payload that could never fit where it was going is assembled and "
                 "sent anyway, so the step fails on size rather than on substance",
        symptom="a question, verdict or prompt refused for its length, with the work "
                "itself never read",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/797",
        since="",
    ),
    # §2.5 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md
    "unwired-precondition": GapClass(
        id="unwired-precondition",
        headline="an order's brief states a stop condition that exists in no state the "
                 "scheduler reads, so the claim takes it and a worker pays to rediscover "
                 "the hold",
        symptom="a dispatched order whose first line says it must not start, abandoned "
                "with no commits",
        detector="INV-PRECONDITION-UNWIRED",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1089",
        since="",
    ),
}

#: Asserted equal to `tuple(GAP_CLASSES)`, on `remedies.SHIPPED_REMEDIES`' precedent
SHIPPED_GAP_CLASSES: tuple[str, ...] = ("stale-hold", "round-burn", "red-main",
                                        "oversized-input", "unwired-precondition")


def checked_slug(raw: str) -> str:
    """The slug, or `GapClassError`"""
    slug = (raw or "").strip()
    if not SLUG_RE.match(slug):
        raise GapClassError(
            f"{raw!r} is not a gap class slug — two to five lowercase hyphenated words "
            f"naming the MECHANISM (`stale-hold`, `oversized-input`), not a sentence and "
            f"not a symptom")
    return slug


def get(slug: str) -> GapClass | None:
    """The registered class, or None"""
    return GAP_CLASSES.get((slug or "").strip())


def registered(slug: str) -> bool:
    """Does this class have a reviewed detector and remedy?"""
    return (slug or "").strip() in GAP_CLASSES


def by_invariant() -> dict[str, str]:
    """Detector id to class id — the inverse the daemon reads per `Violation`"""
    return {c.detector: c.id for c in GAP_CLASSES.values() if c.detector}


def render_registry() -> str:
    """The registry as both the investigator's prompt and a fix order's brief see it"""
    lines = ["# Gap classes the OS already knows",
             "Reuse a slug when the MECHANISM matches, whatever the subject. Coin a new "
             "one only when none of these is the same mechanism — and know what that "
             "commits: a new slug makes the fix order that follows responsible for "
             "registering it, with a detector, a remedy and a test.",
             ""]
    for cls in GAP_CLASSES.values():
        lines += [
            f"- `{cls.id}` — {cls.headline}",
            f"  Looks like: {cls.symptom}",
            f"  Detector: {cls.detector or 'NONE YET — no state check recognises this'}"
            f"; remedy: {cls.remedy}"
            + (f" ({cls.issue_url})" if cls.issue_url else ""),
        ]
    return "\n".join(lines)
