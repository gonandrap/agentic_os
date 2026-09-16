# The panel blocks on blockers, and files the rest as follow-ups

Feature order `fo-cdc193d9`, project `jarvis_os`.

> "llm answers always have a follow up. […] If that translates into PR reviews, we will never
> get approvals, regardless the number of rounds. Or we will need 10 rounds to approve a 100
> lines of code changes. Findings on reviews should only flag blockers or high important
> issues with the changes, smaller changes can be filed as follow up tickets."
> — the user, 2026-09-15

The unit of judgement becomes **"does this BLOCK"**, not "is there anything to say". A
finding that is not a blocker stops being an argument and becomes a ticket.

---

## 1. The problem, measured

On 2026-09-15 six work orders in `jarvis_os` were simultaneously stuck in `needs_review`,
every one of them escalated after exhausting `validation.max_rounds`, and none of them for a
defect anybody disputes. The chair's own closing words across them: *"Rejecting on three
concrete, fixable points; the design and the round-3 resolver collapse are right"*, *"Your
change is right where it matters — one thing must change"*, *"Three concrete fixes, then this
is good to go."* Work that is substantially accepted cannot land.

`wo-37024277` is the signature that proves this is not convergence. Rejected in rounds 1, 2,
3 and 5, with **different findings every round** — round 1: two properties should return
`None` rather than `0.0`, an inbox title should name its cause, a test is misnamed; round 3:
a module import edge points the wrong way; round 5: nothing pins the tick wiring. Each round
the worker fixed what was asked and each round a fresh crop appeared. **No finding recurred.**
A loop whose findings do not repeat is not approaching a fixed point.

Raising `max_rounds` was considered and **rejected**: more rounds is more laps of the same
treadmill. Three mechanisms produce it.

**1.1 The chair rejects on non-blocking findings by instruction.**
`src/jarvis/assets/validator-seats/chair.md` says of the architect and maintainer seats —
which *cannot block by design* — "A concrete, actionable finding from either is reason enough
to reject; a matter of preference is not." An LLM reviewer can *always* produce a concrete,
actionable finding. Under that rule rejection is the fixed point, not passing. The
counterweight in the same file ("Small findings that nobody would act on do not justify a
round trip") is losing, every time, in production.

**1.2 Every round re-reads the whole change.** `evidence.collect_work_order` builds the
packet from the full `base...HEAD` diff on every round; there is no delta and no prior-round
logic in `src/jarvis/evidence.py`. Round 5 re-examines code rounds 1–4 already accepted, and
re-examined code yields new remarks.

**This one is not fixed here, and that is a decision, not an omission.** §5.3 argues it: a
delta-judged pass would authorise an automatic merge of code no seat read, and would break
`evidence.fingerprint`'s repeat-submission guard in the dangerous direction. The cure for 1.2
is 1.3 — a seat that can see what was already settled does not need a smaller diff.

**1.3 The seats are blind to the rounds before them.** Nothing in `src/jarvis/seats.py` or
`validation.decide` passes prior rounds' findings, or their resolutions, into a seat prompt.
A seat cannot know a point was already raised and settled, or that the thing it is about to
object to was an explicit instruction from round 2. The only carry-over is the submitter's
own `--evidence` prose — the party with the least standing to say "this was already agreed".

**Why this is worth doing properly.** `validation.auto_merge` is ON for `jarvis_os`, so once
a round passes the OS merges the pull request itself. This panel is the last thing standing
between a finished work order and an autonomous landing.

---

## 2. What already exists (so this design adds as little as possible)

| Piece | Where |
|---|---|
| the 5-seat roster, blind, in parallel | `src/jarvis/seats.py` — `Roster`, `Opinion`, `run_blind`, `prime_cache` |
| the validation panel | `src/jarvis/validation.py` — `build_shared_prefix`, `build_packet_prompt`, `build_seat_prompt`, `build_chair_prompt`, `_reply`/`_raised`/`_asks`/`_message`, `arbitrate`, `decide`, `_out` |
| the seat mandates | `src/jarvis/assets/validator-seats/{tester,security,architect,maintainer,chair}.md` |
| the veto table, in code | `validation.VETO_SEATS = ("security", "tester")`, applied by `validation.arbitrate` |
| the submission | `src/jarvis/evidence.py` — `EvidencePacket`, `collect_work_order`, `collect_feature`, `fingerprint`, `judged_head`, `_normalise` |
| the round machine | `src/jarvis/daemon.py` — `validation_tick`, `_validate_work_order`, `_validate_feature`, `_reject`, `_escalate`, `_preceding_round`, `_round_config` |
| packet construction, store side | `ops.submit_for_validation`, `ops.submit_feature_for_validation`, `ops.collect_feature_evidence`, `ops.side_effects_of` |
| storage | `project_store.validation_rounds` / `validation_opinions`, `record_validation_opinion`, `validation_opinions()`, `VALIDATION_VERDICTS` |
| the backlog | `central_store.add_backlog` / `list_backlog` — the row already carries `origin_wo_id`, `origin_fo_id`, `origin_note` |
| config | `catalog.ValidationConfig`, resolved per project by `catalog._parse_validation` |
| surfaces | `ops.round_line`, `ops.validation_rounds`, `ops.validation_detail`, `cli.py` (`wo show`, `fo show`, `validation show`), `ui/templates/_validation.html` |
| the measurement | `evals/llm/test_validation_judgment.py`, `evals/llm/validation_baseline.json`, `tests/test_validation_eval_harness.py` |

Today a seat answers with **one** verdict and **one** flat bag of asks:

```json
{"verdict": "reject", "blocking": false, "reason": "<prose>", "asks": ["…", "…"]}
```

`arbitrate` reads `blocking` from `security` and `tester` only. Everything else — every
architect and maintainer finding, and every non-blocking tester or security concern — is
prose handed to the chair, which is instructed that a concrete one is reason enough to
reject. **There is no representation of severity anywhere in the system.** That is the hole
this feature fills.

### 2.1 The seats classify. Neo does not.

The user's words were "can ask Neo about that". This design has the seats self-classify.
Four reasons, in order of weight:

1. **Cost, on the fleet's hottest loop.** A round is five calls today. Neo classification is
   at least one more model round per round, on every unit, up to `max_rounds` times — and
   Neo's queue is a single FIFO drained on one daemon thread (`neo.drain_queue`), so it is
   latency as well as spend. The seats already emit a machine-read `blocking` flag; asking
   them for a severity per finding costs **zero** additional calls.
2. **`validation.py` may not import `neo`, `neo_store`, `panel` or `bus`** — pinned by an AST
   walk in `tests/test_validation_seats.py`, and deliberately: `neo_store.learnings` is one
   OS-wide table whose seat vocabulary also contains `chair`, so a ruling the user taught
   Neo's chair would silently start steering validation verdicts. Routing through Neo needs a
   new seam and a new question kind — seven edits (`kn-4edb0eb7`), not four.
3. **The classifier needs the diff.** Neo would have to be handed the packet to tell a blocker
   from a nit, re-reading a change a seat has just read. The seat already holds the context.
4. **It is checkable.** A severity is a machine-read word in a stored reply, and §6 grades it
   directly. A Neo ruling is a second opinion that would itself need grading.

Nothing here touches Neo's review of plans, gate requests or escalations. If the seats'
classification proves unreliable, escalating to Neo is a later feature and the `severity`
field is the seam it would attach to.

### 2.2 The schema is ADDITIVE, and this is the most important call in the feature

`verdict`, `blocking`, `reason` and `asks` keep today's names, today's semantics and today's
readers. `findings` is **added beside them**. A seat that omits `findings` degrades to exactly
today's behaviour rather than to silence.

This is not compatibility politeness. A replacement schema would:

* **break the veto path's message to the submitter.** `arbitrate` builds a rejected round's
  text with `_message(str(data.get("reason") or ""), _asks(data))`. A blocking tester emitting
  only `findings` yields an empty `reason`, and the submitter reads
  `validation.UNSTATED_REJECTION` — *"the seat that refused it did not say why."*
* **force edits to the safety suite.** `tests/test_validation_arbitrate.py` is 24 tests over
  the veto table, including an AST walk proving `arbitrate` has exactly one non-`None` return
  and that its outcome is `"rejected"`. The veto path was never the rejection loop; spending
  risk there to fix the loop is spending it on the wrong half of the system. **That file must
  stay green with zero edits.**
* **invalidate a paid measurement.** `evals/llm/validation_baseline.json` is a *record of a
  past run*, held to its schema by `tests/test_validation_eval_harness.py`. Its 84 `blocking`
  keys cannot be regenerated without spending again.
* **leave every stored `validation_opinions.reply` in an unreadable shape.** §5 reads those
  rows back.

So: **`findings` absent or unparseable ⇒ `[]`**, and every consumer reads both shapes.

---

## 3. The severity split: what a finding is, and what it may force

**One work order.** The seat reply schema, all five mandates, the pure helpers, the chair's
prompt, and `decide`'s contract key. Needs nothing; runs in parallel with §5.

This section is one work order and not two because the two halves are unshippable apart: a
schema that lands without the chair change leaves a chair reading a shape its mandate does not
describe, and the two halves edit adjacent functions in `validation.py` and the same
`tests/test_validation_seats.py`, which parametrises over the non-chair seats *and* pins the
chair's prose.

### 3.1 The schema

A non-chair seat's reply gains one key, beside the four it already has (§2.2):

```json
{
  "verdict": "reject",
  "blocking": false,
  "reason": "<prose, addressed to the submitter>",
  "asks": ["<a concrete thing to change>"],
  "findings": [
    {"severity": "blocker",   "title": "<one line>", "detail": "<what is wrong and what would satisfy it>"},
    {"severity": "follow_up", "title": "<one line>", "detail": "<…>"}
  ]
}
```

* **Any `severity` that is not exactly `"blocker"` is read as `follow_up`.** Note the
  direction: this is the mirror of `validation._raised`'s permissive `bool()` and it points
  the *opposite* way, because this flag points *away* from a rejection. A malformed severity
  must fail toward filing, never toward rejecting — the failure this feature exists to remove.
  A `findings` key that is absent, not a list, or unparseable is `[]`.
* `title` becomes a backlog item's title: under ~100 characters, naming the file or symbol.
* `detail` becomes that item's description, and on a blocker it is what the chair reads.
* `reason` and `asks` are unchanged and stay the veto seat's own words to the submitter.

### 3.2 What each seat is told, and where

The definition of a blocker ships in every non-chair mandate **in the same words**, placed
adjacent to the OUTPUT section — `kn-abb7356b` measured that models attend to instructions
next to the output format, and that the same fix stated elsewhere in the file did not take:

> **A finding is a `blocker` only if the work is not fit to ship without it.** A defect that
> produces a wrong result, a missing test for behaviour this change introduces, an exposure, a
> contradiction of a standing instruction of this project, a claim in the evidence the diff
> does not support, or a wrong assumption embodied in the code. **Everything else is a
> `follow_up`, including everything you would merely have written differently.** A follow-up
> is not a lesser finding and it is not discarded: it is filed as a ticket against this
> project, in your words, and the work lands. **If you are weighing whether something is worth
> a round trip, that weighing is itself the answer: it is a follow-up.**

**Stating the default is load-bearing.** An LLM asked to classify with no stated default
classifies toward the graver label — which is the production defect in a new costume.

Per seat:

* **`tester.md` / `security.md`** (veto holders). Their existing "you may reject WITHOUT
  blocking" paragraph becomes: a concern you would not stop the work over is a `follow_up`
  finding, filed rather than argued. Set `blocking` when and only when you have written at
  least one `blocker` finding. Their veto itself is untouched (§7).
* **`architect.md` / `maintainer.md`** (no veto). These two are where the treadmill lives.
  They gain the counterpart of the rule the chair is losing: you may write a `blocker` and the
  chair will weigh it, but yours is the seat whose failure mode is an expensive rejection loop,
  and a structural remark the next person could act on next week is a follow-up.

### 3.3 The pure helpers

In `validation.py`, beside `arbitrate`, and pure in the same sense — plain dicts in, plain
data out, no store, no model, no clock:

```python
def findings(reply: Mapping[str, Any]) -> list[dict[str, str]]:
    """One seat reply's findings, normalised. `[]` when the key is absent or unusable."""

def blockers(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
def follow_ups(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
```

They take the `{"seat", "status", "reply"}` shape `arbitrate` already takes — the shape of a
stored `validation_opinions` row — so the same split replays over the record, which is what
§4 and §5 both need.

### 3.4 What a finding may force

| who raised it | severity | what it forces |
|---|---|---|
| `security` / `tester` with `blocking` | any | **rejected**, in code, chair not run — unchanged |
| `security` / `tester` without `blocking` | `blocker` | the chair weighs it |
| `architect` / `maintainer` | `blocker` | the chair weighs it — unchanged in mechanism |
| **any seat** | **`follow_up`** | **nothing. Filed (§4), never argued, never shown to the chair** |

**For a veto seat, `blocking` is authoritative and severity is ignored.** `blocking: true`
with every finding classified `follow_up` is a seat contradicting itself, and the flag wins.
That is enforced by `arbitrate` being untouched: it reads `blocking` and never `findings`.

`arbitrate` and `VETO_SEATS` are **not edited by this work order**, and neither is
`tests/test_validation_arbitrate.py`. A worker that finds itself in either file has taken a
wrong turn.

### 3.5 The chair does not see follow-ups

`build_chair_prompt` today interpolates each seat's reply with `op.raw.strip()` — verbatim.
That is what feeds mechanism 1.1: the chair reads twelve nits and is told a concrete one is
reason enough.

It changes to render, per seat: `verdict`, `reason`, `asks`, and **only that seat's `blocker`
findings** — each quoted unchanged, because a summariser between the seats and the chair is
one more place a concrete ask gets softened. In place of the follow-ups, one line:

> `N` further findings across this panel were classified as follow-ups by the seats that raised
> them. They have been filed as tickets against this project and are not before you. You may
> not reject over them, and you are not being shown them.

**Absence is the only enforcement there is.** The chair emits `{"outcome": "rejected"}` freely
and nothing in code stops it rejecting over something it can see, so showing it follow-up
*titles* would not be a weaker version of this design — it would be no design. That is
`arbitrate`'s own argument about itself: a safety rule that lives in prose is a rule that holds
by prompt luck.

**The "five follow-ups about one file IS a blocker" objection is already answered** by the
mandate the chair keeps: *"A CONCERN OF YOUR OWN IS NOT A FINDING … a worry that occurred to
you and to none of them is the one thing you may not reject on."* Aggregating non-blockers
into a blocker is exactly a chair-originated judgement, and the chair is already forbidden it.
Code-side cluster detection ("N follow-ups naming one file promote to blocker") is
**deliberately not built**: there is no evidence clustering happens, and a guessed threshold is
precisely how a new rejection loop grows.

Nothing is lost from the record. `_out` returns `reply: op.raw` — the seat's *whole* reply —
and the daemon writes that into `validation_opinions.reply`, so follow-ups stay permanently
inspectable through `jarvis validation show` even though the chair never read them.

### 3.6 `chair.md`

* **Delete** "A concrete, actionable finding from either is reason enough to reject; a matter
  of preference is not." It is the sentence this feature exists to remove. Replace it with:
  a finding reaches you only because the seat that raised it judged the work unfit to ship
  without it; weigh whether that judgement is right, and reject only when it is.
* Restate the PASS paragraph as the primary case: **pass when no seat raised a blocker.** Keep
  "Small findings that nobody would act on do not justify a round trip" and note it is now
  enforced upstream.
* Add, adjacent to the OUTPUT section: *reject only on a blocker a seat raised; remarks the
  seats filed as follow-ups are not before you.*
* Keep untouched: "A CONCERN OF YOUR OWN IS NOT A FINDING", the blind-panel reading rules, the
  never-name-a-seat rule (**both halves** — a bare name used as an actor, "the maintainer",
  "three seats found", is narration and must be caught, `kn-abb7356b`), the 200-word / 1500-
  character budget, the two-word `outcome` vocabulary, the reject-if-you-cannot-tell fail-safe,
  and the ASSUMPTION paragraph.

`tests/test_validation_seats.py` asserts seat prose **against the shipped markdown**
(`bootstrap.ASSETS / "validator-seats"`), never a Python constant. Pin that the deleted
sentence is gone and the new bar is present, in the file the runtime reads.

### 3.7 `decide` grows a contract key that nothing reads yet

```python
decide(store, round_row, packet, cfg) -> {
    "outcome": ..., "reason": ..., "seats": [...],          # unchanged
    "follow_ups": [{"seat", "title", "detail", "round"}],   # NEW
}
```

The daemon reads only `outcome`, `reason` and `seats` today, so the key is silently dropped
until §4 lands. **That is what makes this work order shippable on its own.**

### 3.8 Traps

* **The fake `claude` needs its branch before any assertion about it.** `src/jarvis/testing.py`
  claims a validation call by the literal `# Jarvis validation seat: <seat>` and its branch is
  placed FIRST on purpose. Add `FORCE_FOLLOWUP_<SEAT>` beside `FORCE_BLOCK_<SEAT>` and
  `FORCE_REJECT_<SEAT>`, and give the default unforced reply an explicit `"findings": []` —
  otherwise the whole suite exercises the absent-key fallback and nothing exercises the new
  path.
* **`project_store.VALIDATION_VERDICTS` is unchanged.** `record_validation_opinion` asserts
  `verdict in ("pass", "reject", "")` and `validation._verdict` narrows to it in two places.
  Severity is a property of a FINDING, never of a verdict.
* `tests/test_validation_seats.py` asserts the literal key names appear in each non-chair
  mandate; `"findings"` is added to that tuple, not substituted into it.
* **One sanctioned edit to an existing validation test, and only one.**
  `tests/test_validation_panel.py` asserts `set(result) == {"outcome", "reason", "seats"}` on
  `decide`'s return. Adding §3.7's key requires that set to gain `"follow_ups"`. That is the
  only edit to an existing validation test this section permits; anything else being edited is
  a signal that the schema stopped being additive.
* **This section adds no eval battery.** `tests/test_validation_eval_harness.py` requires the
  baseline's measured-case set to equal the battery set in both directions, so a battery added
  without a paid run turns the free suite red. §6 owns that.

---

## 4. Filing a follow-up, and showing it

**One work order.** The filing, the dedupe, the cap, the config knob, the timeline event, and
every surface that shows the result. Needs §3.

Filing and showing are one reviewable unit because **a follow-up filed into a backlog nobody
can reach from the work order is strictly worse than today**, where at least the nit came back
as feedback. Half of this shipped alone is a regression.

### 4.1 Where the write happens

**Not in `validation.py`.** Its contract is *"It is called; it is never messaged, and it
messages nobody."* A panel that writes backlog rows has a side effect the round machine cannot
roll back when the round later fails on transport. (`central_store` is not on that module's
AST ban list — `decide` already opens one — so this is a contract boundary, not a mechanical
one, and it must be written down or it will be crossed.)

**Not inline in `daemon.py` either.** One function in **`ops.py`**, called by both loops.
`ops` already owns `side_effects_of` and `collect_feature_evidence` for exactly this "the
collector may not read a store, so `ops` does" reason, both daemon loops already import it
locally, and two inline copies is the shape that drifts. `_round_config`'s docstring records
Neo ruling (question 176) that the two loops are held identical.

**Called before the outcome branch, not inside it** — immediately after the
`record_validation_opinion` loop and before `outcome = …`, in `_validate_work_order` and in
`_validate_feature`. A follow-up is filed whether the round passed **or** was rejected; that is
the whole point, and making it conditional on an outcome the seat could not see when it wrote
would reintroduce the treadmill in miniature.

**Read the key defensively: `verdict.get("follow_ups") or ()`.** `Daemon.validator` is
injectable and several tests inject fakes returning only the three existing keys.

### 4.2 The backlog row

`CentralStore.add_backlog(project, title, description, origin_wo_id=…, origin_note=…)` — every
column already exists.

* `title` — the finding's `title`, trimmed.
* `description` — the finding's `detail`, plus a provenance footer naming the unit, the round
  and the pull request URL. It is the only context the person who picks the ticket up will have.
* `origin_wo_id` / `origin_fo_id` — the unit under review.
* `origin_note` — reuse the **shape** of `bus.origin_note` so the two filing paths read alike:
  `"validation follow-up · <seat> seat · round <n>"`. **Name the seat here and only here.**
  Deliberation never reaches the submitter, but the backlog is the project's record, read later
  by a person deciding whether to act, and *which reviewer said this* is exactly what they need.
  This is the one door deliberation leaves by, on purpose; say so in the code comment or a
  later reader will "fix" it.

**The submitter is told nothing about filed follow-ups.** They are the user's backlog, not the
worker's homework, and `REASON_LIMIT` is 1500 characters that `daemon.REVIEW_FEEDBACK` and then
the bus each re-frame — every sentence added there pushes the instructions that matter off the
bottom. This is a decision, not an oversight.

### 4.3 Why NOT the bus, though the primitive exists

The OS already has this shape: `bus.DeferralRequest` is "work found on the way that is not this
work order's job", `bus.origin_note` is the canonical note format, `bus._unfilled` files to
`CentralStore.add_backlog` with exactly these origin columns, and `reviewer` is already a legal
sender. **Do not use it, and write the rejection into the docstring**, because an architect
reviewing this will ask.

The reason: when the subject is under a feature order, `bus.resolve` finds the manager and the
deferral is **delivered as a message** telling it to run `jarvis backlog add` — spending a
manager turn per follow-up and turning a review nit into a conversation. You would get backlog
rows for standalone work orders and manager instructions for feature children: the same fact
recorded two different ways depending on parentage, which is the exact failure `_unfilled`'s
own comment warns about. Direct `add_backlog` gives one behaviour.

### 4.4 Dedupe, and the cap

**Dedupe is not optional.** Round 2 re-raises round 1's nit — that is mechanism 1.2 — and
without dedupe this feature converts a rejection treadmill into a backlog flood.

* The source of truth is **the backlog**, never the timeline event: "has this already been
  filed" is a question about the backlog. `CentralStore.list_backlog` filters on project and
  status only, so this needs one small query (an `origin_wo_id` filter, or
  `backlog_from(origin_wo_id=…)`).
* **`list_backlog` DEFAULTS TO `status="open"`, and a dedupe built on that default re-files for
  ever.** A follow-up the user has since closed, dropped or promoted stops being "open", the
  dedupe stops seeing it, and every subsequent round files it again. The new query must read
  **every** status. It is one keyword and it is invisible until production.
* Match on a digest of the normalised title, the way `evidence._normalise` normalises — strip,
  collapse whitespace; that is the house rule.
* **Title matching will not be reliable and the code must say so.** A seat writes a slightly
  different sentence each round. Bound the damage rather than chase it: **cap follow-ups filed
  at 5 per round** (a `ValidationConfig` field), and never file the same digest twice within one
  round. The failure mode is then a few near-duplicate rows a user can drop, not a filled
  backlog. Findings beyond the cap are dropped with a log line and a count in the event; they
  are not blockers, and a later round can re-raise them.
* **Idempotent under a retried round.** `_validate_work_order` can run more than once for one
  round on a transport retry, so the dedupe must key on the finding and the unit, never on the
  round.

### 4.5 The config knob

One new `ValidationConfig` field beside `max_rounds` — name it for what it does (e.g.
`follow_ups: bool`) — with the per-project field-level inheritance every field in that block
has, and parsed by `catalog._parse_validation`.

It controls **filing only**. §3.4 is unconditional: a `follow_up` finding never rejects,
whatever this is set to. So `False` means the finding is discarded rather than recorded, which
is a strictly worse record and should be a deliberate choice.

**It ships `True`, and that is a ruled exception to `ValidationConfig`'s house rule** that new
panel behaviour ships disabled pending a measurement. Neo, question 309: *"Option A — default
True, as you specified. The rejection change is already unconditional, so the knob only decides
ticket-or-discard, and discarding is the one outcome nobody wants; keep the knob (not C) as the
escape hatch if filing proves noisy."* The house rule exists so that a behaviour change is
measured before the fleet runs it; this knob gates no behaviour change, only whether the record
survives. **Say so in the field's own comment**, or the next reader will read it as a mistake.

**The `ValidationConfig` DOCSTRING paragraph recording that carve-out is not this feature's
work.** Neo filed `wo-42f028d1` for it while answering question 309, and it is running
independently. Write the comment on your field; do not also rewrite the class docstring, and
expect a small rebase in `catalog.py`.

### 4.6 The timeline, and what must not be widened

* A new event kind — `validation_follow_ups_filed`, payload `{round, round_id, ids, seats,
  dropped}`. **It is invisible until `timeline._describe` names it** (`kn-3f133363`): that
  function is an `if`-chain whose last line returns the bare kind plus a JSON blob, and
  `event_level` defaults an unknown kind to `signal`, so an unregistered kind *renders* and is
  unreadable. One branch covers `jarvis wo show` **and** the dashboard, because both call
  `timeline.build_timeline`. Do **not** add it to `DEBUG_KINDS`.
* **`ops.side_effects_of` is not affected and must not be widened.** It reads the knowledge
  base's attribution columns to tell the panel what durable work the *submitter* did outside
  the repository. A backlog row the *panel* filed is the panel's own output; a later round that
  saw it in `side_effects` would judge its own remarks as the submitter's deliverable — and
  `side_effects_sha` is in `evidence.fingerprint`, so it would break the repeat guard too.

### 4.7 The surfaces

The CLI is the OS: anything the dashboard shows, the CLI shows, through one shared renderer in
`ops.py`. Two surfaces rendering the same thing separately is how they come to show different
things (`kn-99e37a4b`: "every surface" is a claim you go and count, and a server-rendered
template is always one of them).

**"Every surface" here is THREE code paths plus one Jinja macro — go and count them**, and the
first is the one that is easy to miss:

1. **`ops.validation_rounds` projects a NAMED KEY LIST.** A per-round `follow_ups` key must be
   added there or `jarvis wo show`, `jarvis fo show` and both dashboard pages will never see it.
   **Always present, even when empty** — the rule `validation_rounds`, `assumptions` and
   `alarms` already follow, because a key that comes and goes is one every consumer must guard.
   Note it is **not** the only reader: the dashboard macro is fed `ops.validation_detail`, a
   different function, and the CLI's human (non-`--json`) view goes through a third,
   `cli._readable_rounds`, which drops an empty key on purpose.
2. **`ops.round_line`** is the one-line formatter both `show` commands share. A count belongs
   there or nowhere.
3. **`cli.cmd_validation`** (`jarvis validation show`) gains the classified summary — which
   findings blocked and which were filed, with the backlog id beside each. It already prints
   `reply` raw, so the new shape appears there for free; the work is making "why was this
   rejected" answerable without reading five JSON blobs.
4. **`ui/templates/_validation.html`** is a macro shared by the work-order and feature-order
   pages, so extending it covers both. Its `deliberation` macro dumps `o.reply` into a `<pre>`
   and parses nothing — that too is free.

**Out of scope, and already on the backlog:** showing a panel-filed item's origin on
`jarvis backlog list` and the dashboard's backlog page. That is a change to the *backlog*
surface rather than the validation surface, it needs none of this work order's fixtures, and
it is the piece that would push this session over.

Nothing in this section may put a seat's name in front of the **submitter**. The seat name
lives on the backlog row (§4.2) and in `jarvis validation show`, which is the deliberation
surface and always has been.

---

## 5. What a later round already knows

**One work order.** The packet gains prior-round history; the callers pass it in; the shared
prefix renders it; a free structural test proves round 2 carries round 1. Needs §3, and the
edge is a **merge order, not a behaviour dependency**.

Everything this renders is already on the record today: `validation_rounds.reason`,
`validation_rounds.outcome`, `validation_rounds.head_sha` and `validation_opinions.reply`,
which stores the raw seat JSON verbatim. So the *behaviour* needs nothing from §3. The edge
exists because this section renders history **per finding** — the blockers raised, the
follow-ups marked as already raised — and that split is `validation.blockers()` /
`validation.follow_ups()`, which §3 writes. Writing a second reader here instead would be two
surfaces rendering the same thing separately, which is how they come to show different things.

**The renderer reads BOTH shapes — `findings` when present, else the round's `reason` and the
seat's `asks`** (§2.2). Every opinion already on the record is the old shape, a model will
sometimes answer in the old shape anyway, and the tests that stage a rejected round with an
injected validator write prose replies rather than JSON — so the fallback is not a corner, it
is the path most of the suite takes.

**Backlog ids are out of scope here.** A follow-up is marked as already raised from its own
`severity`; it does not carry the id of the row §4 filed for it. Joining the two is a
cross-child read for no gain to a seat.

### 5.1 The field

`EvidencePacket` gains one field, keyword-only, defaulting to `()`, alongside `assumptions` and
`side_effects`:

```python
#: What earlier rounds raised and how it was settled. One entry per prior round:
#: {"round": int, "outcome": str, "reason": str, "head_sha": str, "findings": [...]}
history: tuple[dict, ...] = ()
```

**Passed in, never looked up.** `evidence.py` reads a repository and never a database — the
separation that keeps `ProjectRef` a two-line stand-in rather than a `ProjectSpec` import. The
caller builds it from `store.validation_rounds(...)` and `store.validation_opinions(round_id)`.

**There are FOUR `collect_*` call sites, not two**, and a worker who does not know this will
either wire all four or panic when they diverge. The packet is built twice per round:
`ops.submit_for_validation` builds one only to compute the fingerprint at round-open time, and
`daemon._validate_work_order` builds the one the seats read. The same pair exists for features
(`ops.submit_feature_for_validation` and `daemon._validate_feature`, via
`ops.collect_feature_evidence`). **Only the daemon-side pair passes the history.** Because the
field is excluded from the fingerprint (§5.4), the two packets differing is harmless.

### 5.2 Rendering

A section in `build_packet_prompt` — therefore inside `build_shared_prefix`, therefore in the
cached prefix every seat of the round reads, therefore paid for once. **Absent entirely on
round 1**, in the house style of `render_knowledge` and the assumptions block: an empty heading
is a thing a model reasons about.

It must say what it is for, in as many words:

> These points were raised in earlier rounds of this same review and are recorded here so you do
> not raise them again. A point already answered is not a new finding. Where an earlier round
> told the submitter to do something, the code doing it is an INSTRUCTION FOLLOWED and not a
> defect. Judge the change in front of you; this is the record of what has already been asked
> of it.

Each entry carries the round number, the outcome, the `reason` the submitter was actually told,
the blockers raised, and the follow-ups marked as **already filed** so no seat re-raises one.

### 5.3 The diff does NOT narrow to a delta

Rejected for three independent reasons:

1. **`automerge.decide`'s conditions compare `ProjectStore.validated_head` against the pull
   request's `head_oid` — the whole commit.** A `passed` round is an assertion about the entire
   tree at that sha, which the OS then merges with no human in the loop. A delta-judged pass
   would authorise merging code no seat read. That is not a review with a smaller window; it is
   a pass that means something different from what the merge machinery believes it means.
2. **`evidence.fingerprint` would break in the dangerous direction.** `diff_sha` is taken over
   the full pre-truncation diff. Under a delta it would hash the *delta*, so two rounds with
   identical trees but different predecessors would hash differently, and
   `daemon._preceding_round`'s repeat guard would stop catching a submitter that changed nothing.
3. A delta hides regressions in code the change already touched.

### 5.4 The orientation line — the cheap honest version, on purpose

**You cannot compute "what changed since the previous judged head" from the packet.** On the
pull-request path `packet.base` and `packet.head` hold **branch names**, not shas — the field's
own comment says so, and `judged_head` exists precisely because `packet.head` is unusable for
this. The previous round's real sha is on `validation_rounds.head_sha`, which `evidence.py` may
not read. And the shas are not necessarily in any local checkout: the pull-request path gets its
diff from `gh` and never fetches the branch, so a `git diff <prev>...<head>` may fail silently to
`""`.

So v1 ships no computed file list. The caller passes the previous round's `head_sha` in with the
history, and the prompt says, beside the prior findings: *round 2 judged commit `abc1234`; you
are judging `def5678`.* When the previous `head_sha` is `""` — a worktree packet, or a round
written before that column — the line is **omitted and says nothing**, never guessed. A real
delta file list is a separate work order that must first solve "is this sha fetchable", and this
feature is not blocked on it.

### 5.5 `history` MUST NOT enter `evidence.fingerprint`

The fingerprint answers exactly one question — *did this submitter produce new evidence?* — and
history is produced by the OS, not the submitter. It differs every round by construction, so
hashing it would make every unchanged resubmission look new, silently disabling
`daemon._preceding_round`'s repeat guard, and would change the hash of every round already
stored. Read that function's exclusion table before going near it, and add a row to it.

### 5.6 The multi-round property is proved here, free

"Round 2's shared prefix contains round 1's findings" is **structural, not a judgement**: it is
provable against the fake `claude` without spending a token, and it belongs in this work order
rather than in §6's paid battery, whose harness runs exactly one round per case. Stage two
rounds, assert that the second round's packet prompt contains the first round's finding text and
that the first round's packet prompt contains no history section at all. The negative half is
what makes it measure anything: "round 2 has history" and "the renderer emits history
unconditionally" are the same observation without it.

Three things about staging it in `tests/test_validation_loop.py`, which already has every piece:

* **The second submission must change the committed bytes.** `daemon._preceding_round` compares
  fingerprints and escalates an identical resubmission *before* the validator is ever called, so
  the same bytes give you one recorded call and an `IndexError`. Assert the round list first so
  the failure names its cause.
* **An injected validator never runs `decide`, so a staged round 1's stored opinions are PROSE,
  not JSON.** That is useful rather than a problem: it exercises the old-shape fallback for
  free, and proves the renderer falls back to the round row's own `outcome` and `reason`. Do not
  "fix" the shared helper into emitting JSON — around twenty tests in that file use it. Add a
  separate local helper for the new-shape test.
* Both shapes need covering, because they are the two that occur in production.

---

## 6. The measurement

**One work order.** Needs §3 and §4. This is the property that decides whether the feature
worked, and it is an **eval**, not a unit test, because it is a judgement about model behaviour.

**No free test proves the headline property, and pretending otherwise is the trap.** "A
follow-up finding no longer causes a rejection" is owned by no section as a checkable fact: §3
changes no outcome by design, §4 files rather than decides, §5 renders. The mechanism that makes
it true is `build_chair_prompt` withholding follow-ups plus the deleted sentence in `chair.md`,
and the only free proof available is *the chair's prompt does not contain the follow-up text* —
a proof about a prompt, not about a verdict. This section is where the property is actually
measured, and it is measured rather than guaranteed.

`evals/llm/test_validation_judgment.py` already has `MUST_REJECT` (floor 4), `MUST_PASS` (floor
3), `FEATURE_CASES` (floor 2) and a degradation case; it rewrites
`evals/llm/validation_baseline.json` on every paid run; and the free companion
`tests/test_validation_eval_harness.py` holds the two together, pinning case counts exactly and
requiring the baseline's measured-case set to equal the battery set.

**The new battery — `MUST_PASS_WITH_FOLLOW_UPS`.** Small, correct submissions (order of 100
lines) that are *also* obviously improvable: a name that could be better, a docstring that could
say more, a helper that could be shared. Single-round, like every other battery. The assertions:

* the outcome is `passed`, **in that one round**;
* at least one `follow_up` finding was raised — a battery the seats pass by finding *nothing*
  measures the wrong thing and would score identically with this feature reverted;
* **zero** of those remarks reached the submitter: the round's `reason` is empty.

**The negative half is not optional and lives in the same file.** The danger of this feature is
a rubber stamp:

* `MUST_REJECT` keeps its floor and its cases unchanged. A real defect — a missing test for new
  behaviour, a security hole, a wrong assumption — is still rejected.
* Add at least one case where the **same** submission carries a real defect **and** cosmetic
  nits: the defect must block and the nits must file. That is the discrimination this feature
  claims, and nothing else in the suite measures it.
* The tester and security veto keep their own case: `blocking: true` still rejects with the
  chair never called.

**The money is authorised and the hand-edit is not.** Adding a case fails the FREE suite until
the baseline is regenerated — deliberate (`kn-abb7356b`): a case no paid run has seen has no
measurement behind it. The harness docstring permits hand-editing the baseline if the pull
request says so; **this work order does not**, because the free harness cannot tell a fabricated
entry from a measured one — it checks that the run keys match the battery names, that each run
has seat rows, and that no floor exceeds a stored integer, all of which a careful hand-edit
satisfies. The rule is the only enforcement there is.

Run `JARVIS_EVALS_LLM=1 uv run pytest evals/llm/test_validation_judgment.py -q`, commit
`validation_baseline.json` **exactly as that run wrote it**, and paste into the pull request
body, verbatim, the cost block the module fixture prints to the terminal (the line beginning
`validation panel cost reading — model=` and the rows beneath it). That block is written past
pytest's capture by a real run and is the witness that one happened. If the paid run cannot be
made — no credentials, a refusal, repeated transport failure — **stop and file
`jarvis wo assume`**; do not proceed by editing the file.

**Four harness facts, each of which is a free test that will go red.**

1. `BATTERIES` and `SIZES` are literals in `tests/test_validation_eval_harness.py`. A battery
   added to the eval and not to **both** of them gets none of the guards — uniqueness, the
   module-level-literal check, the production-path check, the baseline `n` check. That is an
   unmeasured battery wearing a measured one's clothes.
2. `PRODUCTION_SHAPES` bans the case-insensitive substring **`jarvis`** anywhere in a case's
   text, and `ALLOWED_ROOTS` confines every patched path to the invented project. A follow-up
   case written about this panel trips both.
3. Every new eval test needs its `@scenario(...)` marker, or it costs money and never reaches
   the scorecard.
4. `test_no_cost_or_latency_number_is_asserted` AST-walks every assert for `seconds`, `calls`
   and `diff_chars`.

**The floors do not move.** A fresh paid run regenerates the *existing* batteries' scores too,
and if `MUST_PASS` comes back 2/3 the cheapest exit is to lower its floor. That exit is closed:
`MUST_REJECT_FLOOR` stays `4`, `MUST_PASS_FLOOR` stays `3`, and `MUST_REJECT`'s four cases are
unchanged. A red case is read before any number is considered — see (b) below.

**Two rules from the ledger that apply directly.** (a) Nothing here may assert on cost or
latency — a companion test AST-walks eval tests for exactly that, and there is no baseline to
assert against. Print the call count and wall clock from the module fixture's teardown via the
terminal reporter; a bare `print()` is swallowed on a passing run, which is the only run where
the number is wanted. (b) **A `MUST_PASS` bounce is far likelier to be the fixture than the
panel** — three of four paid runs on the original work were red and *every* bounce was a real
defect in an invented submission. Read the reason before touching a floor, and never edit
`src/jarvis/` to make a case pass.

---

## 7. What must not change

This feature changes **when a non-blocking finding rejects**, and nothing else about what the OS
is allowed to do.

* **`tester` and `security` block.** `validation.VETO_SEATS`, `blocking: true`, `arbitrate`'s
  forced rejection, and the chair being skipped entirely when a veto fires.
* **Nothing forces a pass.** `arbitrate` keeps exactly one non-`None` `return` and its outcome is
  `"rejected"`, pinned by an AST walk. `tests/test_validation_arbitrate.py` stays green with zero
  edits.
* **The assumption review.** The panel judges whether an assumption is *wrong*; whether the user
  *wants* it is a parallel review and never the panel's.
* **The privileged-action gates**, and `automerge.decide`'s six conditions. A pass reached with
  follow-ups filed is a pass like any other and grants no new authority.
* **The empty-submission guard, the repeat-submission guard, and the outage path.** An empty
  packet still escalates; an identical resubmission still escalates; a `ClaudeCliError` still
  closes the round `failed` without spending one.
* **Deliberation never reaches the submitter.** No seat name, no vote count, no panel narration
  in anything a worker reads. §4.2's backlog note is the single, deliberate exception, and it is
  not a submitter-facing surface.
* **`validation.py` imports neither `neo`, `neo_store`, `panel` nor `bus`**, function bodies
  included.
* **The shared prefix stays shared.** Anything per-seat added to `build_shared_prefix` un-shares
  it silently: the tests stay green and the bill quadruples.

### 7.1 Calls taken without asking

Recorded on `wo-38e26be0`; each is a decision embodied in this design:

1. The diff does **not** narrow to a per-round delta (§5.3).
2. Feature-order validation is in scope alongside work orders — one panel, one `chair.md`, and
   `_round_config` records Neo holding the two loops identical.
3. A `follow_up` is filed **even when the round rejects** on a blocker (§4.1).
4. The seats self-classify; Neo does not (§2.1).

§4.5's default was **not** taken without asking: it was put to Neo as question 309 and ruled
there. See §4.5 for the ruling and its reasoning.

### 7.2 Deliberately out of scope

Filed on the backlog rather than built here: escalating an ambiguous classification to Neo;
code-side cluster detection promoting N follow-ups on one file to a blocker (§3.5); a computed
delta file list for the orientation line (§5.4); auto-promoting a filed follow-up into a work
order; the production-corpus replay the eval has always deferred; and any change to
`validation.max_rounds`, which was explicitly rejected as the fix.

---

## Agent profile

You are a **Jarvis OS core engineer** working on one piece of the validation panel's correction
— from a reviewer that rejects on anything it can say, into one that blocks only on what must
change and files the rest. You are working in the Jarvis OS repository itself
(`~/workspace/agentic_os`), which is the OS that dispatched you. Production runs a separate
released checkout; nothing you do here touches the running fleet.

**Know this before you write a line.**

Serena is activated and the code map is committed (it under-counts the modules — trust the
layering it describes, not its totals). Read the memories first with `read_memory` —
`codebase-map`, `work-order-lifecycle`, `neo-panel`, `testing`, `feature-orders` — and navigate
with `find_symbol` / `find_referencing_symbols` / `get_symbols_overview` rather than grepping for
definitions. Do not spawn an exploration subagent to rediscover the architecture; that is what
the memories exist to prevent.

Then read the spec section you were handed, and the two specs this design sits on:
`docs/superpowers/specs/2026-09-13-a-round-the-panel-can-afford.md` (the shared-prefix cache
layout you must not break) and `docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md`
(what a pass authorises). Search the knowledge base before you decide anything:
`jarvis learn search "validation panel" --project jarvis_os`. `kn-e4ebe91b`, `kn-abb7356b`,
`kn-19425534`, `kn-2b3deec1`, `kn-dd0b015a`, `kn-3f133363` and `kn-99e37a4b` will each save you a
mistake.

The core is **stdlib-only**: argparse, sqlite3, json. No YAML, no new dependencies. Imports run
strictly downward — leaves (`paths`, `db`, `catalog`, `claude_cli`, `seats`, `structured`,
`timeline`) → stores (`central_store`, `project_store`, `neo_store`) → adapters → `dispatch`/`ops`
→ `daemon`/`cli`/`ui`. There are no import cycles at module-import time; `cli.py` imports lazily
inside function bodies and you should match that style.

**Conventions you must follow.**

- Business logic lives in `ops.py` and is shared by the CLI and the UI. A dashboard route
  delegates; it does not implement. Anything the dashboard can do, the CLI can do.
- Prefer extending an existing shared function over adding a parallel one. Two surfaces rendering
  the same thing separately is how they come to show different things.
- Every threshold a surface judges by belongs in `catalog.py` with per-project field-level
  inheritance, never as a module constant.
- Comment density here is reviewed. Comments explain **why**, decisions taken and rejected, and
  traps — never what the line does. Read a neighbouring module before your first docstring.
- Standard flow: work in your worktree, `uv sync --extra dev`, `uv run pytest tests/ evals/`, PR
  against `main`. Never commit to `main`.

**The traps that will bite you.**

- **The panel is judging you.** `validation.enabled` and `validation.auto_merge` are ON for
  `jarvis_os`, so your own pull request is reviewed by the code you are changing. Leave the system
  coherent at every landing: **every reader of a seat reply must tolerate BOTH the old shape (no
  `findings` key) and the new one**, because the seats are prompts and a model will sometimes
  answer in the old shape anyway, and because every row already in `validation_opinions` is the
  old shape.
- **`seats.definition` is cached on the ROSTER, not the seat name.** `chair.md` exists in
  `assets/neo-seats/` and in `assets/validator-seats/`. Never add a name-only cache key.
- **`assets/validator-seats/` is deliberately not `assets/agents/`.** `bootstrap._rebuild`
  copytrees that directory wholesale into every feature-order planner's `.claude/agents/`, so a
  seat dropped there becomes a bogus subagent. Do not move seat files.
- **The fake `claude` in `src/jarvis/testing.py` recognises a call by a literal in its prompt**,
  and its validation branch is placed FIRST on purpose. A new call shape answered by the wrong
  branch returns a well-formed reply of the wrong kind, and every "nothing was raised" assertion
  passes for the wrong reason. Give a new shape its own branch and its own helper before you write
  one assertion about it.
- **`timeline.event_level` returns `signal` for an unknown kind**, so an event with no `_describe`
  branch renders as a bare name beside a JSON blob and looks fine. Add the branch.
- **`ProjectStore._migrate()` runs `ADDED_COLUMNS` on every open**, over live databases. A new
  column must be listed there, idempotent and cheap; a table rebuild is not an option.
- **Assert seat prose against the SHIPPED MARKDOWN** (`bootstrap.ASSETS / "validator-seats"`),
  never against a Python constant. The file the runtime reads is the enforcement.
- **A test that compares a function to its own body cannot fail.** When you refactor one definition
  into another, assert the behaviour, not the identity. And every check must name the case it is
  about: a guard asking whether *some* case satisfies a property is satisfied by the wrong one.
- **"Every surface" is a claim you go and count**, and a server-rendered Jinja template is always
  one of them.

**What you must never do.**

Never weaken the `tester` or `security` veto, the assumption review, the privileged-action gates,
or `automerge.decide`'s conditions. Never let anything force a PASS in `arbitrate`. Never put a
seat's name, a vote count or any narration of the panel into a message the submitter reads. Never
make `validation.py` import `neo`, `neo_store`, `panel` or `bus`. Never put per-seat content into
`build_shared_prefix`. Never add a field to `evidence.fingerprint` without reading its exclusion
table first. And never raise `validation.max_rounds` to make your tests pass — that is the fix
this feature was created to replace.
