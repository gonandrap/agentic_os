"""The GAP CLASS registry: the stable name a diagnosis and a detector share.

Appendix A of docs/superpowers/specs/2026-09-27-investigation-orders.md. An
investigation's durable output used to be prose, so occurrence *n* of one mechanism cost
the same model session as occurrence 1 and nothing on the record could say it was
occurrence *n*. A class slug is what makes the two vocabularies meet: a verdict carries
one, the recurrence ledger keys on one, and a detector in `invariants.INVARIANTS` plus a
remedy in `remedies.REMEDIES` is what registering one HERE means.

Modelled on `remedies.py` in full — a frozen dataclass carrying the words a reviewer
reads, a registry closed by a test, and ONE renderer for every reader.

**TWO TIERS, and only one of them is closed.** Getting this backwards is the whole design
mistake the appendix rejects (A.12):

* **A verdict and a ledger row accept any WELL-FORMED SLUG** — `SLUG_RE`, nothing else.
  The first occurrence of a class nobody has seen is exactly the case an investigation
  exists for, and a closed vocabulary at the verdict would either refuse that verdict or
  make the investigator pick the nearest existing slug and record a lie.
* **`GAP_CLASSES` is closed by `SHIPPED_GAP_CLASSES`, asserted by a test.** Membership does
  not mean "a name the OS has heard"; it means **this class has a detector and a remedy,
  and a human reviewed both**. The only route in is a merged pull request that carries them
  — an investigator cannot write here (`hooks.investigator_write_decision` refuses it),
  which is what makes the split safe rather than notional.

The loop closes at `invariants.check_gap_classes_are_registered` (INV-GAP-REGISTERED): a
ledger row whose fix work order LANDED, for a class still absent from `GAP_CLASSES`, is
"somebody shipped a symptom fix and called it done" — reported by `jarvis doctor` with no
model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A class that is deliberately NOT automated: no remedy may run, and the detector raises
#: attention instead. Not the absence of an answer — the answer being "a human decides".
REMEDY_NONE = "none"

#: The shape of a slug, and the ONLY thing a verdict or a ledger row checks. Two to five
#: lowercase hyphenated words: `stale-hold`, `oversized-input`. Long enough to name a
#: mechanism, short enough to be a key someone types.
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){1,4}$")


class GapClassError(ValueError):
    """A slug that is not well formed. The one refusal this module makes."""


@dataclass(frozen=True)
class GapClass:
    """One mechanism the OS gets stuck on, named once so every surface agrees.

    `detector` is an `invariants` id or a doctor check — a predicate over STATE, never a
    model call (A.9: the fleet-health trigger asks "is this order progressing?" and buys a
    session; a detector asks "is THIS class present?" and buys a remedy). `remedy` is a
    `remedies.REMEDIES` id or `REMEDY_NONE`, and it is the ONLY thing a daemon obeys: the
    `remedy` an investigator names in prose is a recommendation to the fix order's author.

    An entry with an empty `detector` is honest rather than broken — it says the class is
    KNOWN and NOT YET MECHANICAL, which is what the metric should read as 0% mechanical.
    """

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
}

#: Asserted equal to `tuple(GAP_CLASSES)`, on `remedies.SHIPPED_REMEDIES`' precedent. The
#: registry is closed BY A TEST rather than by convention, so registering a class fails a
#: suite until a human has read the diff that carries its detector, remedy and test.
SHIPPED_GAP_CLASSES: tuple[str, ...] = ("stale-hold", "round-burn", "red-main",
                                        "oversized-input")


def checked_slug(raw: str) -> str:
    """The slug, or `GapClassError`. SHAPE ONLY — never membership.

    The verdict's check, and the reason is the two tiers above: an unregistered slug is
    the normal case for a mechanism nobody has seen before.
    """
    slug = (raw or "").strip()
    if not SLUG_RE.match(slug):
        raise GapClassError(
            f"{raw!r} is not a gap class slug — two to five lowercase hyphenated words "
            f"naming the MECHANISM (`stale-hold`, `oversized-input`), not a sentence and "
            f"not a symptom")
    return slug


def get(slug: str) -> GapClass | None:
    """The registered class, or None. None means "not mechanical yet", never "invalid"."""
    return GAP_CLASSES.get((slug or "").strip())


def registered(slug: str) -> bool:
    """Does this class have a reviewed detector and remedy? See the module docstring."""
    return (slug or "").strip() in GAP_CLASSES


def by_invariant() -> dict[str, str]:
    """Detector id to class id — the inverse the daemon reads per `Violation`.

    Classes with an empty `detector` are absent rather than keyed on `""`: an unkeyed
    violation must not be attributed to the first class that has no detector yet.
    """
    return {c.detector: c.id for c in GAP_CLASSES.values() if c.detector}


def render_registry() -> str:
    """The registry as both the investigator's prompt and a fix order's brief see it.

    One renderer for both readers, for `remedies.render_catalogue`'s stated reason: a
    model shown a different list from the one the code enforces asks for things that are
    refused.
    """
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
