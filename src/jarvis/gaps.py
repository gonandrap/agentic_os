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
        headline="a hold written once, never re-derived, keeps an order parked after its "
                 "cause clears",
        symptom="the order sits in `validating` or `waiting_pr_merge` with nothing in "
                "flight and no question open",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/786",
        since="",
    ),
    "round-burn": GapClass(
        id="round-burn",
        headline="validation rounds spent on what is not the submitter's work, so the "
                 "order runs out unjudged",
        symptom="`max_rounds` reached with panel feedback about the base branch, not the "
                "diff",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/806",
        since="",
    ),
    "red-main": GapClass(
        id="red-main",
        headline="`main` is red and nothing notices, so every order built on it inherits "
                 "a failure it did not cause",
        symptom="a pull request whose checks fail for a reason absent from its own diff",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/793",
        since="",
    ),
    "oversized-input": GapClass(
        id="oversized-input",
        headline="a payload that could never fit is assembled and sent anyway, so the "
                 "step fails on size, not substance",
        symptom="a question, verdict or prompt refused for length, its work never read",
        detector="",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/797",
        since="",
    ),
    # §2.5 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md
    "unwired-precondition": GapClass(
        id="unwired-precondition",
        headline="an order's brief states a stop condition in no state the scheduler "
                 "reads, so a worker pays to rediscover the hold",
        symptom="a dispatched order whose first line says not to start, abandoned with "
                "no commits",
        detector="INV-PRECONDITION-UNWIRED",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1089",
        since="",
    ),
    "background-task-silence": GapClass(
        id="background-task-silence",
        headline="a worker awaiting a background task's result writes nothing, so the "
                 "sweep reads its open turn as wedged",
        symptom="a `running` order investigated while its transcript ends on a background "
                "launch",
        detector="background.live_tasks",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1562",
        since="",
    ),
    "designed-wait-undiscounted": GapClass(
        id="designed-wait-undiscounted",
        headline="the sweep is not taught a deliberate hold, so the wait is judged as "
                 "lost time",
        symptom="investigated with blocker `nothing the record can name`; OS working as "
                "designed",
        detector="INV-RED-RELEASE-UNDISCOUNTED",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1603",
        since="",
    ),
    "superseded-delivery": GapClass(
        id="superseded-delivery",
        headline="an equivalent change reaches the base first, so the pull request nets "
                 "to nothing",
        symptom="a passed order escalated as nothing to review; its open PR changes no files",
        detector="Daemon._settle_superseded",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1592",
        since="",
    ),
    "base-chase-livelock": GapClass(
        id="base-chase-livelock",
        headline="a queued pull request loses its turn when the sibling ahead merges, so "
                 "it is caught up and re-run after every merge and never reaches the front",
        symptom="a passed, green order in `waiting_pr_merge` whose history alternates "
                "`merge_queued` holds with `cause=behind` catch-ups, with no merge",
        detector="INV-BASE-CHASE-LIVELOCK",
        remedy=REMEDY_NONE,
        issue_url="https://github.com/gonandrap/agentic_os/issues/1608",
        since="",
    ),
}

#: Asserted equal to `tuple(GAP_CLASSES)`, on `remedies.SHIPPED_REMEDIES`' precedent
SHIPPED_GAP_CLASSES: tuple[str, ...] = ("stale-hold", "round-burn", "red-main",
                                        "oversized-input", "unwired-precondition",
                                        "background-task-silence",
                                        "designed-wait-undiscounted",
                                        "superseded-delivery", "base-chase-livelock")


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
    lines = ["Reuse a slug when the MECHANISM matches, whatever the subject. A new slug "
             "makes the next fix order register it, with a detector, a remedy and a test.",
             ""]
    for cls in GAP_CLASSES.values():
        # Terse, issue number alone: this list is inside a worker's 10K start budget
        lines += [
            f"- `{cls.id}` — {cls.headline}",
            f"  Seen: {cls.symptom}",
            f"  Detector: {cls.detector or 'NONE YET'}"
            + (f"; remedy: {cls.remedy}" if cls.remedy != REMEDY_NONE else "")
            + (f" (#{cls.issue_url.rsplit('/', 1)[-1]})" if cls.issue_url else ""),
        ]
    return "\n".join(lines)
