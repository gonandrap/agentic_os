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
        headline="a hold never re-derived keeps an order parked after its cause clears",
        symptom="parked in `validating` or `waiting_pr_merge`, nothing in flight, no question open",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/786",
        since="",
    ),
    "round-burn": GapClass(
        id="round-burn",
        headline="rounds spent on what is not the submitter's work, so the order runs out unjudged",
        symptom="`max_rounds` reached with panel feedback about the base, not the diff",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/806",
        since="",
    ),
    "red-main": GapClass(
        id="red-main",
        headline="`main` is red and nothing notices, so every order on it inherits the failure",
        symptom="a pull request whose checks fail for a reason absent from its own diff",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/793",
        since="",
    ),
    "oversized-input": GapClass(
        id="oversized-input",
        headline="a payload that can never fit is sent anyway; the step fails on size",
        symptom="a question, verdict or prompt refused for length",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/797",
        since="",
    ),
    # §2.5 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md
    "unwired-precondition": GapClass(
        id="unwired-precondition",
        headline="a brief states a stop condition no state holds, so a worker pays to rediscover it",
        symptom="dispatched, first line says not to start, abandoned with no commits",
        detector="INV-PRECONDITION-UNWIRED",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1089",
        since="",
    ),
    "background-task-silence": GapClass(
        id="background-task-silence",
        headline="a worker awaiting a background task writes nothing; the sweep reads it as wedged",
        symptom="`running`, investigated while its transcript ends on a background launch",
        detector="background.live_tasks",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1562",
        since="",
    ),
    "blocker-contradicts-record": GapClass(
        id="blocker-contradicts-record",
        headline="a catch-all blocker fires though its order's events deny its premise",
        symptom="a finished order blocked as 'stopped mid-task'",
        detector="INV-BLOCKER-CONTRADICTS-RECORD",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1591",
        since="",
    ),
    "designed-wait-undiscounted": GapClass(
        id="designed-wait-undiscounted",
        headline="the sweep judges a deliberate hold as lost time",
        symptom="blocker `nothing the record can name`; OS working as designed",
        detector="INV-RED-RELEASE-UNDISCOUNTED",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1603",
        since="",
    ),
    "superseded-delivery": GapClass(
        id="superseded-delivery",
        headline="an equivalent change lands first, so the pull request nets to nothing",
        symptom="a passed order escalated as nothing to review; its PR changes no files",
        detector="Daemon._settle_superseded",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1592",
        since="",
    ),
    "base-chase-livelock": GapClass(
        id="base-chase-livelock",
        headline="a queued PR loses its turn when the one ahead merges",
        symptom="green, alternating `merge_queued` holds and catch-ups, no merge",
        detector="INV-BASE-CHASE-LIVELOCK",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1608",
        since="",
    ),
    "mention-held-as-act": GapClass(
        id="mention-held-as-act",
        headline="high-stakes net matches a word, not an act; routine assumption waits for user",
        symptom="`needs_review` held as `commits to an act`; Neo's advice would approve",
        detector="rules.mention-held-as-act",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1610",
        since="",
    ),
    "jobless-run-unseen": GapClass(
        id="jobless-run-unseen",
        headline="a run that produced no job reports no check, so every CI remedy is blind",
        symptom="`waiting_pr_merge`, empty check rollup on the head, no blocker named",
        detector="Daemon.rerun_aborted_checks",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1657",
        since="",
    ),
}

#: Asserted equal to `tuple(GAP_CLASSES)`, on `remedies.SHIPPED_REMEDIES`' precedent
SHIPPED_GAP_CLASSES: tuple[str, ...] = ("stale-hold", "round-burn", "red-main",
                                        "oversized-input", "unwired-precondition",
                                        "background-task-silence",
                                        "blocker-contradicts-record",
                                        "designed-wait-undiscounted",
                                        "superseded-delivery", "base-chase-livelock",
                                        "mention-held-as-act", "jobless-run-unseen")


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
    lines = ["Reuse a slug for the same mechanism; a new slug makes the fix order register it.",
             ""]
    for cls in GAP_CLASSES.values():
        # One line, no issue number: this list is inside a worker's 10K start budget
        lines.append(f"- `{cls.id}`: {cls.headline}. Seen: {cls.symptom}. "
                     f"Detector: {cls.detector or 'NONE YET'}"
                     + (f"; remedy: {cls.remedy}" if cls.remedy != REMEDY_NONE else ""))
    return "\n".join(lines)
