"""Neo deciding a work order's pending assumptions, so the last human step can go.

docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md. With `validation.enabled`
and `validation.auto_merge` on, a work order already runs worker -> panel -> gate -> Neo ->
merged with nobody typing anything. One stop is left: a PENDING ASSUMPTION. `jarvis wo
review` is a decision the user owes, `wo ack` and `wo done` both refuse while one is
outstanding, and `automerge.decide`'s condition 3 holds the merge on exactly that. So an
order that recorded one assumption waits for a person however green everything else is.

Built in the image of `automerge.py` deliberately, because it is the same class of thing —
the OS taking an authority that was the user's — and a second vocabulary for it would be
the defect: a per-project flag shipping false at both levels (`ValidationConfig.
auto_review`), a PURE decision function with its conditions enumerated and unit-testable
without a network, a thin daemon half that has the database and the model call, and a hold
recorded once per (subject, reason) and rendered as one line the user can read.

**AUTO-ACCEPTING EVERY ASSUMPTION IS NOT THE FEATURE; IT IS THE FAILURE MODE.** An
assumption is a worker saying "I had to decide something you did not specify, and here is
what I chose" — some are typography and some change the product. Four things hold that
line, and each fails toward the user:

* **THE VERDICT SPACE IS ACCEPT OR ESCALATE. There is no machine rejection** (Neo, question
  301). Rejecting is not deciding an assumption, it is commissioning rework: it writes
  guidance to a worker that finished long ago and restarts a turn on an order nobody asked
  to reopen — the precise act `gates.apply_decision` fences `self_heal` and `auto_merge`
  against. So "this assumption is wrong" is something the OS cannot defend accepting, and
  it goes to the user WITH Neo's reading attached, which is strictly more than they get
  today.
* **TWO INDEPENDENT NETS CATCH A HIGH-STAKES ASSUMPTION**, and neither relies on the other.
  `HIGH_STAKES` below is the code form of Neo's own escalation clause (`neo.PERSONA`:
  production or live credentials, spending money, deleting or publishing, legal and people
  matters), applied before a call is even made; `read_ruling` then force-escalates anything
  Neo ITSELF did not classify as `stakes: routine`, whatever verdict it reached — an
  ALLOWLIST (`ROUTINE_STAKES`), because a missing, empty or misspelled `stakes` read as a
  blocklist means "no danger here" and switches the backstop off on the quietest possible
  failure. A regex cannot read meaning and a model cannot be relied on to volunteer its own
  doubt, or even to answer in the shape it was asked for. "Before any call" is
  a claim about the TEXT, so `sibling_line` applies the same net to the context list: an
  assumption held back for naming a credential must not arrive in the prompt for the
  routine one beside it.
* **ONE QUESTION PER ASSUMPTION**, never a batch verdict over a list. A list invites one
  judgement over the easiest member of it.
* **THE RECORD SAYS THE OS DECIDED IT**, with the reason, the model and the config version
  in force (`assumptions.decided_by`, and §4). `jarvis wo show` must never present a
  machine decision as the user's.

The interaction with auto-merge is the whole point and it needs no code: once the
assumptions are settled they are not pending, so `automerge.decide`'s condition 3 passes on
its own. That condition is NOT weakened — it still holds on a genuinely pending assumption,
and the redundancy is the argument of
docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: The Neo question kind. `neo_store.Q_KINDS` carries the full list — adding one is SEVEN
#: edits, not one (kn-4edb0eb7, which corrects kn-9b18a8eb's four). The four everyone
#: finds: this constant, a `deliver()` branch in `Daemon._neo_drain`,
#: `ops._neo_attention`'s filter and `invariants.check_neo_escalations_are_live`'s. The
#: three that bit this work order, all of them user-facing ANSWER paths that end in
#: `queue_message` and so can reopen a finished work order: `ops.neo_answer_escalated`,
#: `answer_form` in `ui/templates/_question.html`, and `ops.neo_review`'s `--correct`
#: tail. All seven are done for this kind; see the comment at `Q_KINDS`.
QUESTION_KIND = "assumption"

#: `assumptions.decided_by` for a verdict this module reached. Spelled through
#: `project_store.ASSUMPTION_DECIDER_OS` at its call sites; named here for
#: `automerge.GATE_KIND`'s reason.
DECIDER = "neo"

STAKES_ROUTINE = "routine"
STAKES_HIGH = "high"
#: What the record calls a `stakes` the reviewer never gave. Not `routine`: the absence of
#: a warning is not a warning's absence, and writing `routine` into the row would put a
#: judgement in Neo's mouth that it did not make.
STAKES_UNCLASSIFIED = "unclassified"

#: **AN ALLOWLIST, NOT A BLOCKLIST, AND THAT IS THE WHOLE POINT** (kn-32434cef's shape).
#: The one `stakes` value that leaves an acceptance standing. Everything else — the field
#: absent, empty, misspelled, truncated by `neo._validate_verdict`'s 20-char cap, or a
#: word nobody anticipated — is treated as high and escalates.
#:
#: Written as a blocklist (`== "high"`) the second net FAILED OPEN: the single most likely
#: malformed reply is one that simply omits the key, and that parsed cleanly, read as
#: routine and was accepted with the backstop silently off. A net whose default is "no
#: danger here" is not a net. The cost of the allowlist is an escalation the user was
#: going to handle anyway; the cost of the blocklist is a decision made in their name with
#: no backstop at all — the asymmetry the module docstring's "every row fails toward the
#: user" rests on.
#:
#: Exactly one entry, deliberately: `routine` is the word `ASSUMPTION_REVIEWER_PERSONA`
#: asks for. If a model spells it otherwise the escalation reason SAYS so, so the fix is a
#: visible persona edit rather than a quiet widening of this set.
ROUTINE_STAKES = frozenset({STAKES_ROUTINE})

#: WHY THE OS DID NOT DECIDE THIS ASSUMPTION, as a stable token. The dedupe in
#: `Daemon.auto_review` keys on (assumption, this), so one work order records each
#: distinct reason once instead of every reconcile tick — and a hold that CHANGES is still
#: recorded, which keying on the assumption alone would lose.
HELD_DISABLED = "disabled"
HELD_STATUS = "status"
HELD_SETTLED = "settled"
HELD_PANEL_GAVE_UP = "panel_gave_up"
HELD_REFUSAL_UNANSWERED = "refusal_unanswered"
HELD_ASKED = "asked"
HELD_HIGH_STAKES = "high_stakes"

#: THE FIRST NET, and it is deliberately not a taste filter. Every entry is the code form
#: of a clause `neo.PERSONA` already tells Neo to escalate on — production or live
#: credentials, spending money, deleting or publishing anything, legal and people matters —
#: so this is that rule applied one layer earlier rather than a second vocabulary for it.
#:
#: It is matched against the assumption's own text, before any model call, and a match
#: HOLDS: the assumption stays pending and the work order stays exactly where it is today.
#: So a false positive costs the user precisely what every assumption costs them now, and a
#: false negative is the only expensive direction — which is why the list errs wide and why
#: `read_ruling` is a second net behind it rather than the same one again.
#:
#: Word boundaries on both sides: `\bkey\b` must not fire on "monkey", and a substring
#: match would make the list unreadable as the rule it is meant to be.
HIGH_STAKES = (
    r"credential|secret|password|\bapi[ -]?key\b|\btoken\b|\bauth\b",
    r"\bproduction\b|\bprod\b|\blive\b",
    r"\bdelet|\bdestroy|\bdrop(ped|ping)?\b|\btruncat|\birreversib|\bpurge",
    r"\bmigrat|\bschema\b|\bbackfill",
    r"\bbill(ed|ing|s)?\b|\bprice|\bpricing\b|\binvoice|\bcharge[ds]?\b|\bspend",
    r"\bpublish|\bdeploy|\brelease[ds]?\b|\bship(ped|ping)?\b to ",
    r"\bpii\b|\bgdpr\b|personal data|\bpersonally identifiable",
    r"\blicen[cs]e|\blegal\b|\bcopyright\b",
    r"breaking change|backward(s)? incompatible",
)

_HIGH_STAKES_RE = re.compile("|".join(HIGH_STAKES), re.IGNORECASE)


def high_stakes_marker(text: str) -> str:
    """The high-stakes phrase this assumption contains, or `''`. Pure, no model.

    Returns the matched text rather than a boolean so the hold can SAY what it matched:
    "held — 'production' is a word the OS does not rule on" is actionable, and "high
    stakes" is not.
    """
    m = _HIGH_STAKES_RE.search(text or "")
    return m.group(0) if m else ""


@dataclass(frozen=True)
class Decision:
    """Armed to ask Neo, or held with a reason a person can read. Nothing else.

    NOT "armed to accept". This module's `decide` never settles anything — it decides
    whether the OS may put THIS assumption to Neo at all, and the ruling that comes back
    is `read_ruling`'s. Two functions because the two fail in different directions: a
    wrong `decide` spends a model call, a wrong `read_ruling` settles a decision that was
    the user's.
    """

    armed: bool
    code: str
    reason: str
    assumption_id: int = 0
    #: The assumption's position in its work order's list, as `all_assumptions` numbers
    #: it. What a person calls it; the id is what the database calls it.
    n: int = 0


@dataclass(frozen=True)
class Ruling:
    """What Neo's reply MEANS for one assumption, after the code has had its say."""

    accept: bool
    reason: str
    stakes: str
    model: str
    #: True when Neo accepted and this module overrode it — the `stakes: high` net. Worth
    #: its own field because "Neo declined" and "Neo agreed and the OS would not take it"
    #: are different facts about the reviewer, and only one of them is a reason to teach.
    overridden: bool = False


def _held(code: str, reason: str, **fields: Any) -> Decision:
    return Decision(armed=False, code=code, reason=reason, **fields)


def decide(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
           round_outcome: str = "", refusal_answered: bool = True,
           asked_question_id: int = 0) -> Decision:
    """May the OS decide this assumption right now? PURE — no store, no clock, no model.

    Dicts in, armed-or-held-with-a-reason out, for `automerge.decide`'s reason: the whole
    condition table is then unit-testable without a network, and the safety rule lives in
    one function rather than in a sequence of `if`s spread through a daemon method.

    The seven conditions, all of which must hold, cheapest and most specific first:

    1. the project has opted in AND the panel is on (`cfg.auto_review and cfg.enabled`);
    2. the work order is parked in `needs_review` — the one status that means the user
       owes a decision, and the analogue of `automerge.decide`'s `waiting_pr_merge`. An
       assumption recorded mid-run is NOT judged: the work it was part of does not exist
       yet, so a reviewer would be ruling on an intention;
    3. the assumption is still pending — nobody has settled it;
    4. the PANEL HAS NOT GIVEN UP on this work order. `ops.land_when_cleared` lands an
       `escalated` round, and the only caller that could reach it with one is the user
       saying ship it anyway. So clearing the assumptions under a give-up would have the
       OS silently answer a question the panel put in front of the user — a different
       question from the one it was asked;
    5. no refusal of the user's is outstanding (`ops.refusal_answered`). A refused
       assumption is guidance the worker has not answered, and settling its siblings
       would land the very decision the user turned down;
    6. it is not already with Neo on some OTHER question — one question per assumption,
       ever (see `asked_question_id` below);
    7. `high_stakes_marker` finds nothing in its text.

    Condition 1's redundancy with `Daemon.auto_review`'s own guard is deliberate and is
    `two-gates-not-a-chain`'s shape: a project's permission is asserted at the site that
    decides as well as the site that spends.

    **ASKED AT THE ASK SITE, ASKED AGAIN AT THE SETTLE SITE.** Everything above is a fact
    about state the ask does not freeze: a model call takes seconds to minutes, and in
    that window the panel can escalate, the user can cancel the order or refuse a
    sibling. Every one of those turns an armed assumption into one the OS must not touch,
    and the SETTLE is the act that cannot be taken back — it clears the assumption and
    `ops.land_when_cleared` lands the order behind it. So `Daemon._deliver_assumption_
    verdict` re-runs this against freshly read state immediately before accepting, and
    drops Neo's ruling when it no longer arms.

    `asked_question_id` is what makes that second call meaningful. Condition 6 exists to
    stop a SECOND question being filed; at the settle site the assumption is linked to
    the very question being delivered, so passing its id excludes it — while a link to a
    DIFFERENT question still holds, because two rulings on one assumption is a state
    nobody designed and not one to settle under.
    """
    if not (getattr(cfg, "enabled", False) and getattr(cfg, "auto_review", False)):
        return _held(HELD_DISABLED,
                     "this project has not given the OS permission to decide its "
                     "assumptions (`validation.auto_review`)")
    status = str(wo.get("status") or "")
    if status != "needs_review":
        return _held(HELD_STATUS,
                     f"the work order is {status or 'in no status'}, not waiting on a "
                     f"review")

    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
    if str(round_outcome or "").lower() == "escalated":
        return _held(HELD_PANEL_GAVE_UP,
                     "the validation panel gave up and put this work order in front of "
                     "you — settling its assumptions would answer that for you too",
                     **fields)
    if not refusal_answered:
        return _held(HELD_REFUSAL_UNANSWERED,
                     "you refused an assumption on this work order and the worker has "
                     "not delivered again since", **fields)
    asked = int(assumption.get("neo_question_id") or 0)
    if asked and asked != int(asked_question_id or 0):
        return _held(HELD_ASKED,
                     f"assumption #{n} is already with Neo (question {asked})", **fields)
    marker = high_stakes_marker(str(assumption.get("content") or ""))
    if marker:
        return _held(HELD_HIGH_STAKES,
                     f"assumption #{n} mentions {marker!r} — the OS does not decide "
                     f"those for you, whatever it thinks of them", **fields)
    return Decision(armed=True, code="armed",
                    reason=f"assumption #{n} is routine enough to put to Neo", **fields)


def read_ruling(verdict: dict[str, Any], default_model: str = "") -> Ruling:
    """What Neo's reply means for one assumption. PURE — the second of the two nets.

    ACCEPT IS THE NARROW PATH AND EVERYTHING ELSE IS AN ESCALATION, which is what makes
    the fail-closed direction structural rather than remembered: acceptance needs two
    positive facts (Neo did not escalate, AND it ruled `approve`), so an unparseable
    reply, a transport failure, a missing field or a model that answered some other
    question all land with the user.

    `verdict: deny` — Neo thinking the assumption is WRONG — escalates too, carrying its
    reason. There is no machine rejection (module docstring), and the user reading "Neo
    would have turned this down: …" is strictly better informed than they are today.

    **WHY THIS COMPARES `"approved"` WHEN THE PERSONA ASKS FOR `"approve"`.** It is not a
    mismatch: `neo._validate_verdict` puts every reply through `neo._gate_verdict`, whose
    `_VERDICT_ALIASES` maps the bare verb to the past participle before this function sees
    it, and falls back to `denied` for anything it does not recognise. So the persona's
    word and the database's word are the same fact and the normalisation is one layer
    down. The direction of that fallback is what makes comparing the participle safe: if a
    future persona edit taught Neo a word `_VERDICT_ALIASES` has never heard of, every
    reply would read `denied` and this feature would stop accepting anything — dead rather
    than dangerous, which is the one way round it is allowed to break.

    `stakes` OVERRIDES AN ACCEPTANCE, AND IT IS READ AS AN ALLOWLIST. The reviewer is asked
    to classify the stakes separately from ruling on them, and the classification wins, for
    the reason the plan path's child cap wins over Neo (`Daemon._deliver_plan_verdict`): a
    backstop a reviewer can wave through is not one. `ROUTINE_STAKES` holds the only value
    that leaves an acceptance standing — so a missing, empty, misspelled or truncated
    `stakes` escalates, instead of reading as "no danger here" and switching the second net
    off on the quietest possible failure. Three positive facts are therefore needed to
    accept, not two.
    """
    stakes = str(verdict.get("stakes") or "").strip().lower() or STAKES_UNCLASSIFIED
    reason = str(verdict.get("reason") or "").strip() or "no reason given"
    model = str(verdict.get("model") or default_model or "")
    accept = (not verdict.get("escalate")) and verdict.get("verdict") == "approved"
    if accept and stakes not in ROUTINE_STAKES:
        # Two spellings of one override, because the user reads this line and the two
        # facts are different: Neo warned and the OS obeyed, or Neo said nothing readable
        # and the OS would not take the silence for an answer.
        said = (f"marked it high-stakes, and those are yours"
                if stakes == STAKES_HIGH else
                "did not classify the stakes at all, and the OS does not read silence "
                "as routine"
                if stakes == STAKES_UNCLASSIFIED else
                f"classified the stakes as {stakes!r}, which the OS cannot read as "
                f"routine")
        return Ruling(accept=False, stakes=stakes, model=model, overridden=True,
                      reason=f"Neo would have accepted it but {said}: {reason}")
    if not accept and not verdict.get("escalate"):
        return Ruling(accept=False, stakes=stakes, model=model,
                      reason=f"Neo would have turned this down: {reason}")
    return Ruling(accept=accept, stakes=stakes, model=model, reason=reason)


# -- the reviewer ----------------------------------------------------------------------


ASSUMPTION_REVIEWER_PERSONA = """You are Neo, the user's delegate inside the Jarvis \
agentic OS, ruling on ONE assumption a worker recorded.

An assumption is a worker saying: "you did not specify this, I had to decide it, and here
is what I chose." Until now every one of them waited for the user. Your job is to take the
ROUTINE ones off their desk and leave the rest exactly where they are.

YOU HAVE TWO ANSWERS. There is no third.
- ACCEPT — `{"escalate": false, "verdict": "approve", "stakes": "routine", "reason": "…"}`.
  The worker's call is one the user would have made, or one that costs nothing either way.
- ESCALATE — `{"escalate": true, "verdict": "deny", "stakes": "…", "reason": "…"}`. The
  user decides this one.
You CANNOT reject an assumption. Rejecting means sending a finished worker back to redo
work, which is not yours to order. If you think the call was WRONG, escalate and say so in
`reason` — the user will read it and reject it themselves.

ACCEPT when the assumption is mechanical or conventional: naming, file layout, test
placement, comment and docstring style, branch and commit shape, which of two equivalent
libraries already in the project was used, an internal helper's signature, log wording.
These are decisions in name only, and charging the user a review action for one is the
cost this exists to remove.

ESCALATE when the assumption CHANGES SOMETHING THE USER WOULD RECOGNISE: user-visible
behaviour or wording, a default value, an API or CLI surface others call, a data shape
written to disk, an error the user will see, scope the worker added or dropped, a
dependency added, or anything the work order's own text said the user cared about. Also
escalate whenever you would need to know a preference you have no learning about — a
learning below is authority, and its absence is not.

`stakes` IS A SEPARATE JUDGEMENT FROM YOUR VERDICT, and you must give it on every answer.
Say `"high"` when the assumption touches production or live credentials, spends money,
deletes or publishes anything, carries legal or personal-data weight, or would be painful
to undo — even if you are also accepting it. High stakes goes to the user whatever you
ruled, and marking one honestly is how you stay trusted on the rest.

A SIBLING MARKED `(withheld — high-stakes …)` IS NOT A BLANK TO FILL IN. The OS holds
those assumptions for the user and does not show you their text, deliberately. Do not
guess at what one says, and do not treat its absence as permission: if your ruling would
turn on what it contains, that is precisely a case to ESCALATE.

WHEN IN ANY DOUBT, ESCALATE. The cost of escalating wrongly is one review action the user
was going to make anyway. The cost of accepting wrongly is a decision they never made,
shipped in their name.

`reason` is ONE LINE. On an acceptance it says why the call was routine; on an escalation
it says what the user has to decide. It is read on the work order's record, not by the
worker — the worker has finished and nothing you say here reaches it.
"""


def sibling_line(s: dict[str, Any]) -> str:
    """One sibling assumption as CONTEXT, with the high-stakes net applied to it too.

    **THE NET IS ABOUT TEXT REACHING A MODEL, NOT ABOUT WHOSE ROW IT IS.** Condition 7
    holds an assumption that names a credential, production, a deletion or a migration —
    and the guarantee that buys (spec §2.2: caught "before any model call") is worth
    nothing if the same sentence is then pasted into the prompt for the routine
    assumption beside it. One high-stakes row and one routine row on one work order is
    the ordinary case, not a corner, so the leak would have been the common path.

    Withheld rather than DROPPED. Silence would tell the reviewer this work order had
    only routine assumptions, and "is this one defensible on its own?" is a different
    question when the answer is no because of a row it cannot see. The number, the status
    and the fact that something is being withheld are a classification, not the secret.
    """
    content = str(s.get("content") or "")
    if high_stakes_marker(content):
        return (f"  #{s['n']} [{s['status']}] (withheld — high-stakes, and the user's "
                f"alone to decide)")
    return f"  #{s['n']} [{s['status']}] {content[:200]}"


def _ruling_question(project: str, wo: dict[str, Any], assumption: dict[str, Any],
                     siblings: list[dict[str, Any]]) -> str:
    """What the reviewer reads. One assumption, quoted; the rest listed, not ruled on.

    The siblings are here because an assumption is sometimes only defensible given
    another one, and NOT as a list to rule over — the instruction says so twice, and the
    code applies the ruling to exactly one row whatever comes back. Each one goes through
    `sibling_line`, which applies the same high-stakes rule that decided whether it could
    be ruled on at all.
    """
    n = assumption.get("n")
    others = "\n".join(sibling_line(s) for s in siblings
                       if s["id"] != assumption["id"]) or "  (none)"
    return "\n\n".join([
        f"ASSUMPTION REVIEW — rule on assumption #{n} of {wo['id']} in {project}, and on "
        f"nothing else.",
        f"# The assumption\n{assumption.get('content') or '(empty)'}",
        f"# The work order it was recorded against\n{wo.get('title') or '(untitled)'}\n"
        f"{(wo.get('description') or '')[:2000]}",
        f"# What the worker says it delivered\n"
        f"{(wo.get('result_summary') or '(nothing recorded)')[:1500]}",
        f"# The work order's other assumptions, for context only — do not rule on these\n"
        f"{others}",
        "Answer with `escalate`, `verdict` (`approve` to accept it, `deny` to send it to "
        "the user), `stakes` (`routine` or `high`) and a one-line `reason`.",
    ])


def propose(store: Any, neo: Any, project: str, wo: dict[str, Any],
            assumption: dict[str, Any], siblings: list[dict[str, Any]]) -> dict[str, Any]:
    """Put ONE assumption to Neo. Returns the question row.

    Idempotency is `assumptions.neo_question_id` and it is checked in `decide` (condition
    6), not here, so that "already asked" is a hold with a reason like every other rather
    than a silent `None` — one question per assumption, for its whole life. A question
    that Neo escalated is therefore never re-asked: the user holds it, and asking again
    every reconcile tick would be the OS lobbying them.
    """
    question = neo.ask(project, wo["id"],
                       _ruling_question(project, wo, assumption, siblings),
                       context=f"{wo.get('title') or ''}\n"
                               f"{(wo.get('description') or '')[:800]}",
                       kind=QUESTION_KIND)
    store.link_assumption_question(assumption["id"], question["id"])
    store.add_event(wo["id"], "autoreview_asked", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "neo_question_id": question["id"]})
    log.info("auto-review asked Neo about assumption #%s of %s as question %s",
             assumption.get("n"), wo["id"], question["id"])
    return question
