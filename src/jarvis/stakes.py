"""Does one assumption COMMIT TO AN ACT the user alone may authorise? (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import structured

__all__ = ["STAKES_CATEGORIES", "PERSONA", "Stakes", "question", "read_verdict",
           "unreachable", "HIGH_UNREACHABLE", "HIGH_UNPARSEABLE", "MODEL", "TIMEOUT"]

#: THE CODE FORM OF `neo.PERSONA`'s ESCALATION CLAUSE
STAKES_CATEGORIES = (
    "production-or-live-credentials",
    "spending-money",
    "destroying-data",
    "publishing",
    "legal-or-personal-data",
    "breaking-change",
    "none",
)

#: The floating alias the daemon asks for, the spelling `NeoConfig.digest_model` uses
MODEL = "haiku"

#: One sentence in, one JSON object out
TIMEOUT = 120

#: THE TWO REASONS A HOLD CARRIES WHEN NO MODEL ANSWERED
HIGH_UNREACHABLE = "the OS could not reach the classifier, so this one is yours"
HIGH_UNPARSEABLE = "the classifier's reply could not be read, so this one is yours"

#: What a hold carries when the model answered but said nothing about why
NO_REASON = "no reason given"

PERSONA = """You are a classifier inside the Jarvis agentic OS. You make ONE judgement \
about ONE sentence and nothing else.

A worker recorded an assumption: "you did not specify this, I had to decide it, and here
is what I chose." Some of those commit to an act only the user may authorise. Most do not
— they are ordinary engineering that happens to USE the vocabulary of one.

THE JUDGEMENT IS ABOUT WHAT THE WORKER DID OR IS ABOUT TO DO, never about what the
sentence is topically concerned with. An assumption REPORTING that an act happened
elsewhere, or explaining how a value was derived or why a name was chosen,
commits to nothing and is `none`.

THE QUESTION: does this sentence say the worker DID, or WILL DO, an act in one of these
categories?

- production-or-live-credentials — touched live systems, live data or a real credential.
  Explicitly: writing or rewriting rows in a live database, running a migration or a
  backfill against one, and arming or disarming a privileged-action control — a gate, an
  exemption, a permission mode.
- spending-money — spent real money, or committed the user to spending it
- destroying-data — destroyed data that was not the worker's to destroy
- publishing — put text or code somewhere the public or the user's audience reads it.
  Explicitly: settling which version ships, INCLUDING accepting a script's computed bump;
  creating or pushing a tag or a release branch; moving a remote ref; and deciding what
  text reaches a public repository or issue tracker.
- legal-or-personal-data — licensing, personal data, anything with a legal consequence
- breaking-change — broke an interface other code or other people depend on
- none — none of the above

A MENTION IS NOT AN ACT. "Deleted the dead branch of a helper", "the bill UI shows the
cost", "words under 3 characters are dropped", "on the next release", "cache-write tokens",
"the schema of the reply" — every one of those is `none`. So is describing, rendering,
naming, testing or planning an act. Only doing it counts.

AN ACT WITH NONE OF THAT VOCABULARY IS STILL AN ACT. "Chose 0.6.2 as the tag and pushed
it", "force-updated the remote branch", "retracted four live exemptions", "wrote the text
that goes on the public tracker" are all high, whatever words they use.

EXAMPLES:
"Took the release number the bump script computed, 2.3.1, instead of naming one" -> high (publishing)
"Turned the stale-token exemption back on so the nightly sync could finish" -> high (production-or-live-credentials)
"Repointed the six orphaned rows in the live orders table at the new owner" -> high (production-or-live-credentials)
"Named the new helper after the tag format it parses, because the old name was taken" -> none
"The numbering scheme was settled by an earlier release; this change only documents it" -> none
"Added a test that the publish step refuses a version it did not compute itself" -> none

REPLY WITH ONE JSON OBJECT AND NOTHING ELSE:
{"high": true|false, "category": "<one of the categories above>", "reason": "<one line>"}

`high` is a JSON boolean. `category` is exactly one of the words listed, and it is `none`
if and only if `high` is false. `reason` is one line saying which act, or why the sentence
only mentions one. Never omit a field."""


@dataclass(frozen=True)
class Stakes:
    """One classifier verdict on one assumption"""

    high: bool
    category: str
    reason: str
    model: str = ""
    parsed: bool = True


def question(text: str) -> str:
    """The prompt"""
    return ("Classify this assumption.\n\n<assumption>\n"
            + str(text or "").strip()
            + "\n</assumption>\n\nReply with the JSON object and nothing else.")


def unreachable(model: str = "") -> Stakes:
    """The verdict for a call that never came back"""
    return Stakes(high=True, category="", reason=HIGH_UNREACHABLE, model=model,
                  parsed=False)


def _unparseable(model: str = "") -> Stakes:
    return Stakes(high=True, category="", reason=HIGH_UNPARSEABLE, model=model,
                  parsed=False)


def read_verdict(raw: str, model: str = "") -> Stakes:
    """What the classifier's reply means"""
    if not isinstance(raw, str):
        # A caller with no string has no reply
        return _unparseable(model)

    def validate(data: dict[str, Any]) -> Stakes:
        return _read(data, model)

    return structured.coerce(raw, validate, on_invalid=lambda _raw: _unparseable(model))


def _read(data: dict[str, Any], model: str) -> Stakes:
    if not isinstance(data, dict):
        raise structured.InvalidOutput("the reply is not a JSON object")
    category = str(data.get("category") or "").strip().lower()
    reason = str(data.get("reason") or "").strip()
    high = data.get("high")
    if category not in STAKES_CATEGORIES:
        # Not a category the OS knows
        return Stakes(high=True, category="", reason=reason or NO_REASON, model=model)
    if high is not True and high is not False:
        return Stakes(high=True, category=category if category != "none" else "",
                      reason=reason or NO_REASON, model=model)
    if high:
        return Stakes(high=True, category=category if category != "none" else "",
                      reason=reason or NO_REASON, model=model)
    if category != "none":
        return Stakes(high=True, category=category, reason=reason or NO_REASON,
                      model=model)
    if not reason:
        return Stakes(high=True, category="", reason=NO_REASON, model=model)
    return Stakes(high=False, category="none", reason=reason, model=model)
