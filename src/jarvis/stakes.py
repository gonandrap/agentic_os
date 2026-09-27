"""Does one assumption COMMIT TO AN ACT the user alone may authorise? Pure.

docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md SS3.1-SS3.3.

`autoreview.HIGH_STAKES` is a word-shaped net asked a question about acts, and measured on
the fleet it fires on 91 of 498 assumptions of which ~13 are acts — while missing a release
cut, a live gate exemption retracted and a force-pushed branch. A wide net is a cost; a net
that is wide in the wrong places is a defect, and no further narrowing reaches the misses.
Deciding whether a sentence commits to an act is a READING task, delegated to a model
everywhere else in this feature and to a regex only at the one point where the answer
decides whether to call at all.

**THE PROMPT, THE PARSE, THE VERDICT TYPE — NO STORE, NO CLOCK, NO MODEL CALL.** The same
split `autoreview` / `daemon` uses everywhere: the transport and the accounting live in
`Daemon._classify_stakes`, so the decision table stays unit-testable without a network.

**ROUTINE NEEDS TWO POSITIVE FACTS; HIGH NEEDS NONE** (`read_verdict`). Written the other
way round — a blocklist of dangerous categories — this would fail OPEN, which is the
precise bug `autoreview.ROUTINE_STAKES` was written to fix after it shipped once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import structured

__all__ = ["STAKES_CATEGORIES", "PERSONA", "Stakes", "question", "read_verdict",
           "unreachable", "HIGH_UNREACHABLE", "HIGH_UNPARSEABLE", "MODEL", "TIMEOUT"]

#: THE CODE FORM OF `neo.PERSONA`'s ESCALATION CLAUSE, which is what `HIGH_STAKES`' own
#: comment already claims to be — production or live credentials, spending money,
#: deleting or publishing anything, legal and people matters. Same clause, expressed as
#: categories a READER can answer rather than as words a matcher can find.
#: `breaking-change` is `HIGH_STAKES`' last row.
STAKES_CATEGORIES = (
    "production-or-live-credentials",
    "spending-money",
    "destroying-data",
    "publishing",
    "legal-or-personal-data",
    "breaking-change",
    "none",
)

#: The floating alias the daemon asks for, the spelling `NeoConfig.digest_model` uses.
#: The model that ACTUALLY answered is `HeadlessResult.model` and that is what is
#: recorded on the verdict and in `agent_calls`.
MODEL = "haiku"

#: One sentence in, one JSON object out. Generous next to the work, tight enough that a
#: hung call cannot hold an assumption pass open for the panel's five minutes.
TIMEOUT = 120

#: THE TWO REASONS A HOLD CARRIES WHEN NO MODEL ANSWERED, and they are different facts:
#: nothing came back, versus something came back that could not be read. Both hold, and
#: the NEVER-FABRICATE-A-DEFAULT-FROM-A-FAILURE learning is why each says so in the line
#: the timeline renders instead of borrowing a category or a reason from the regex.
HIGH_UNREACHABLE = "the OS could not reach the classifier, so this one is yours"
HIGH_UNPARSEABLE = "the classifier's reply could not be read, so this one is yours"

#: What a hold carries when the model answered but said nothing about why.
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
    """One classifier verdict on one assumption.

    `parsed` is False only on the two failure paths — nothing answered, or the answer was
    unreadable — so a reader can tell a model that ruled HIGH from a hold nobody ruled on.
    """

    high: bool
    category: str
    reason: str
    model: str = ""
    parsed: bool = True


def question(text: str) -> str:
    """The prompt. ONE assumption's text and NOTHING THAT IS NOT NEEDED.

    No work order, no siblings, no diff, on purpose (SS3.1): context is what makes a
    classifier drift. Given the work order's title the model starts ruling on whether the
    CHANGE is safe, which is `ASSUMPTION_REVIEWER_PERSONA`'s job and already happens one
    call later.
    """
    return ("Classify this assumption.\n\n<assumption>\n"
            + str(text or "").strip()
            + "\n</assumption>\n\nReply with the JSON object and nothing else.")


def unreachable(model: str = "") -> Stakes:
    """The verdict for a call that never came back. HELD, and it says so."""
    return Stakes(high=True, category="", reason=HIGH_UNREACHABLE, model=model,
                  parsed=False)


def _unparseable(model: str = "") -> Stakes:
    return Stakes(high=True, category="", reason=HIGH_UNPARSEABLE, model=model,
                  parsed=False)


def read_verdict(raw: str, model: str = "") -> Stakes:
    """What the classifier's reply means. PURE, NEVER RAISES, fail-closed default HELD.

    `read_ruling`'s ALLOWLIST discipline verbatim (autoreview.py:786-793, kn-32434cef):

    1. `category` is read against `STAKES_CATEGORIES` as an allowlist. Absent, empty,
       misspelled or a word nobody anticipated is HIGH, not `none`.
    2. `high: false` is honoured ONLY when `category == "none"`. The two fields
       disagreeing is a model that did not answer the question asked, and that is HIGH —
       and the category it named rides on the hold, because it is the informative part.
    3. `high` absent, or not a real boolean (the string `"false"`, `0`), is HIGH.
    4. `reason` empty on a HIGH verdict is legal and reads "no reason given"; empty on a
       `none` verdict is HIGH, because the routine path is the one that needs defending.

    Parsing goes through `structured.coerce`, which tolerates fenced and chatty output and
    catches EVERY exception from the validator rather than only `InvalidOutput`.
    """
    if not isinstance(raw, str):
        # A caller with no string has no reply, which is the unreadable case and not an
        # argument error: this function's contract is that it never raises.
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
        # Not a category the OS knows. `category=""` because writing the model's invented
        # word into the field would let a later reader treat it as one of the six.
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
