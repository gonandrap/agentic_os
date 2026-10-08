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

**THE MID-RUN CASE IS JUDGED TOO, AND A MID-RUN VERDICT SETTLES NOTHING.**
docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
reverses one ruling that used to be in print here: that an assumption recorded mid-run is
not judged at all. The argument for it — a reviewer would be ruling on an INTENTION, with
no result summary and no diff — is not overturned, it is answered. `decide_early` arms a
SECOND pass over `running` orders whose verdict is written to the `provisional_*` columns
and to nothing else: `assumptions.status` stays `pending`, `ops.accept_assumption` is not
called, and every caller that reads a pending assumption reads one. What the early pass
buys is the one thing a verdict on an intention is good for — the worker learns while it
can still act on it. The settle is still `decide`'s, still at `needs_review`, and a
provisional approval is re-asked against the diff before it settles (§7).

So the two functions fail in opposite directions and that is why there are two: a wrong
`decide_early` spends a model call and tells a worker something, a wrong `decide` settles a
decision that was the user's. **`decide`'s condition 2 is therefore never relaxed** — it is
the only guard on `ops.accept_assumption` -> `ops.land_when_cleared`, and a running work
order that got past it would LAND.

The interaction with auto-merge is the whole point and it needs no code: once the
assumptions are settled they are not pending, so `automerge.decide`'s condition 3 passes on
its own. That condition is NOT weakened — it still holds on a genuinely pending assumption,
and the redundancy is the argument of
docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from .catalog import DEFAULT_VALIDATION_DECISION_RECORD_CHARS
from .stakes import Stakes

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

#: A question id the assumption's own text names — `Neo question 887`, `question 887`,
#: `Q887`, `Neo 887`, every form seen in the field. Bounded to six digits so a commit-hash
#: fragment or a line count cannot become an id.
_CITED_QUESTION_RE = re.compile(
    r"\b(?:neo\s+question|question|neo|q)\s*#?\s*(\d{1,6})\b", re.IGNORECASE)

#: How many cited ids one packet quotes. A reviewer packet is not a bibliography, and
#: `content` naming six question ids is a different problem.
_CITED_LIMIT = 5

#: How many answered rows the record PULLS (spec §3). Not a second policy: the character
#: cap binds first at any sane `decision_record_chars`, and this only stops a very old
#: order loading thousands of rows into memory. Rows it cuts are counted in the same
#: omission line, so no ruling is ever withheld in silence.
_ANSWERED_ROWS = 200

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

#: The two sentences that code renders, and which one depends on whether anything moved.
#: Spec §2c of
#: docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md: the
#: first is FALSE on wo-dbea82cf, where the worker pushed three commits and never ran
#: `jarvis wo finish`. Neither carries a count, a sha or an elapsed time — `ack_attention`
#: stores this verbatim and INV-ATTENTION-REASON compares it (kn-681db233 point 3).
REFUSAL_UNANSWERED_REASON = ("you refused an assumption on this work order and the "
                             "worker has not delivered again since")
#: Issue #975: the held reason a user reads must end in something they can type.
REFUSAL_UNDECLARED_REASON = ("you refused an assumption on this work order, and the "
                             "worker has pushed commits since without running `jarvis "
                             "wo finish` — the OS has asked it to declare them; you can "
                             "also send it a message asking it to finish, or run `jarvis "
                             "validation force` to judge the pull request as it stands")
HELD_ASKED = "asked"
HELD_HIGH_STAKES = "high_stakes"
#: What the RUNNING pass needs and the parked one cannot reach: an assumption this pass
#: has already formed a verdict on. Its own token rather than `HELD_SETTLED`, because
#: `settled` is a fact about the user's decision and this one is explicitly not
#: (`provisional_verdict` settles nothing — §4.1 of the 2026-09-23 spec).
HELD_JUDGED = "judged_early"

#: The confirmation pass's own five (spec §7), beside the seven `decide` already has.
#: `unjudged` and `objected` mean there is nothing to confirm — an early `object`
#: approved nothing, so no second call is spent on one and the user decides it;
#: `confirming` is the question already filed; `objection_in_flight` means NOT YET and is
#: retried next tick; `evidence_secret` is `decide_evidence`'s, over BOTH texts the
#: question would carry — the diff and the result summary.
#:
#: FOUR OF THE FIVE REACH THE RECORD, and which ones is not arbitrary (kn-22ba6087: a
#: guard that returns early must still record why). `objected`, `unjudged`,
#: `objection_in_flight` and `evidence_secret` are all facts about a row the OS LOOKED AT
#: and did not act on, and `objection_in_flight` and `evidence_secret` most of all —
#: without the line there is nothing on the record saying why an assumption the feature
#: was switched on for is still sitting with the user. `confirming` is the only one
#: suppressed, on `asked`'s list in `Daemon._note_autoreview_held` and for `asked`'s
#: reason: the question IS filed, which is the pass working.
HELD_UNJUDGED = "unjudged"
HELD_OBJECTED = "objected"
HELD_CONFIRMING = "confirming"
HELD_OBJECTION_IN_FLIGHT = "objection_in_flight"
HELD_EVIDENCE_SECRET = "evidence_secret"

#: docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-an-assumption-
#: for-ever.md §5 and §6. `round_open` is `decide_confirm`'s alone — the confirmation
#: judges the DELIVERED RESULT, so a round the panel has not finished may be about to send
#: that result back (§5.2, and the ASK pass keeps its licence to arm on a pending round).
#: `confirm_spent` is `confirming` SPLIT: one code with a conditional suppression would
#: make `daemon._holds_not_recorded`'s answer depend on state it cannot see. The live one
#: stays suppressed, the spent one is VISIBLE — it is the user's, and nothing else on the
#: record says so.
HELD_ROUND_OPEN = "round_open"
HELD_CONFIRM_SPENT = "confirm_spent"

#: WHICH DROPPED CONFIRMATION MAY BE ASKED AGAIN — the settle site's allowlist (§4.1). A
#: code is here when the drop is a fact that can clear with the user doing nothing AND
#: says nothing about whether the assumption is theirs to decide. An ALLOWLIST, not
#: "everything but the three that stay": a blocklist admits a new code the day somebody
#: writes a new hold, and this set governs whether the OS spends another model call.
#: `settled` (no reader — the drop site returns earlier) and `evidence_secret` (a
#: statement that the row is the user's, and a second chance to copy a secret into the
#: question store) were examined and refused.
TRANSIENT_DROPS = frozenset({HELD_STATUS, HELD_REFUSAL_UNANSWERED,
                             HELD_OBJECTION_IN_FLIGHT})

#: A round's outcome that means the panel has NOT finished with it. `pending` is the
#: column's default on an open round and `''` is a row that never got one.
_UNRESOLVED_OUTCOMES = ("", "pending")

#: THE STATUS EACH PASS ACTS IN, and the conditions below read these rather than a literal
#: of their own (2026-09-27-a-stale-merge-hold-is-not-the-reason-a-pr-is-not-merging.md §6).
#: `ops._panel_hold_is_stale` reads `REVIEW_PASS_STATUSES` to decide when a recorded
#: `HELD_STATUS` has stopped being true — the order is now in a state a pass acts in — so
#: the two surfaces share the pair structurally and no second list can drift from it.
REVIEW_PASS_STATUS = "needs_review"
EARLY_PASS_STATUS = "running"
REVIEW_PASS_STATUSES = (REVIEW_PASS_STATUS, EARLY_PASS_STATUS)

#: `provisional_verdict` values §5 writes and this module reads back. Spelled here so
#: this module stays pure — `project_store.PROVISIONAL_VERDICTS` is the same two words
#: and asserts them at the write.
PROVISIONAL_ACCEPT = "accept"
PROVISIONAL_OBJECT = "object"

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
#: **EVERY ENTRY MATCHES AN ACT, NOT A WORD** (issue #713). `release`, `live`, `drop`,
#: `migrate` and `schema` are everyday vocabulary in a repo that builds a release tool, so
#: bare word-boundary patterns held a large share of ROUTINE assumptions — "on the next
#: release", "words under 3 characters are dropped", "not verified against the live CLI" —
#: and a net that holds the routine ones defeats the feature it guards. Erring wide is
#: still the rule; erring wide on VOCABULARY is not, because it is indistinguishable from
#: the net being off.
#:
#: Word boundaries on both sides: `\bkey\b` must not fire on "monkey", and a substring
#: match would make the list unreadable as the rule it is meant to be.
#: The data objects a destructive verb needs before it is an act rather than a word. ONE
#: list, shared by the wide net's `drop` row and by every tightened destructive verb.
_DATA_OBJECTS = (r"tables?|columns?|databases?|db|indexe?s?|rows?|records?|data|"
                 r"collections?|buckets?|volumes?")
#: Up to two words may sit between the verb and its object, but not a preposition or a
#: conjunction: "dropped from the record" and "deleted when the row is stale" are not acts.
_NOT_A_PREPOSITION = (r"(?:(?!from\b|in\b|into\b|out\b|off\b|on\b|to\b|by\b|for\b|at\b|"
                      r"when\b|during\b|because\b|so\b)\w+\s+){0,2}")

_HS_CREDENTIAL = r"credential|secret|password|\bapi[ -]?key\b|\btoken\b|\bauth\b"
# `live` only as a live THING; `production`/`prod` name it on their own.
_HS_PRODUCTION = (r"\bproduction\b|\bprod\b"
                  r"|\blive\s+(?:credential|key|secret|token|data|database|db|"
                  r"environment|env|traffic|system|server|fleet|user|account|customer|"
                  r"instance|deployment|service)"
                  r"|\bgo(?:es|ing|ne)?\s+live\b")
# `drop` only with a data object AFTER it — "dropped from the record" is not one.
_HS_DESTRUCTIVE = (r"\bdelet|\bdestroy|\btruncat|\birreversib|\bpurge"
                   r"|\bdrop(?:s|ped|ping)?\s+(?:(?:the|a|an|all|this|these|its)\s+)?"
                   + _NOT_A_PREPOSITION + rf"(?:{_DATA_OBJECTS})\b")
# The noun `migration` IS the act. The verb needs its object, and `schema` needs a
# verb: "migrate to the new helper" and "the schema of the reply" are neither.
_HS_SCHEMA = (r"|\bmigrat(?:e|es|ed|ing)\s+(?:(?:the|a|an|all)\s+)?(?:\w+\s+){0,2}"
              r"(?:database|db|schema|tables?|data|rows?|users?|production|prod)\b"
              r"|\bschema\s+(?:change|migration|edit|rewrite)|\bchange\s+the\s+schema\b"
              r"|\balter\s+(?:the\s+)?(?:table|column|schema)|\bALTER\s+TABLE\b"
              r"|\b(?:add|adds|adding|drop|drops|dropping|rename|renames|renaming)\s+"
              r"(?:a|the)\s+column\b")
_HS_MIGRATION = r"\bmigrations?\b|\bbackfill" + _HS_SCHEMA
_HS_MONEY = r"\bbill(ed|ing|s)?\b|\bprice|\bpricing\b|\binvoice|\bcharge[ds]?\b|\bspend"
# Shipping something somewhere — not the noun "the next release".
_HS_SHIPPING_TAIL = (r"|\brelease\w*\s+(?:\S+\s+){0,3}?to\s+(?:prod|production|users?|"
                     r"customers?|the\s+fleet|pypi|npm)\b"
                     r"|\b(?:ship|ships|shipped|shipping|push|pushes|pushed|pushing)\s+"
                     r"(?:\S+\s+){0,3}?to\s+(?:prod|production|users?|customers?|"
                     r"the\s+fleet|main|master|pypi|npm)\b")
_HS_SHIPPING = (r"\bpublish\w*\s+(?:\S+\s+){0,3}?to\b|\bdeploy\w*\s+(?:\S+\s+){0,3}?to\b"
                r"|\b(?:cut|cuts|cutting|ship|ships|shipped|shipping|make|makes|making)"
                r"\s+(?:a|the|another)\s+release\b" + _HS_SHIPPING_TAIL)
_HS_PII = r"\bpii\b|\bgdpr\b|personal data|\bpersonally identifiable"
_HS_LEGAL = r"\blicen[cs]e|\blegal\b|\bcopyright\b"
_HS_BREAKING = r"breaking change|backward(s)? incompatible"

HIGH_STAKES = (
    _HS_CREDENTIAL,
    _HS_PRODUCTION,
    _HS_DESTRUCTIVE,
    _HS_MIGRATION,
    _HS_MONEY,
    _HS_SHIPPING,
    _HS_PII,
    _HS_LEGAL,
    _HS_BREAKING,
)

#: THE ONE CARVE-OUT, and it is a sense of a word rather than a word (Neo, question 562).
#: `token` stays bare — "I hard-coded the token in settings.json" must still be held — but
#: this repo MEASURES ITSELF IN TOKENS, so every assumption about a cache write or a
#: prompt's size tripped the credential row. A match that lies entirely inside one of
#: these spans is not a match. Only `token` has this problem; do not grow the list into a
#: general excuse register.
HIGH_STAKES_SENSE_CARVE_OUTS = (
    r"\b(?:input|output|cached?|cache[ -](?:read|write)|prompt|completion|re-?write|"
    r"total|context|thinking)\s+tokens?\b",
    r"\btokens?\s+(?:count|counts|budget|budgets|cost|costs|usage|economics|spend|"
    r"accounting|limit|limits|per\b|used\b)",
    r"\b\d+[km]?\s+tokens?\b",
)

# -- the tightened net (`validation.stakes_classifier: regex-tightened`) ---------------

#: THE TIGHTENED NET, and every row of it was derived from a MEASURED false positive.
#:
#: The A/B of §3.9 ran on 485 hand-labelled production assumptions. No model arm cleared
#: the bar (recall >= 0.667 at precision > 0.140) and haiku answered the same row two ways
#: on 7.5% of a repeat run, so the recommendation became a tightened regex instead
#: (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md §6-§7).
#:
#: Each row below replaces a WIDE row and names the class of false positives it removes;
#: `evals/tools/score_stakes_regex.py` re-scores both nets and is the only place the
#: corpus is read. THE WIDE NET IS UNCHANGED and stays the default, so this is an opt-in
#: mode rather than a narrowing of what ships.
#:
#: **`production`/`prod` IS DELIBERATELY NOT TIGHTENED — a knowing asymmetry.** It carries
#: 7 of the false positives, and tightening it to acts would clear wo-1ee46481#1, which is
#: the true positive the USER themselves named: its text mentions production rather than
#: acting on it. A rule that loses the one case the user pointed at is not worth 7 rows of
#: precision.
_HST_DESTRUCTIVE_VERBS = (r"delet(?:e|es|ed|ing)|destroy(?:s|ed|ing)?|"
                          r"truncat(?:e|es|ed|ing)|drop(?:s|ped|ping)?|"
                          r"wip(?:e|es|ed|ing)")
#: 19 false positives, every one deleting CODE or a record about code — a test, an
#: assertion, a line of markup, a method, dead code. `truncat` adds 5 more, all clipping
#: text for display. So every destructive verb needs what `drop` already needed: a DATA
#: object, or a live subject. `irreversib` and `purge` stay bare (no false positives).
_HST_DESTRUCTIVE = (rf"\b(?:{_HST_DESTRUCTIVE_VERBS})\s+"
                    r"(?:(?:the|a|an|all|this|these|its|four|every|six)\s+)?"
                    + _NOT_A_PREPOSITION
                    + rf"(?:{_DATA_OBJECTS}|(?:live|production|prod)\s+\w+)\b"
                    r"|\birreversib|\bpurge")
#: A currency amount, or money named as money. 14 false positives spent a ROUND, a SLOT, a
#: WRITER, a CAP, a BUDGET, a concurrency slot — this repo's own units, not the user's.
_HST_MONEY_OBJECT = (r"(?:\$\s?[\d,]+(?:\.\d+)?|\b\d+(?:\.\d+)?\s*(?:usd|dollars?|"
                     r"cents?|eur|gbp)\b|\bdollars?\b|\busd\b|\bmoney\b|"
                     r"\breal\s+api\s+calls?\b)")
#: `bill` is DROPPED, not narrowed: 6 false positives and zero high rows. In this repo it
#: is the name of the cost report and its module (`bill.PAYLOAD_VERSION`, "the window's
#: bill", "still being billed"). `invoice`, `charge`, `price` stay.
_HST_MONEY = (r"\bprice|\bpricing\b|\binvoice|\bcharge[ds]?\b"
              rf"|\bspen(?:d|ds|t|ding)\b[^.;]{{0,40}}?{_HST_MONEY_OBJECT}")
#: 4 false positives are the ADDED_COLUMNS mechanism or a gap left open on purpose ("no
#: migration", "would buy a migration and five store verbs"). The bare NOUN is not the act
#: here: it must be RUN or APPLIED. The verb forms of the wide row are unchanged.
_HST_MIGRATION = (r"\b(?:run|runs|ran|running|appl(?:y|ies|ied|ying)|"
                  r"execut(?:e|es|ed|ing))\s+(?:(?:the|a|an|another|its|one)\s+)?"
                  r"(?:\w+\s+){0,2}(?:migrations?|backfill\w*)\b"
                  r"|\b(?:migrations?|backfill\w*)(?:\s+\w+){0,3}?\s+(?:was|were|is|are|"
                  r"be|been|gets?|got)\s+(?:\w+\s+){0,2}?(?:run|ran|applied|executed)\b"
                  + _HS_SCHEMA)
#: `make/makes/making a release` is 1 false positive ("what makes a release verifiable")
#: and no high rows: `cut` and `ship` are the verbs that ship.
_HST_SHIPPING = (r"\bpublish\w*\s+(?:\S+\s+){0,3}?to\b"
                 r"|\bdeploy\w*\s+(?:\S+\s+){0,3}?to\b"
                 r"|\b(?:cut|cuts|cutting|ship|ships|shipped|shipping)\s+"
                 r"(?:a|the|another)\s+release\b" + _HS_SHIPPING_TAIL)
#: ADDITION 1 of 3, for 4 of the 6 rows the wide net MISSES: settling which version ships.
#: A BARE DOTTED VERSION MUST NOT MATCH — this repo quotes tool versions constantly ("gh
#: 2.86", "CC 2.1.282") and a bare match would be a worse false-positive class than the
#: ones being removed. So each alternative carries the act: a bump, the word `version`, or
#: a tag or release branch naming the number.
_HST_VERSION = (r"\b(?:patch|minor|major)[\s-]?bump\w*"
                r"|\b(?:patch|minor|major)[\s-]?bumps?\s+the\b"
                r"|\bbump(?:s|ed|ing)?\s+(?:\S+\s+){0,3}?v?\d+\.\d+(?:\.\d+)?\b"
                r"|\b(?:release\s+)?version\s+v?\d+\.\d+\.\d+\b"
                r"|\b(?:tag|tags|tagged|tagging)\s+[\w./-]*\d+\.\d+\.\d+"
                r"|\brelease/[\w.-]*\d+\.\d+\.\d+")
#: ADDITION 2: moving a remote ref (`wo-29d99c67#3`, a force-push with a lease).
#: `non-fast-forward` was in the brief and is NOT here: measured, it named a push being
#: REJECTED as one ("my push was rejected as non-fast-forward, and a rebase would have
#: rewritten commits already on the public remote") — a failure being handled, the same
#: shape as the auth carve-out, and it bought no true positive.
_HST_REMOTE_REF = (r"\bforce[- ]?push\w*|--force-with-lease|\bforce[- ]updat\w+"
                   r"|\b(?:updat|mov|repoint|push|reset)\w*\s+(?:only\s+)?"
                   r"(?:(?:the|a|its)\s+)?remote\s+(?:branch|ref|tag|head)\b")
#: ADDITION 3: arming or disarming a privileged-action control (`wo-551f5e8c#1`,
#: four live gate exemptions retracted). `exemption` may NOT stand bare — "the manager
#: exemption in count_active" is a code-level exemption and would be a new false positive.
_HST_GATE_CONTROL = (r"\brule-retract\b"
                     r"|\bretract\w*\s+(?:\S+\s+){0,3}?(?:rules?|exemptions?)\b"
                     r"|\b(?:live|learned|gate)\s+exemptions?\b"
                     r"|\bre-?arm(?:s|ed|ing)?\s+(?:(?:a|the)\s+)?gate")

HIGH_STAKES_TIGHTENED = (
    _HS_CREDENTIAL,
    _HS_PRODUCTION,     # deliberately untightened — see above
    _HST_DESTRUCTIVE,
    _HST_MIGRATION,
    _HST_MONEY,
    _HST_SHIPPING,
    _HS_PII,
    _HS_LEGAL,
    _HS_BREAKING,
    _HST_VERSION,
    _HST_REMOTE_REF,
    _HST_GATE_CONTROL,
)

#: The tightened net's extra senses. SAME KIND AS `HIGH_STAKES_SENSE_CARVE_OUTS` and its
#: docstring's rule still holds — not a general excuse register: the five `token` spans
#: below are the measurement-and-parsing sense that list already carves (a single-token
#: insertion, a zero-token row, a `FORCE_` marker, a token-efficiency request, a JSON
#: token), and none of them is a credential. The other three are a word quoted as a
#: keyword or used in its permission sense: `ON DELETE CASCADE` is SQL in prose, an auth
#: FAILURE is one being handled or rendered rather than a credential touched, and "licence
#: to ship a thin body" is permission, not licensing.
HIGH_STAKES_TIGHTENED_CARVE_OUTS = HIGH_STAKES_SENSE_CARVE_OUTS + (
    r"\b(?:single|zero|one|two|multi|per|no)[- ]token\b",
    r"\btoken[- ]efficien\w*",
    r"\bFORCE_\w*\s+tokens?\b",
    r"\b(?:json|valid|closing|closer|bare|last)\s+tokens?\b",
    r"\bon\s+delete\s+(?:cascade|set\s+null|restrict|no\s+action)\b",
    r"\bauth[\s-](?:failure|failures|error|errors|blocker|blockers|paused)\b",
    r"\bcould\s+not\s+authenticate\b",
    r"\blicen[cs]e\s+to\s+\w+",
)

_HIGH_STAKES_RE = re.compile("|".join(HIGH_STAKES), re.IGNORECASE)
_CARVE_OUT_RE = re.compile("|".join(HIGH_STAKES_SENSE_CARVE_OUTS), re.IGNORECASE)
_HIGH_STAKES_TIGHTENED_RE = re.compile("|".join(HIGH_STAKES_TIGHTENED), re.IGNORECASE)
_CARVE_OUT_TIGHTENED_RE = re.compile("|".join(HIGH_STAKES_TIGHTENED_CARVE_OUTS),
                                     re.IGNORECASE)


def high_stakes_marker(text: str, *, tightened: bool = False) -> str:
    """The high-stakes phrase this assumption contains, or `''`. Pure, no model.

    Returns the matched text rather than a boolean so the hold can SAY what it matched:
    "held — 'production' is a word the OS does not rule on" is actionable, and "high
    stakes" is not.

    `tightened=False` is the SHIPPED net and every existing caller: one matcher, two
    pattern sets, so the two nets can never drift apart in how a match is read or how a
    carve-out is applied. `tightened=True` is `HIGH_STAKES_TIGHTENED`, reached only by
    `validation.stakes_classifier: regex-tightened`.
    """
    text = text or ""
    net = _HIGH_STAKES_TIGHTENED_RE if tightened else _HIGH_STAKES_RE
    carve = _CARVE_OUT_TIGHTENED_RE if tightened else _CARVE_OUT_RE
    carved = [m.span() for m in carve.finditer(text)]
    for m in net.finditer(text):
        start, end = m.span()
        if any(a <= start and end <= b for a, b in carved):
            continue
        return m.group(0)
    return ""


def tightened_verdict(text: str) -> Stakes:
    """The tightened net's answer in the CLASSIFIER's verdict type. Pure, no model.

    `decide`'s condition 7 takes either `stakes=None` (run the wide net here) or a
    `Stakes`, so mode `regex-tightened` needs no third code path in the decision table:
    the daemon hands the tightened net's answer over in the same shape a model's would
    arrive in. `model` says which net ruled, so a hold is never read as a model's ruling —
    `parsed` is True because something DID rule, unlike the two classifier failure paths.

    `category` stays empty: a regex match is a phrase, not one of `stakes.STAKES_CATEGORIES`,
    and inventing one would put a category on the record that nothing decided.
    """
    marker = high_stakes_marker(text, tightened=True)
    if not marker:
        return Stakes(high=False, category="none", reason="", model="regex-tightened")
    return Stakes(high=True, category="",
                  reason=f"its text says {marker!r}", model="regex-tightened")


# -- the second net's second gate: a diff the OS will not copy into a question ---------

#: SECRET-SHAPED EVIDENCE, and it is deliberately NOT part of `HIGH_STAKES`.
#:
#: WHY IT EXISTS: `_confirm_question` interpolates the diff, the diff stat and the result
#: summary, and `neo.ask` PERSISTS that text as a question row `/neo` and `jarvis neo
#: list` display. A diff that adds a credential therefore lands in a store and on a
#: surface the ask pass never put diff content on. kn-deef42ea — a redaction decision is
#: also a filing decision: if the text may not travel, the row must not either, so the
#: hold is the whole answer here and there is no withhold-and-file.
#:
#: **WHY IT IS NARROW WHERE `HIGH_STAKES` IS WIDE, AND THE DIRECTIONS ARE OPPOSITE.**
#: `HIGH_STAKES` reads ONE ASSUMPTION'S SENTENCE, where a false positive costs the user a
#: review action they were making anyway — so it errs wide. This reads A WHOLE DIFF OF
#: THIS REPO, where a false positive holds the confirmation and the wide net would hold
#: nearly every one: "credential", "production" and "delete" are in almost every change
#: this codebase makes. That is the feature switched off, silently, which is the
#: expensive direction here — and it is Neo's own reason (question 593) for refusing to
#: run `high_stakes_marker` over the diff and ruling for this shape instead. There is no
#: redaction in `evidence.py`, `validation.py` or `panel.py` to reuse: the panel sends
#: full diffs to its seat prompts, so this rule is invented here rather than borrowed.
#:
#: Three shapes, and each is a SHAPE rather than a word: a line assigning a real-looking
#: value to a secret-named thing, a key or auth block, and a path that only secrets live
#: at.
SECRET_EVIDENCE = (
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----",
    r"\bssh-(?:rsa|dss|ed25519)\s+AAAA[0-9A-Za-z+/=]+",
    # `["\']?` on BOTH sides of the colon and nowhere else: `"Authorization": "Bearer
    # …"` is the header written as a JSON or dict entry, and the quote before the colon
    # is the only reason the bare pattern missed it. Widening past the quote would start
    # matching prose that merely says the word.
    r"\bAuthorization[\"\']?\s*:\s*[\"\']?(?:Bearer|Basic|Token)\s+\S+",
)

#: What each of `SECRET_EVIDENCE`'s shapes is CALLED on the record. The marker is
#: rendered on the timeline and on `jarvis wo show`, so it names the shape and never
#: quotes the match — see `secret_marker`. The names say no more than the shape because
#: the same scanner reads a diff and a result summary; WHICH of the two carried it is
#: the hold's to say, in `decide_evidence`.
SECRET_EVIDENCE_NAMES = (
    "a private key block",
    "an ssh key line",
    "an Authorization header value",
)

#: A VALUE THAT IS NOT A SECRET, however secret its name. The commonest added line in
#: any repo is the one that names a credential without carrying one — an empty default, a
#: type annotation, a read from the environment, a `changeme` in an example config — and
#: holding on those would be the wide net by another route.
SECRET_PLACEHOLDERS = (
    r"none|null|nil|nan|true|false|str|int|bool|x+|\.+|-+|_+",
    r"todo|tbd|fixme|changeme|change[-_]me|placeholder|redacted|dummy|fake|sample",
    r"example|examples|test|testing|secret|password|passwd|token|key|value",
    r"your[-_].*|my[-_].*|some[-_].*|the[-_].*",
)

#: PATHS ONLY SECRETS LIVE AT. Matched on the stat and the diff HEADERS, never on prose:
#: the path is evidence on its own, and reading the file to find out whether this `.env`
#: really holds anything would be the same mistake one layer down.
SECRET_PATHS = (
    r"(?:^|/)\.env(?:\.[\w.-]+)?$",
    r"\.(?:pem|key|p12|pfx|jks|keystore)$",
    r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$",
    r"(?:^|/)credentials(?:\.[\w-]+)?$",
    r"(?:^|/)\.(?:netrc|npmrc|pypirc|pgpass)$",
    r"(?:^|/)secrets?\.[\w-]+$",
    r"service[-_]account[\w-]*\.json$",
)

#: The words that make an assignment's LEFT SIDE a secret's name, matched against the
#: identifier's PARTS rather than as substrings: `monkey` and `keyword` must not be a
#: `key`, and `AWS_SECRET_ACCESS_KEY` and `apiKey` must both be one.
SECRET_NAME_PARTS = frozenset({
    "key", "keys", "apikey", "accesskey", "privatekey", "secretkey", "seckey",
    "token", "tokens", "authtoken", "secret", "secrets", "clientsecret",
    "password", "passwd", "passphrase", "pwd", "credential", "credentials",
})

#: One line assigning something. Read over a line the CALLER has already vouched for —
#: an added diff line with its `+` stripped, or a line of the result summary — because a
#: removed secret (`-`) is the change doing the right thing and `+++ b/path` is a header.
#:
#: The name may be QUOTED, and that is the whole widening: `"api_key": "sk-live-…"` is
#: the JSON and Python-dict shape, which is how an added credential is most often
#: spelled, and the bare pattern could not match it. A quoted name is still an
#: IDENTIFIER — same charset, same closing quote as the opening one, still the whole
#: name up to the separator — so a regex literal, a comment or a sentence still never
#: parses as one, and the VALUE test below is untouched: `"api_key": ""` and
#: `"api_key": "changeme"` do not fire.
_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?:(?:export|set|const|let|var|readonly)[ \t]+)?"
    r"(?P<quote>[\"\']?)(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P=quote)"
    r"[ \t]*(?:=>|:=|=|:)[ \t]*"
    r"(?P<value>[^\r\n]*?)[ \t]*[,;]?[ \t]*$")

#: The charset a credential is spelled in. Anything else — a space, a bracket, a call, a
#: `+` concatenation — means the right side is an EXPRESSION, and an expression is not a
#: value: `os.environ["API_KEY"]` names a secret and contains none.
_SECRET_VALUE_CHARS = re.compile(r"[A-Za-z0-9+/=._~-]+")

_SECRET_EVIDENCE_RE = [re.compile(p, re.IGNORECASE) for p in SECRET_EVIDENCE]
_SECRET_PATH_RE = [re.compile(p, re.IGNORECASE) for p in SECRET_PATHS]
_PLACEHOLDER_RE = re.compile("|".join(SECRET_PLACEHOLDERS), re.IGNORECASE)
_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _names_a_secret(identifier: str) -> bool:
    parts = [p.lower() for p in _WORD_SPLIT_RE.split(identifier) if p]
    return any(p in SECRET_NAME_PARTS for p in parts)


def _secret_value(raw: str) -> bool:
    """Is this assignment's right side a REAL-LOOKING credential? See `secret_marker`."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if len(value) < 6 or not _SECRET_VALUE_CHARS.fullmatch(value):
        return False
    if _PLACEHOLDER_RE.fullmatch(value):
        return False
    has_digit = any(c.isdigit() for c in value)
    has_alpha = any(c.isalpha() for c in value)
    # A digit beside letters, or sheer length. Neither alone: `evidence_secret` is this
    # module's own constant and `2026-09-23` is a date, and both are added every week.
    return (has_digit and has_alpha) or len(value) >= 20


def _paths(stat: str, diff: str) -> list[str]:
    """Every path the stat and the diff HEADERS name. Prose is not read."""
    found = [line.split("|")[0].strip() for line in (stat or "").splitlines()]
    for line in (diff or "").splitlines():
        if line.startswith(("--- ", "+++ ", "diff --git ", "rename to ", "copy to ")):
            for token in line.split()[1:]:
                found.append(re.sub(r"^[ab]/", "", token))
    return [p for p in found if p and p not in ("a", "b", "/dev/null")]


def secret_marker(stat: str, diff: str) -> str:
    """The secret-shaped thing this evidence carries, NAMED and never quoted. Pure.

    **THE RETURN VALUE IS RENDERED** — on the timeline, in the hold's reason and on
    `jarvis wo show` — so it must never contain the match. Returning the matched text,
    the way `high_stakes_marker` does over one sentence, would move the credential out of
    the question store and into the event store, which is the same defect one table
    along. So: the offending PATH (safe — it is a filename, and the user needs it to know
    which change is being held), or a fixed phrase naming the SHAPE.

    Three nets, cheapest first, and all three read ADDED lines only. A removed secret is
    the change doing the right thing and holding on it would punish the one diff that
    fixes the problem.
    """
    for path in _paths(stat, diff):
        for pattern in _SECRET_PATH_RE:
            if pattern.search(path):
                return path[:200]
    for line in (diff or "").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        marker = _line_marker(line[1:])
        if marker:
            return marker
    return ""


def secret_marker_text(text: str) -> str:
    """The same nets over PLAIN TEXT — the result summary. Named, never quoted. Pure.

    ONE scanner, two callers. The worker's summary is prose it typed, so there is no `+`
    to require and no header to skip; `secret_marker` strips the `+` and everything after
    is this function. Writing the rules twice is how the two copies drift.

    The PATH net is deliberately NOT run here. A path is evidence in a diff — the change
    touched `.env` — but in prose it is a mention, and a summary that says it edited
    `.env.example` is the commonest sentence in this repo's records.
    """
    for line in (text or "").splitlines():
        marker = _line_marker(line)
        if marker:
            return marker
    return ""


def _line_marker(line: str) -> str:
    """One line, no diff marker. The shape it carries, or `""`."""
    for pattern, name in zip(_SECRET_EVIDENCE_RE, SECRET_EVIDENCE_NAMES):
        if pattern.search(line):
            return name
    m = _ASSIGNMENT_RE.match(line)
    if m and _names_a_secret(m.group("name")) and _secret_value(m.group("value")):
        return f"a line assigning {m.group('name')[:60]}"
    return ""


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
    #: The escalated round this hold is about, 0 when no round was named. Carried onto the
    #: `autoreview_held` payload, where it is both the dedupe key and what
    #: `ops._panel_hold_is_stale` compares the CURRENT round against — a hold is a claim
    #: about now, and this is the fact that says whether it still holds.
    round: int = 0


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


#: The panel reason, clamped to one line beside an assumption. `ops.objection_response_
#: line`'s rule, spelled again rather than imported: this module is PURE and pulling `ops`
#: in for a three-line clamp would invert the layering for nothing.
_HINT_CHARS = 120


def _round_hint(round_reason: str) -> str:
    text = " ".join(str(round_reason or "").split())
    return text[:_HINT_CHARS - 3] + "…" if len(text) > _HINT_CHARS else text


def _panel_gave_up(round_n: int, round_reason: str, tail: str,
                   fields: dict[str, Any]) -> Decision:
    """The `panel_gave_up` hold, built once for all three decision functions.

    WHICH ROUND and WHAT IT SAID, because the static sentence cannot tell a real
    disagreement from a reviewer outage whose round reads "nobody could be reached to
    review this submission" (GitHub issue #778).

    THE EXISTING SENTENCE SURVIVES rather than becoming "round N: hint": the prefix
    `ops.assumption_ruling_line` adds is the generic `Held by the OS — `, so a reason
    opening with the round would render a hold that never says WHO held it or why. Both
    additions are omitted when there is nothing to say — `round_n == 0` is every caller
    that names no round, and "round 0" is not a sentence.

    NO COMMAND AND NO URL: this reason is rendered by a pure one-liner on both surfaces,
    so each of them adds its own pointer to the round (`cli._readable_autoreview`, the
    work-order page).
    """
    on_round = f" on round {round_n}" if round_n else ""
    hint = _round_hint(round_reason)
    return _held(HELD_PANEL_GAVE_UP,
                 f"the validation panel gave up{on_round} and put this work order in "
                 f"front of you{f' — {hint}' if hint else ''} — {tail}",
                 round=round_n, **fields)


def decide_evidence(assumption: dict[str, Any], stat: str, diff: str,
                    summary: str = "") -> Decision:
    """May the OS put THIS EVIDENCE into a stored question? PURE — no store, no model.

    **THE SECOND GATE, AND THERE ARE TWO BECAUSE THEY SEE DIFFERENT THINGS.**
    `decide_confirm` is pure over a ROW — an assumption, a work order, a config — and
    cannot see a diff; this is the gate over the EVIDENCE, and it runs after the packet
    is collected and before a single character of it reaches `neo.ask`. Folding it into
    `decide_confirm` would mean handing that function a diff it has no other use for, on
    every call site including the ask pass that has none.

    HELD MEANS NO QUESTION IS FILED. Not "filed with the diff withheld": a confirmation
    with no diff in it is the cheap design Neo refused in question 549, and filing the
    row at all is the filing decision kn-deef42ea says the redaction decision IS. The
    assumption stays pending and is the user's — exactly what happens today on a fleet
    that never switched this on.

    BOTH TEXTS THE QUESTION CARRIES, because `_confirm_question` interpolates the diff
    AND `wo["result_summary"]` — a worker that quotes the credential it wired up puts it
    in the question store by the route the diff was gated on. The hold NAMES which of
    the two it was: the user has to know which text to go and read.

    Carries `assumption_id` and `n` like every other hold, so
    `Daemon._note_autoreview_held` dedupes per assumption instead of writing a line every
    reconcile tick.
    """
    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    marker = secret_marker(stat, diff)
    if marker:
        return _held(HELD_EVIDENCE_SECRET,
                     f"the delivered diff carries {marker}, and the OS will not copy a "
                     f"secret into a stored question — assumption #{n} is yours",
                     **fields)
    marker = secret_marker_text(summary)
    if marker:
        # WHICH TEXT, named: the user has to know where to go and look, and the summary
        # and the diff are two different places.
        return _held(HELD_EVIDENCE_SECRET,
                     f"the work order's result summary carries {marker}, and the OS "
                     f"will not copy a secret into a stored question — assumption #{n} "
                     f"is yours",
                     **fields)
    return Decision(armed=True, code="armed",
                    reason=f"the evidence for assumption #{n} carries nothing "
                           f"secret-shaped",
                    **fields)


def _stakes_hold(n: int, verdict: Stakes, fields: dict[str, Any]) -> Decision:
    """The high-stakes hold as the CLASSIFIER states it.

    `reason` REPLACES the matched phrase (2026-09-25 spec SS3.2). `high_stakes_marker`
    returns matched text rather than a boolean so the hold can say why — "mentions
    'production'" — and under the classifier there is no phrase to name: the line says
    what the OS thinks the worker DID, and the category names which clause of
    `neo.PERSONA` it falls under. On the two failure paths `reason` is
    `stakes.HIGH_UNREACHABLE` or `stakes.HIGH_UNPARSEABLE` and `category` is empty, so a
    hold nobody ruled on never reads like a ruling.
    """
    category = f" ({verdict.category})" if verdict.category else ""
    return _held(HELD_HIGH_STAKES,
                 f"assumption #{n} commits to an act{category}: "
                 f"{verdict.reason or 'no reason given'} — the OS does not decide those "
                 f"for you, whatever it thinks of them", **fields)


def decide(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
           round_outcome: str = "", round_n: int = 0, round_reason: str = "",
           refusal_answered: bool = True, undeclared_delivery: bool = False,
           asked_question_id: int = 0,
           unreachable_question_ids: Collection[int] = (),
           stakes: Stakes | None = None) -> Decision:
    """May the OS decide this assumption right now? PURE — no store, no clock, no model.

    Dicts in, armed-or-held-with-a-reason out, for `automerge.decide`'s reason: the whole
    condition table is then unit-testable without a network, and the safety rule lives in
    one function rather than in a sequence of `if`s spread through a daemon method.

    The seven conditions, all of which must hold, cheapest and most specific first:

    1. the project has opted in AND the panel is on (`cfg.auto_review and cfg.enabled`);
    2. the work order is parked in `needs_review` — the one status that means the user
       owes a decision, and the analogue of `automerge.decide`'s `waiting_pr_merge`. **A
       mid-run assumption is not SETTLED here and this condition is never relaxed**: the
       reviewer would be ruling on an intention, and this function is the only guard on
       the settle path. It is judged PROVISIONALLY instead, by `decide_early`, which
       writes a verdict that settles nothing;
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
    7. nothing in its text is high-stakes. WHICH NET ANSWERS THAT IS THE CALLER'S
       CHOICE: with `stakes=None` — every existing caller, and the shipped `regex` mode
       — this function runs `high_stakes_marker` itself, exactly as it always has. Given
       a `stakes.Stakes`, the classifier's verdict decides instead and the hold carries
       its reason and category (2026-09-25 spec SS3.7). THE FUNCTION STAYS PURE either
       way: the model call is the caller's, in `Daemon._classify_stakes`.

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

    `unreachable_question_ids` — AN ID IN THIS SET IS A QUESTION NOBODY WILL EVER ANSWER:
    its retry ladder is spent and `questions.status` is `failed`, so condition 6's mandate
    of one question per assumption is untouched by excluding it
    (2026-09-26-an-unreachable-neo-question-is-not-a-question-in-flight.md §4). The caller
    derives it — `Daemon._unreachable_question_ids` — because this function is pure.
    """
    if not (getattr(cfg, "enabled", False) and getattr(cfg, "auto_review", False)):
        return _held(HELD_DISABLED,
                     "this project has not given the OS permission to decide its "
                     "assumptions (`validation.auto_review`)")
    status = str(wo.get("status") or "")
    if status != REVIEW_PASS_STATUS:
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
        return _panel_gave_up(round_n, round_reason,
                              "settling its assumptions would answer that for you too",
                              fields)
    if not refusal_answered:
        return _held(HELD_REFUSAL_UNANSWERED,
                     REFUSAL_UNDECLARED_REASON if undeclared_delivery
                     else REFUSAL_UNANSWERED_REASON, **fields)
    asked = int(assumption.get("neo_question_id") or 0)
    if asked and asked != int(asked_question_id or 0) \
            and asked not in unreachable_question_ids:
        return _held(HELD_ASKED,
                     f"assumption #{n} is already with Neo (question {asked})", **fields)
    if stakes is not None:
        # The caller computed the verdict (`Daemon._classify_stakes`) and this function
        # stays PURE. `stakes=None` is today's behaviour and the shipped `regex` mode.
        if stakes.high:
            return _stakes_hold(n, stakes, fields)
    else:
        marker = high_stakes_marker(str(assumption.get("content") or ""))
        if marker:
            return _held(HELD_HIGH_STAKES,
                         f"assumption #{n} mentions {marker!r} — the OS does not decide "
                         f"those for you, whatever it thinks of them", **fields)
    return Decision(armed=True, code="armed",
                    reason=f"assumption #{n} is routine enough to put to Neo", **fields)


def decide_confirm(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
                   round_outcome: str = "", round_n: int = 0, round_reason: str = "",
                   refusal_answered: bool = True,
                   objections_outstanding: bool = False,
                   unreachable_question_ids: Collection[int] = (),
                   confirmation_open: bool = True,
                   stakes: Stakes | None = None) -> Decision:
    """May the OS CONFIRM this early verdict now, at delivery? PURE, like `decide`.

    docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
    §7. A provisional approval is an opinion about an intention; at `needs_review` the
    intention has become a diff and a result summary, and only then may it settle.

    A SETTLED ROW IS HELD BEFORE THE GATES, `decide`'s own clause byte for byte
    (docs/superpowers/specs/2026-10-01-a-confirmation-is-not-re-run-on-a-settled-
    assumption.md §3.2): the `confirm_question_id` gate short-circuits below it, so a
    guard reached only at the tail call never runs on that path.

    Four gates of its own, then **`decide` itself, unchanged and in full**. A provisional
    verdict is not a ticket past any of its seven conditions: the early pass judged an
    intention, so it cannot buy the order past a panel that gave up, a permission the
    project revoked, or the high-stakes net.

    * no `accept` to confirm — `unjudged` (nothing judged it) or `objected`. An early
      `object` approved NOTHING, so there is nothing to confirm and no question is asked
      on one: the user decides it, with the objection in front of them.
    * `confirm_question_id` already set — the confirmation is out. **THIS, AND NOT
      CONDITION 6, IS WHAT KEEPS ONE QUESTION PER ASSUMPTION PER PASS HERE.** It splits
      in two on `confirmation_open` (2026-09-28 spec §6): a question Neo still holds is
      `confirming`, the pass working and suppressed; one that is no longer open is
      `confirm_spent`, the row the user owes a decision on with nothing else saying so.
    * A VALIDATION ROUND THE PANEL HAS NOT FINISHED — `round_open`, this function's
      alone. The ask pass may arm on a pending round (`decide`'s docstring) because it
      judges a sentence the worker wrote; this one interpolates the DIFF, and a round
      still open means that diff may be about to be sent back (2026-09-28 spec §5.2).
      A `round_n` of 0 is no round at all and does NOT hold: a project with validation
      off behaves exactly as it did.
    * an objection still in flight on the work order — §6.6 has not withdrawn it yet.
      Means NOT YET and costs nothing: retried next tick. Without it the two passes race
      on one assumption, one settling it while the other has a message to the worker in
      flight about it.

    `unreachable_question_ids` covers `confirm_question_id` as well as condition 6, and it
    means what it means in `decide`: the id is a question nobody will ever answer, so a
    dead confirmation is re-asked rather than held for ever (2026-09-26 spec §4). It is
    checked BEFORE `confirmation_open`, so a `failed` question is asked again rather than
    reported as spent — an outage is not a decision.

    `confirmation_open` is the caller's fact, derived the way `unreachable_question_ids`
    is (`Daemon._question_liveness`) because this function is pure: open means the
    question's status is one of `neo_store.NEO_HELD_Q_STATUSES`. `escalated` and
    `answered` are NOT open — Neo is finished with it either way. The default is True, so
    every existing caller keeps today's behaviour.

    **`asked_question_id` IS PASSED ON PURPOSE**, and it is the escape hatch `decide`'s
    own docstring documents for condition 6. `neo_question_id` points at the EARLY
    question and always will, so without it every confirmation would hold as "already
    with Neo" and this pass would never run once.
    """
    verdict = str(assumption.get("provisional_verdict") or "")
    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    # 2026-10-01-a-confirmation-is-not-re-run-on-a-settled-assumption.md §3.2.
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
    if not verdict:
        return _held(HELD_UNJUDGED,
                     f"assumption #{n} carries no early verdict — there is nothing to "
                     f"confirm", **fields)
    if verdict != PROVISIONAL_ACCEPT:
        return _held(HELD_OBJECTED,
                     f"Neo objected to assumption #{n} while the work ran — it approved "
                     f"nothing, so there is nothing to confirm and it is yours",
                     **fields)
    confirming = int(assumption.get("confirm_question_id") or 0)
    if confirming and confirming not in unreachable_question_ids:
        if not confirmation_open:
            # 2026-09-28 spec §6: the question id is in the PROSE because the dashboard's
            # link is driven by `os_ruling.neo_question_id` and a hold payload has none.
            return _held(HELD_CONFIRM_SPENT,
                         f"assumption #{n} is yours — the OS asked Neo to confirm its "
                         f"early reading (question {confirming}) and that question is no "
                         f"longer open", **fields)
        return _held(HELD_CONFIRMING,
                     f"assumption #{n} is already with Neo to confirm "
                     f"(question {confirming})", **fields)
    if round_n > 0 and str(round_outcome or "").lower() in _UNRESOLVED_OUTCOMES:
        # 2026-09-28 spec §5.1. The round rides on the decision: it is part of the
        # dedupe key, so the NEXT round's hold is written too.
        return _held(HELD_ROUND_OPEN,
                     f"the validation panel has not finished round {round_n} — the "
                     f"result this confirms against may be about to be sent back",
                     round=round_n, **fields)
    if objections_outstanding:
        return _held(HELD_OBJECTION_IN_FLIGHT,
                     "an objection on this work order has not reached the worker or "
                     "been withdrawn yet — confirming is retried once it has", **fields)
    return decide(assumption, wo, cfg, round_outcome=round_outcome,
                  # The round travels with its outcome: the hold it produces names which
                  # round gave up and quotes what it said.
                  round_n=round_n, round_reason=round_reason,
                  refusal_answered=refusal_answered,
                  asked_question_id=int(assumption.get("neo_question_id") or 0),
                  # Forwarded, not consumed: both gates need it, and an id in it is a
                  # question nobody will ever answer (2026-09-26 spec §4).
                  unreachable_question_ids=unreachable_question_ids,
                  # Forwarded unchanged (2026-09-25 spec SS3.7): the confirmation pass is
                  # gated by the same verdict the ask pass was.
                  stakes=stakes)


def decide_early(assumption: dict[str, Any], wo: dict[str, Any], cfg: Any, *,
                 round_outcome: str = "", round_n: int = 0, round_reason: str = "",
                 refusal_answered: bool = True, undeclared_delivery: bool = False,
                 asked_question_id: int | None = None,
                 unreachable_question_ids: Collection[int] = (),
                 stakes: Stakes | None = None) -> Decision:
    """May the OS put this assumption to Neo WHILE THE WORKER IS STILL TYPING? PURE.

    docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
    §5.1. `decide`'s shape, its nets and its vocabulary — and a separate function, because
    the two guard opposite acts. What this one arms is an ASK whose verdict lands in the
    `provisional_*` columns; what `decide` arms is the settle. Relaxing `decide`'s
    condition 2 instead of writing this would have let a RUNNING work order reach
    `ops.accept_assumption` and land behind it, which is the one failure this feature
    cannot have.

    The conditions, all of which must hold:

    1. the project has opted in AND the panel is on — `decide`'s condition 1 verbatim,
       asserted here as well as in `Daemon.auto_review` for `two-gates-not-a-chain`'s
       reason;
    2. the work order is `running`. Not "is not settled": an order that is `pending`,
       `blocked` or `waiting_pr_merge` has no worker at a keyboard, so an objection to it
       would start a turn nobody asked for — the precise act the parent spec refused;
    3. the assumption is still pending, and
    4. this pass has not already formed a verdict on it (`provisional_verdict`). One
       early verdict per assumption: a second would overwrite what the OS thought first,
       and it is what §7 confirms against;
    5. the PANEL HAS NOT GIVEN UP, 6. no refusal of the user's is outstanding and 7. it is
       not already with Neo, each for `decide`'s reason at the same number;
    8. nothing in its text is high-stakes, by `decide`'s condition 7 and its `stakes`
       argument. **Both nets stay armed in this pass** (§2): net 1 before any reviewer
       call, `read_ruling`'s allowlist on the reply.

    `asked_question_id` is here for `decide`'s reason and is unused by any caller today —
    this pass has no settle site to re-check against, and a second call excluding its own
    question is what §7 does with `confirm_question_id`. It stays in the signature so the
    two functions are called the same way, and `None` and `0` mean the same thing.

    `unreachable_question_ids` is `decide`'s, meaning the same thing — an id in it is a
    question nobody will ever answer — and this pass can reach a dead early link on an
    order that is still `running` (2026-09-26 spec §4).
    """
    if not (getattr(cfg, "enabled", False) and getattr(cfg, "auto_review", False)):
        return _held(HELD_DISABLED,
                     "this project has not given the OS permission to decide its "
                     "assumptions (`validation.auto_review`)")
    status = str(wo.get("status") or "")
    if status != EARLY_PASS_STATUS:
        return _held(HELD_STATUS,
                     f"the work order is {status or 'in no status'}, not running — there "
                     f"is no worker to tell")

    aid = int(assumption.get("id") or 0)
    n = int(assumption.get("n") or 0)
    fields = {"assumption_id": aid, "n": n}
    if str(assumption.get("status") or "") != "pending":
        return _held(HELD_SETTLED,
                     f"assumption #{n} is already {assumption.get('status')}", **fields)
    if str(assumption.get("provisional_verdict") or ""):
        return _held(HELD_JUDGED,
                     f"assumption #{n} already carries an early verdict "
                     f"({assumption.get('provisional_verdict')})", **fields)
    if str(round_outcome or "").lower() == "escalated":
        return _panel_gave_up(round_n, round_reason,
                              "the OS does not rule on its assumptions while it waits "
                              "for you", fields)
    if not refusal_answered:
        return _held(HELD_REFUSAL_UNANSWERED,
                     REFUSAL_UNDECLARED_REASON if undeclared_delivery
                     else REFUSAL_UNANSWERED_REASON, **fields)
    asked = int(assumption.get("neo_question_id") or 0)
    if asked and asked != int(asked_question_id or 0) \
            and asked not in unreachable_question_ids:
        return _held(HELD_ASKED,
                     f"assumption #{n} is already with Neo (question {asked})", **fields)
    if stakes is not None:
        # The caller computed the verdict (`Daemon._classify_stakes`) and this function
        # stays PURE. `stakes=None` is today's behaviour and the shipped `regex` mode.
        if stakes.high:
            return _stakes_hold(n, stakes, fields)
    else:
        marker = high_stakes_marker(str(assumption.get("content") or ""))
        if marker:
            return _held(HELD_HIGH_STAKES,
                         f"assumption #{n} mentions {marker!r} — the OS does not decide "
                         f"those for you, whatever it thinks of them", **fields)
    return Decision(armed=True, code="armed",
                    reason=f"assumption #{n} is routine enough to put to Neo while the "
                           f"worker can still act on it", **fields)


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


def escalation_cause(ruling: Ruling | None = None, *, escalate: bool = False,
                     over_cap: bool = False) -> str:
    """WHY THE OS ESCALATED A QUESTION NEO HAD ALREADY ANSWERED, as one groupable label.

    `neo_store.ESCALATION_CAUSES_OVERRIDDEN` or `""`, and `""` means NOTHING IS WRITTEN:
    a NULL reads "not recorded", and a label that does not match the fact that caused the
    escalation is worse than none — it cannot be told apart from a measured one (Neo,
    question 1161). So every branch here is a fact the caller already holds, and there is
    no "closest member" fallback.

    PURE and in this module because `Ruling` and the stakes vocabulary are, and because
    ONE derivation is what keeps the four daemon call sites from drifting apart (§1 of
    docs/specs/2026-10-01-neo-observability.md, Neo's ruling on question 1170). The members
    are spelled as literals rather than imported: this module depends on no store, and
    `tests/test_autoreview.py` pins what it returns against the enum instead.

    `escalate` is the model's own `escalate` flag: when Neo escalated of its own accord
    the cause is the CHOSEN label off its reply, which `neo.drain_queue` has already
    written, and nothing here may overwrite it. `over_cap` is the plan path, which has no
    `Ruling` at all — the child cap outranks whatever Neo said.

    A `ruling` that ACCEPTED is not an override, so it gets `""`: an escalation that
    follows one (the settle-time re-run dropping the OS's own ruling) was caused by the
    condition table, not by anything in Neo's reply.
    """
    if over_cap:
        return "" if escalate else "scope-over-cap"
    if ruling is None or escalate:
        return ""
    if ruling.overridden:
        if ruling.stakes == STAKES_HIGH:
            return "stakes-high"
        if ruling.stakes == STAKES_UNCLASSIFIED:
            return "stakes-unclassified"
        # `read_ruling`'s third `said` branch: a word that is neither, and not routine.
        return "" if ruling.stakes in ROUTINE_STAKES else "stakes-unreadable"
    # `verdict: deny` with no machine rejection behind it (module docstring).
    return "" if ruling.accept else "neo-denied"


# -- the reviewer ----------------------------------------------------------------------


ASSUMPTION_REVIEWER_PERSONA = """You are Neo, the user's delegate inside the Jarvis \
agentic OS, ruling on ONE assumption a worker recorded.

An assumption is a worker saying: "you did not specify this, I had to decide it, and here
is what I chose." Until now every one of them waited for the user. Your job is to take the
ROUTINE ones off their desk and leave the rest exactly where they are.

YOUR ANSWERS, and which are open to you depends on what the question tells you about
the work order:
- ACCEPT — `{"escalate": false, "verdict": "approve", "stakes": "routine", "reason": "…"}`.
  The worker's call is one the user would have made, or one that costs nothing either way.
- ESCALATE — `{"escalate": true, "verdict": "deny", "stakes": "…", "reason": "…"}`. The
  user decides this one.
- OBJECT — `{"escalate": false, "verdict": "deny", "stakes": "routine", "reason": "…"}`,
  and **only when the question says the work order is STILL RUNNING.** Your `reason` is
  then delivered to that worker as guidance while it is mid-task.

YOU CANNOT REJECT AN ASSUMPTION AND OBJECTING IS NOT REJECTING. Nothing you answer
settles anything by itself: an acceptance on a running order is PROVISIONAL and you will
be asked again once the work exists, and an objection leaves the assumption exactly where
it was — pending, and the user's to decide. What an objection buys is the one thing a
ruling on an intention is good for: the worker finds out now instead of building on it for
the rest of its turn. On a work order that has FINISHED there is no worker to tell, so
your two answers there are accept and escalate, and a disagreement is an escalation
carrying your reading.

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

THE DECISION RECORD IS AUTHORITY AND YOU MAY CITE IT. What it lists has already been
decided on this work order — a question the user or Neo answered, a message the user sent,
an assumption they already ruled on. When it settles the assumption in front of you,
ACCEPT and name the id in your `reason`; escalating to ask for a decision that is quoted
in your own prompt spends a review action the user has already spent. An item marked
`(withheld …)`, `(not answered)` or `(not shown)` is NOT a ruling, and neither is an
omission line: those are cases to escalate, not blanks to fill in.

A SIBLING MARKED `(withheld — high-stakes …)` IS NOT A BLANK TO FILL IN. The OS holds
those assumptions for the user and does not show you their text, deliberately. Do not
guess at what one says, and do not treat its absence as permission: if your ruling would
turn on what it contains, that is precisely a case to ESCALATE.

WHEN IN ANY DOUBT, ESCALATE. The cost of escalating wrongly is one review action the user
was going to make anyway. The cost of accepting wrongly is a decision they never made,
shipped in their name.

RULING ON A RUNNING ORDER MEANS RULING ON AN INTENTION. There is no diff and no result
summary, because the work does not exist yet; the question says so where the summary would
be. Judge what the worker says it decided, and if your ruling would turn on a result you
cannot see, escalate rather than guess.

`reason` is ONE LINE, and WHO READS IT DEPENDS ON YOUR ANSWER. On an acceptance it says
why the call was routine and the user reads it on the record. On an escalation it says
what the user has to decide. **On an objection the WORKER reads it, mid-task** — so write
it to that reader: say what is wrong with the call and what to do instead, in the
imperative, with no preamble and nothing about the machinery that sent it.

An escalation may carry one optional label, which is grouped and reported:
  "cause": "<on an escalation, name the cause from this list; omit it if none fits: \
high-stakes | no-learning-applies | evidence-insufficient>"
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


def decision_record(store: Any, neo: Any, wo_id: str, siblings: list[dict[str, Any]],
                    assumption: dict[str, Any] | None = None, *,
                    chars: int = DEFAULT_VALIDATION_DECISION_RECORD_CHARS) -> str:
    """What has ALREADY been decided on this work order, newest first. `''` when nothing.

    `sibling_line`'s neighbour because it is the same kind of thing — context rows
    rendered into one packet section with a redaction net over them — and the reason it
    lives here rather than in `daemon.py` is that the daemon half of this module runs git
    and calls models, so a string assembled there would be out of reach of the pure tests
    that cover every other line of packet text (GitHub issue #832, §7).

    Three groups, each newest-first: the order's answered Neo Q&A, the user's own
    messages, then the user's rulings on sibling assumptions. Grouped rather than merged
    because provenance differs per group and a merged stream would need a per-item source
    label to stay readable.

    **THE SECRET NET ONLY, NOT THE HIGH-STAKES NET** (§5, on the user's ruling on Neo
    question 943). `sibling_line`'s argument does not transfer: every item here is SETTLED
    BY CONSTRUCTION — an answered question, a message the user sent, an assumption already
    decided — so there is no decision left to withhold, and withholding one reproduces
    #832 one table along. The secret net stays because a credential reaching a model at
    all is true whoever decided what.

    `assumption` is the row being ruled on, and it is a parameter rather than a lookup in
    `siblings` because it is the ONLY row whose citations are resolved
    (`_cited_question_ids`). `None` therefore means no cited items at all.

    `chars` is the project's `validation.decision_record_chars`;
    `catalog.DEFAULT_VALIDATION_DECISION_RECORD_CHARS` is the fleet answer it falls back to
    when the project names nothing.
    """
    # One row past the bound is how the overflow becomes VISIBLE rather than silent (§3).
    answered = list(neo.answered_questions(wo_id, limit=_ANSWERED_ROWS + 1) or [])
    cut_by_rows = max(0, len(answered) - _ANSWERED_ROWS)
    answered = answered[:_ANSWERED_ROWS]
    n_by_question = {}
    for s in siblings:
        for column in ("neo_question_id", "confirm_question_id"):
            if s.get(column):
                n_by_question[int(s[column])] = s.get("n")

    cited_ids = _cited_question_ids(assumption)
    by_id = {int(q["id"]): q for q in answered}
    # Cited FIRST and exempt from the cap: it is the single item most likely to be why
    # this packet exists — Q900 escalated naming a question id it could not see (§4).
    head = [_cited_item(neo, wo_id, qid, by_id.get(qid), n_by_question)
            for qid in cited_ids]

    items = [_question_item(q, n_by_question) for q in answered
             if int(q["id"]) not in cited_ids]
    items += [_message_item(m) for m in (store.user_messages(wo_id) or [])]
    items += [line for s in siblings if (line := _user_ruling_item(s))]

    kept, dropped, spent = [], 0, 0
    for item in items:
        if spent + len(item) + 1 > chars:
            dropped = len(items) - len(kept)
            break
        kept.append(item)
        spent += len(item) + 1
    lines = [*head, *kept]
    if not lines:
        return ""
    if dropped or cut_by_rows:
        # An omission stated, and LAST (kn-1485b845). Going silent is the failure this
        # whole spec is about: a reviewer that cannot tell "no prior decisions" from
        # "decisions I was not shown" must escalate, and would be right to. ONE line
        # whatever the cause — the character cap or the row bound — and "at least" only
        # where it is true: the query returned one row past the bound, so how many older
        # rulings it cut is not known here.
        why = f"the record is capped at {chars} characters"
        if cut_by_rows:
            why += f" and {_ANSWERED_ROWS} answered questions"
        omitted = dropped + cut_by_rows
        lines.append(f"  (… {'at least ' if cut_by_rows else ''}{omitted} older "
                     f"item{'' if omitted == 1 else 's'} omitted — {why})")
    return "\n".join(lines)


def _cited_question_ids(assumption: dict[str, Any] | None) -> list[int]:
    """Question ids the ASSUMPTION BEING RULED ON names, deduplicated, capped.

    **THE RULED ROW ONLY. SIBLINGS ARE NOT SCANNED** (the user's ruling on the recorded
    assumption): a citation is authority the row under review named, and a sibling's
    citation is that sibling's business. The siblings still reach the packet as context
    through `sibling_line` and their user rulings through `_user_ruling_item`; what they do
    not do is spend this budget or pull a question row in behind them.

    `_CITED_QUESTION_RE` OVER-MATCHES ON PURPOSE. Prose like "question 3" becomes a
    citation, and that costs a line saying `(no such question)` — stated rather than
    silent, which is this spec's rule everywhere. `_CITED_LIMIT` bounds one row's prose:
    `content` naming six ids is a different problem.
    """
    found: list[int] = []
    for match in _CITED_QUESTION_RE.finditer(str((assumption or {}).get("content") or "")):
        qid = int(match.group(1))
        if qid not in found:
            found.append(qid)
    return found[:_CITED_LIMIT]


def _cited_item(neo: Any, wo_id: str, qid: int, answered: dict[str, Any] | None,
                n_by_question: dict[int, Any]) -> str:
    """One cited id, resolved. An ANSWERED ruling goes in whole; otherwise it is NAMED.

    `answered` is the row when the record's own fetch returned it, and it is NOT the only
    way a row can be answered: the fetch is bounded (`_ANSWERED_ROWS`), so an older ruling
    arrives here as `None` and its `status` is read off the single-row lookup instead. That
    row is rendered in full exactly like one from the fetch — calling an answered ruling
    "not answered" reproduces #832 on the path this feature exists for, because the persona
    escalates on that line.

    The three remaining unresolved cases are distinct facts and each is stated rather than
    swallowed (§4). A pending question is not authority, and saying it is pending is what
    stops the reviewer reading silence as a ruling. A question on ANOTHER work order is not
    quoted at all: its text has not been through this order's evidence gates, and a worker
    citing it does not make it this order's record. One that never existed is a fact about
    the assumption being ruled on.
    """
    cited = "(cited by the assumption"
    if answered is not None:
        return _question_item(answered, n_by_question, cited=True, truncate=False)
    row = neo.get(qid)
    if row is None:
        return f"  Q{qid} {cited}; no such question)"
    if str(row.get("wo_id") or "") != wo_id:
        return f"  Q{qid} {cited}; belongs to another work order — not shown)"
    if str(row.get("status") or "") == "answered":
        return _question_item(row, n_by_question, cited=True, truncate=False)
    return f"  Q{qid} {cited}; asked on this order, not answered)"


def _question_item(q: dict[str, Any], n_by_question: dict[int, Any], *,
                   cited: bool = False, truncate: bool = True) -> str:
    """One answered question. `answered_by` is verbatim: "the user said" and "Neo said"
    are different authority, and the Q879 escalation turned on not knowing which.

    **AN `assumption`-KIND ROW'S QUESTION TEXT IS NEVER QUOTED.** That row IS a review
    packet — several thousand characters carrying the work order description, the diff and
    the sibling list — so quoting it is recursive and would spend the whole cap on one
    item. It is rendered rather than EXCLUDED because the ruling itself is the payload:
    Q879's missing fact was Neo's verdict on a sibling assumption. Its own question text
    is therefore not the net's payload either: the synthesised headline is all there is to
    classify, and the packet it replaces already passed this order's gates line by line.
    """
    qid = int(q["id"])
    if str(q.get("kind") or "") == QUESTION_KIND:
        n = n_by_question.get(qid)
        headline = raw_headline = (f"assumption #{n}" if n is not None
                                   else "assumption (row not found)")
    else:
        raw_headline = str(q.get("question") or "")
        first_line = raw_headline.strip().splitlines()
        headline = (first_line[0] if first_line else "(no question text)")[:160]
    raw_answer = str(q.get("answer") or "(no answer recorded)")
    answer = " ".join(raw_answer.split())
    if truncate:
        answer = answer[:800]
    tail, raw_tail = "", ""
    if str(q.get("review_status") or "") == "corrected":
        # `neo_store.review` leaves the user's correction as the only ruling that survived.
        raw_tail = str(q.get("review_feedback") or "")
        tail = f" (the user corrected this: {' '.join(raw_tail.split())[:200]})"
    marker = " (cited by the assumption)" if cited else ""
    return _netted(
        f"Q{qid}{marker}",
        f"  Q{qid}{marker} [answered by {q.get('answered_by') or 'unknown'}] "
        f"{headline} -> {answer}{tail}",
        (raw_headline, raw_answer, raw_tail))


def _message_item(m: dict[str, Any]) -> str:
    raw = str(m.get("body") or "")
    stamp = _stamp(m.get("ts"))
    return _netted(f"[user message {stamp}]",
                   f"  [user message {stamp}] {' '.join(raw.split())[:400]}", (raw,))


def _user_ruling_item(s: dict[str, Any]) -> str:
    """A sibling assumption the USER settled, with the reasoning they gave, or `''`.

    `jarvis wo review --feedback` writes that reasoning to `decided_reason` — the
    `assumptions` table has no `review_feedback` column — with `decided_by` naming who
    decided; `ops.assumption_ruling_line` reads the same pair. A row `DECIDER` settled is
    Neo's and reaches the packet through its answered question row instead, never as a
    ruling of the user's.
    """
    status = str(s.get("status") or "")
    if status in ("", "pending") or str(s.get("decided_by") or "") == DECIDER:
        return ""
    raw = str(s.get("decided_reason") or "")
    reason = " ".join(raw.split())[:400]
    return _netted(f"#{s.get('n')} {status}",
                   f"  #{s.get('n')} {status} by the user"
                   f"{' — ' + reason if reason else ''}", (raw,))


def _netted(label: str, rendered: str, payload: tuple[str, ...]) -> str:
    """The secret net over ONE item. Named, never quoted, never dropped.

    The net runs over the PAYLOAD fields rather than the rendered line, because
    `_ASSIGNMENT_RE` anchors at the start of a line and every rendered item begins with
    its own label. The classification is the shape and not the value, which is the
    contract `secret_marker_text`'s docstring states; dropping instead would be the
    silence #832 is about.

    **THE PAYLOAD IS EACH FIELD AS THE USER TYPED IT — newlines included, untruncated.**
    Callers normalise and truncate only the text they RENDER. The same anchor is why:
    flattening the newlines first leaves every line but the first unanchored, so a
    credential assignment on line two is invisible to the net and reaches both the model
    and the question row; truncating first hides one past the limit.
    """
    marker = secret_marker_text("\n".join(payload))
    return rendered if not marker else f"  {label} (withheld — carries {marker})"


def _stamp(ts: Any) -> str:
    """A record timestamp as a date, for ordering the reviewer can read."""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return "unknown time"


#: The decision record's heading, and its empty case. Last of the context blocks on
#: purpose: it is what the reviewer reads just before it answers.
RECORD_HEADING = "# What has already been decided on this work order — authority you may CITE"
NO_RECORD = "  (no prior decisions recorded)"


def _record_block(record: str) -> str:
    return f"{RECORD_HEADING}\n{record or NO_RECORD}"


def _ruling_question(project: str, wo: dict[str, Any], assumption: dict[str, Any],
                     siblings: list[dict[str, Any]], *, early: bool = False,
                     record: str = "") -> str:
    """What the reviewer reads. One assumption, quoted; the rest listed, not ruled on.

    The siblings are here because an assumption is sometimes only defensible given
    another one, and NOT as a list to rule over — the instruction says so twice, and the
    code applies the ruling to exactly one row whatever comes back. Each one goes through
    `sibling_line`, which applies the same high-stakes rule that decided whether it could
    be ruled on at all.

    `record` is what has already been decided on this order (`decision_record`), last of
    the context blocks and the one whose absence produced the escalations in #832. It
    defaults to `""` so a caller not yet teaching one is merely unaware, never wrong.
    """
    n = assumption.get("n")
    others = "\n".join(sibling_line(s) for s in siblings
                       if s["id"] != assumption["id"]) or "  (none)"
    # THE REVIEWER IS TOLD WHICH PASS THIS IS: it decides which answers are open to it
    # (`ASSUMPTION_REVIEWER_PERSONA`) and who reads its `reason`. "(nothing recorded)"
    # where the result belongs would read as a worker that delivered nothing, which is a
    # different and much worse fact than a worker still typing.
    delivered = (
        "# THE WORK ORDER IS STILL RUNNING\nThe worker is mid-task: there is no diff and "
        "no result summary yet, and you are ruling on an intention. Your `reason` reaches "
        "that worker if you object, and an acceptance here is provisional — you will be "
        "asked again once the work exists."
        if early else
        f"# What the worker says it delivered\n"
        f"{(wo.get('result_summary') or '(nothing recorded)')[:1500]}")
    answers = (
        "Answer with `escalate`, `verdict` (`approve` to accept it, `deny` to OBJECT — "
        "your `reason` goes to the worker), `stakes` (`routine` or `high`) and a one-line "
        "`reason`. Set `escalate` true to leave it to the user instead."
        if early else
        "Answer with `escalate`, `verdict` (`approve` to accept it, `deny` to send it to "
        "the user), `stakes` (`routine` or `high`) and a one-line `reason`.")
    answers += (" When the decision record above settles it, say which id in `reason` "
                "instead of escalating to ask for it again.")
    return "\n\n".join([
        f"ASSUMPTION REVIEW — rule on assumption #{n} of {wo['id']} in {project}, and on "
        f"nothing else.",
        f"# The assumption\n{assumption.get('content') or '(empty)'}",
        f"# The work order it was recorded against\n{wo.get('title') or '(untitled)'}\n"
        f"{(wo.get('description') or '')[:2000]}",
        delivered,
        f"# The work order's other assumptions, for context only — do not rule on these\n"
        f"{others}",
        _record_block(record),
        answers,
    ])


#: How many dropped paths the truncation marker NAMES. The COUNT is always exact — spec
#: docs/superpowers/specs/2026-09-26-bounded-model-inputs.md § 2.
CONFIRM_DROPPED_FILES_SHOWN = 10

#: The per-field cuts this question makes, named instead of spelled inline — the limit
#: discipline of `gates.build_request_question` (spec § 2).
CONFIRM_DESCRIPTION_CHARS = 2000
CONFIRM_SUMMARY_CHARS = 1500

#: `supervisor.build_evidence`'s closing sentence, reused verbatim for the same purpose.
CANNOT_SEE = "Escalate rather than judge on what you cannot see."

#: The two `EvidencePacket.source` values, whose `head` means different things.
SOURCE_WORKTREE = "worktree"
SOURCE_PULL_REQUEST = "pull_request"


@dataclass(frozen=True)
class ConfirmEvidence:
    """The delivered change as ONE confirmation question carries it.

    The packet trimmed to `validation.confirm_diff_chars`, plus what the trim removed:
    a diff the reviewer is not told was cut is how it confirms a change it never saw.

    `full_chars` is the length of the diff the COLLECTION handed over, and
    `collect_truncated` says that collection was itself bounded — so the total is not
    knowable and the marker must not state one (Neo, question 945).
    """

    stat: str = ""
    diff: str = ""
    files: tuple[str, ...] = ()
    diff_truncated: bool = False
    dropped_files: tuple[str, ...] = ()
    pr_url: str = ""
    source: str = ""
    head: str = ""
    full_chars: int = 0
    collect_truncated: bool = False
    #: The collection bound, carried so the marker can name it without this module
    #: holding a number the daemon owns.
    collect_limit: int = 0

    def what_changed(self) -> str:
        """The EXACT `# What changed` block `_confirm_question` interpolates.

        ONE renderer, called twice — by the question and by the daemon's
        `decide_evidence` call — so the text scanned and the text sent cannot drift:
        docs/superpowers/specs/2026-09-26-bounded-model-inputs.md § 2, every byte that is
        persisted or sent has been through the net.
        """
        return _what_changed(self)


def confirm_evidence(packet: Any, assumption: dict[str, Any], limit: int, *,
                     collect_limit: int = 0) -> ConfirmEvidence:
    """Trim one collected packet for one assumption. PURE — no git, no store, no model.

    **THE HUNKS THE ASSUMPTION NAMES GO FIRST.** An assumption that mentions a path is
    confirmed against that path, and spending the budget in diff order drops exactly the
    file the question is about. Where it names none the collection's order stands.

    `None` is the collector having failed or there being nothing, and it answers an empty
    instance: AN EMPTY DIFF STILL ASKS (`Daemon._confirmation_evidence`).
    """
    from . import evidence

    if packet is None:
        return ConfirmEvidence(collect_limit=collect_limit)
    files = tuple(packet.files or ())
    full = str(packet.diff or "")
    content = str(assumption.get("content") or "").lower()
    named: list[str] = []
    rest: list[str] = []
    for new, old, text in evidence._sections(full):
        path = new or old
        (named if path in files and _names_path(content, path) else rest).append(text)
    kept, cut, dropped = evidence._truncate(
        "".join(named + rest) if named else full, limit, files)
    return ConfirmEvidence(
        stat=str(packet.stat or ""), diff=kept, files=files,
        diff_truncated=bool(cut or packet.diff_truncated),
        dropped_files=tuple(dict.fromkeys(tuple(packet.dropped_files or ())
                                          + tuple(dropped))),
        pr_url=str(packet.pr_url or ""), source=str(packet.source or ""),
        head=str(packet.head or ""), full_chars=len(full),
        collect_truncated=bool(packet.diff_truncated), collect_limit=collect_limit)


def _names_path(content: str, path: str) -> bool:
    """Does this text name that file — by its full path or by its basename?"""
    lower = path.lower()
    return lower in content or lower.rsplit("/", 1)[-1] in content


def _truncation_marker(ev: ConfirmEvidence) -> str:
    """What was cut, in one line: the kept size, the TOTAL, the count and the names."""
    total = (f"more than {ev.collect_limit:,}" if ev.collect_truncated
             else f"{ev.full_chars:,}")
    shown = ev.dropped_files[:CONFIRM_DROPPED_FILES_SHOWN]
    more = len(ev.dropped_files) - len(shown)
    named = ", ".join(shown) + (f", and {more} more" if more else "")
    which = (f"{len(ev.dropped_files)} file(s) not shown: {named}" if shown
             else "no whole file dropped")
    return (f"[diff truncated — {len(ev.diff):,} of {total} chars; {which}; "
            f"full diff: {ev.pr_url or '(no pull request)'}]")


def _reference(ev: ConfirmEvidence) -> str:
    """The reference, which rides truncated or not (Neo, question 753).

    A sha as a sha and a branch as a branch: `head` is a HEAD sha on the worktree path
    and GitHub's `headRefName` on the pull-request path (evidence.py's field comment).
    """
    if ev.head and ev.source == SOURCE_WORKTREE:
        head = f"head sha: {ev.head}"
    elif ev.head and ev.source == SOURCE_PULL_REQUEST:
        head = f"head branch: {ev.head}"
    else:
        head = "head: (unknown)"
    return f"pull request: {ev.pr_url or '(none)'}\n{head}"


def _what_changed(ev: ConfirmEvidence) -> str:
    """The full stat, the full file list, the kept hunks, what was cut, the reference."""
    from . import provenance

    stat = (provenance.borrowed_block(provenance.Borrowed(
        label="the `git diff --stat` of the delivered change", whose="git",
        # FULL: the stat is what a reviewer reads when no hunk survived the budget.
        text=ev.stat, limit=len(ev.stat))) if ev.stat else "(no files reported)")
    paths = "\n".join(f"  {f}" for f in ev.files)
    listing = ("changed files, all of them — this list is never truncated:\n"
               + provenance.borrowed_block(provenance.Borrowed(
                   label="the changed-file list of the delivered change", whose="git",
                   # FULL: evidence.py's rule 3 — `files` is truncated at no limit.
                   text=paths, limit=len(paths))) if ev.files
               else "changed files: (none reported)")
    diff = (provenance.borrowed_block(provenance.Borrowed(
        label="the delivered diff", whose="the worker of this work order",
        # Bounded upstream by `confirm_diff_chars` at a FILE BOUNDARY: a blind character
        # limit here would re-cut it mid-hunk.
        text=ev.diff, limit=len(ev.diff))) if ev.diff else "(no diff)")
    parts = ["# What changed", stat, listing, diff]
    if ev.diff_truncated:
        parts += [f"{_truncation_marker(ev)}\n{CANNOT_SEE}"]
    parts.append(_reference(ev))
    return "\n\n".join(parts)


def _confirm_question(project: str, wo: dict[str, Any], assumption: dict[str, Any],
                      siblings: list[dict[str, Any]], ev: ConfirmEvidence,
                      record: str = "") -> str:
    """What the reviewer reads at DELIVERY. Everything the early pass could not have.

    Each block earns its place, and the two new ones are the whole point of the second
    call (Neo, question 549):

    * **the provisional verdict, its reason and its model, labelled as mid-turn.** The
      reviewer is being asked to confirm a READING, not to rule from scratch, and one
      formed with no diff in front of it is evidence rather than authority — saying so is
      what stops the earlier line being read as a decision already taken.
    * **the diff stat and the diff** (trimmed by `confirm_evidence` to
      `validation.confirm_diff_chars`, with a marker naming what was cut),
      and the result summary. This is the fact that did not exist when the assumption was
      an intention, and confirming without it would be the cheap design Neo refused.
      **`decide_evidence` HAS ALREADY PASSED BOTH**, because `neo.ask` persists
      everything below as a question row: neither a diff nor a summary carrying a secret
      reaches here.

    The rest mirrors `_ruling_question` deliberately: the assumption quoted, the work
    order's title and description, and the siblings through `sibling_line` — the
    high-stakes net applies to the context list here exactly as it does there, because
    the net is about text reaching a model, not about which pass is asking.

    The ANSWER SHAPE is `_ruling_question`'s, unchanged, so `read_ruling` reads this
    reply with both nets armed and no second parser exists to disagree with it.
    """
    n = assumption.get("n")
    others = "\n".join(sibling_line(s) for s in siblings
                       if s["id"] != assumption["id"]) or "  (none)"
    return "\n\n".join([
        f"ASSUMPTION REVIEW — CONFIRM an earlier reading of assumption #{n} of "
        f"{wo['id']} in {project}, against the result that has now been delivered, and "
        f"rule on nothing else.",
        f"# The assumption\n{assumption.get('content') or '(empty)'}",
        f"# The reading formed WHILE THE WORKER WAS STILL TYPING\n"
        f"verdict: {assumption.get('provisional_verdict') or '(none)'} "
        f"(model: {assumption.get('provisional_model') or 'unknown'})\n"
        f"{assumption.get('provisional_reason') or '(no reason recorded)'}\n"
        f"That reading had NO diff and NO result summary in front of it. You do.",
        f"# The work order it was recorded against\n{wo.get('title') or '(untitled)'}\n"
        f"{(wo.get('description') or '')[:CONFIRM_DESCRIPTION_CHARS]}",
        f"# What the worker says it delivered\n"
        f"{(wo.get('result_summary') or '(nothing recorded)')[:CONFIRM_SUMMARY_CHARS]}",
        _what_changed(ev),
        f"# The work order's other assumptions, for context only — do not rule on these\n"
        f"{others}",
        _record_block(record),
        "You are CONFIRMING that earlier reading against the delivered result. "
        "Confirming settles this assumption in the user's name; anything else leaves it "
        "with them, carrying both readings.",
        "Answer with `escalate`, `verdict` (`approve` to confirm it, `deny` to send it "
        "to the user), `stakes` (`routine` or `high`) and a one-line `reason`. When the "
        "decision record above settles it, say which id in `reason` instead of "
        "escalating to ask for it again.",
    ])


def _record_chars(cfg: Any) -> int:
    """This project's `validation.decision_record_chars`, or the fleet default with no
    `cfg` — the pure tests and every other caller that has no catalog in hand."""
    return int(getattr(cfg, "decision_record_chars",
                       DEFAULT_VALIDATION_DECISION_RECORD_CHARS))


def propose_confirmation(store: Any, neo: Any, project: str, wo: dict[str, Any],
                         assumption: dict[str, Any], siblings: list[dict[str, Any]],
                         *, evidence: ConfirmEvidence | None = None,
                         cfg: Any = None) -> dict[str, Any]:
    """Put ONE already-judged assumption back to Neo at delivery. Returns the question.

    `propose`'s mirror, and the differences are the two that matter: the link is
    `confirm_question_id` — `neo_question_id` already points at the early question and
    overwriting it would lose which reading came from where — and the `autoreview_asked`
    payload carries `confirm: True`.

    **THE PASS IS WRITTEN DOWN AT ASK TIME** (kn-e29d10fe). Re-deriving later from the
    row ("it has a provisional verdict, so this must have been the confirmation") reads a
    column that keeps changing under it. §5 writes `early` into the same payload field
    for the same reason.

    Reuses `QUESTION_KIND` and `ASSUMPTION_REVIEWER_PERSONA`: the persona is per KIND
    (`neo.py:180`), and a new kind is seven edits in other people's modules
    (kn-4edb0eb7).
    """
    question = neo.ask(project, wo["id"],
                       _confirm_question(project, wo, assumption, siblings,
                                         evidence or ConfirmEvidence(),
                                         decision_record(
                                             store, neo, wo["id"], siblings, assumption,
                                             chars=_record_chars(cfg))),
                       context=f"{wo.get('title') or ''}\n"
                               f"{(wo.get('description') or '')[:800]}",
                       kind=QUESTION_KIND)
    store.link_assumption_confirmation(assumption["id"], question["id"])
    store.add_event(wo["id"], "autoreview_asked", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "neo_question_id": question["id"], "confirm": True})
    log.info("auto-review asked Neo to confirm assumption #%s of %s as question %s",
             assumption.get("n"), wo["id"], question["id"])
    return question


def propose(store: Any, neo: Any, project: str, wo: dict[str, Any],
            assumption: dict[str, Any], siblings: list[dict[str, Any]], *,
            early: bool = False, cfg: Any = None) -> dict[str, Any]:
    """Put ONE assumption to Neo. Returns the question row.

    Idempotency is `assumptions.neo_question_id` and it is checked in `decide` (condition
    6), not here, so that "already asked" is a hold with a reason like every other rather
    than a silent `None` — one question per assumption, for its whole life. A question
    that Neo escalated is therefore never re-asked: the user holds it, and asking again
    every reconcile tick would be the OS lobbying them.

    **`early` IS WRITTEN ON THE EVENT, AND THAT IS HOW THE VERDICT IS ROUTED BACK.** The
    ruling arrives minutes later through the Neo drain, by which time the work order may
    already have left `running` — and the two passes mean opposite things: an early ruling
    may only be recorded provisionally, a parked one settles. Live status cannot tell them
    apart after that race, `kind` is shared deliberately (a new one is seven edits) and
    neither pass may have a column of its own, so which pass asked is written down where
    it is known for certain: here. `Daemon._asked_early` reads it back.
    """
    question = neo.ask(project, wo["id"],
                       _ruling_question(
                           project, wo, assumption, siblings, early=early,
                           record=decision_record(store, neo, wo["id"], siblings,
                                                  assumption,
                                                  chars=_record_chars(cfg))),
                       context=f"{wo.get('title') or ''}\n"
                               f"{(wo.get('description') or '')[:800]}",
                       kind=QUESTION_KIND)
    store.link_assumption_question(assumption["id"], question["id"])
    store.add_event(wo["id"], "autoreview_asked", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "neo_question_id": question["id"], "early": early})
    log.info("auto-review asked Neo about assumption #%s of %s as question %s",
             assumption.get("n"), wo["id"], question["id"])
    return question
