# Neo as a panel — replacing the single decider with a team of profiled agents

*2026-08-02*

## Problem

Neo is one headless Claude call. `neo.answer_question` builds one system prompt — a
persona plus up to 50 learnings — and asks one model for one strict-JSON verdict
(`src/jarvis/neo.py:153`). Two personas exist (`PERSONA` for open questions,
`gates.REVIEWER_PERSONA` for privileged-action gates) but only ever one runs per
decision.

That worked while Neo answered a handful of scoped questions. It does not obviously
scale with the OS's ambition: the more work the fleet does autonomously, the more of the
user's judgment Neo has to carry, and a single prompt can only put **one** concern first.

This document is the evidence pass the work order asked for — what Neo actually decided,
what the user actually corrected — and the design those decisions imply. **It recommends
no implementation.** It proposes a *panel* primitive, a four-seat roster inferred from
the record, and a measured path to adopting it.

---

## The corpus

Everything below is drawn from the live production state through the `jarvis` CLI
(`jarvis neo list --all`, `jarvis neo show <id>`, `jarvis neo learnings`,
`jarvis gate list`, `jarvis wo show <id>`), on 2026-08-02.

| | count |
|---|---|
| Neo questions on record | **34** |
| — privileged-action gate reviews (`kind=approval`) | 20 |
| — open questions (`kind=question`) | 14 |
| Decided by Neo | 32 |
| Escalated / answered by the user | 2 |
| Reviewed and **approved** by the user | 28 |
| Reviewed and **corrected** by the user | **5** |
| Standing Neo learnings | 5 (all `manual`, all about one topic) |

### Finding 1 — every correction the user has ever made is the same failure

The five corrected decisions are Neo questions 2, 3, 4, 18 and 22. All five are gate
reviews. All five are the same class: a command that **performed no privileged action**
was adjudicated on its merits instead of being recognised as an OS classifier bug.

> *"this was incorrectly flagged due to an internal error in the wording, because the
> grep command included the shipit word. There wasn't any actual issue"* — review of #2
>
> *"it shouldn't have denied the request since it is a false positive and is not about
> cutting a release"* — review of #18 and #22

Nobody has ever corrected Neo's engineering judgment. Of the 13 open questions Neo
answered, **13 were accepted with no delta** — including multi-part design calls on the
release clean-tree guard (#20), the version-string format (#21), the session-injection
model (#17), and the dashboard work-order controls (#16).

### Finding 2 — the gate channel is ~95% noise

Of the 20 gate reviews, exactly **one** command actually performed a privileged action:
`scripts/shipit.sh 0.1.11` (#7). The other 19 tripped the recogniser on a literal inside
a search pattern, a file path, quoted prose, or a heredoc body. Neo denied 6 of them,
approved 12, and escalated 1 to the user.

So the single largest consumer of Neo's attention is a question that is not a decision at
all. That is what the fourth `dismissed` verdict (shipped in PR #58) exists for, and it
is the strongest single input to the roster below.

### Finding 3 — the same input got opposite verdicts

`grep -rn shipit src/jarvis/gates.py` was **denied** as gate 2 and **approved** as gate 4,
about 33 hours apart, with a learning already on file covering exactly that class
(`bl-84aab5f5`). Nothing in the system compares a new decision against past ones.

### Finding 4 — the ground truth was itself inconsistent, and a *worker* had to say so

The user approved Neo's denial on #9 and #12, then corrected the near-identical denial on
#18 and #22. That is not carelessness: until `dismissed` existed, approve recorded an
authorisation that never happened, deny recorded misbehaviour that never happened, and
escalate spent the user's attention on an OS bug. Every option was false.

The contradiction was surfaced by a *worker*, in Neo question 24 — "I need you to settle a
contradiction in your own standing instructions … as written the instructions are
unsatisfiable." Neo's ledger had no owner. It still doesn't: `NeoStore.add_learning` and
`CentralStore.add_knowledge` only INSERT, there is no retraction anywhere in the store,
the CLI or the UI (`bl-28e18638`), so a reversed ruling stays in every prompt beside its
replacement. The live learnings today contain a `CONFLICT NOTICE` entry quoting three of
the user's own mutually-unsatisfiable rulings.

### Finding 5 — the fix for Finding 1 was *reordering a prompt*

The recorded diagnosis, now a comment at `src/jarvis/gates.py:329`, is worth quoting
because it is the whole argument for this refactor:

> The earlier persona OPENED by asserting "a worker tried to run a command that ships
> code", then required of every approval that "work landed on a branch, in a pull request,
> with checks passing" — which a misclassified `grep` can never satisfy, so the closest
> fitting clause left was DENY. **The persona structurally forced the wrong answer on
> false positives; Neo was following it correctly.**

The fix was to move the premise check to the *front*. It works: the opt-in eval
`evals/llm/test_gate_review_judgment.py` measured 6/6 false positives dismissed, 0/6
genuine privileged actions dismissed, 0 false denials.

**A single prompt has exactly one front.** That is the structural limit this design
addresses.

### Finding 6 — the only structural feedback on Neo itself was about length

Work order wo-b113b156: *"questions from the worker and the answers from Neo are too long
to review … If Neo approves, just 1 line of explanation. If Neo rejects, no more than 50
words."* Attention is the scarce resource. Any design that multiplies the number of
agents must not multiply the number of words that reach a human.

---

## Why a team, and not simply a better prompt

Three arguments, in descending strength.

**1. Ordering.** Finding 5 shows a concern that is not first gets outcompeted by one that
is. Four concerns cannot each be first in one prompt. As the OS takes on more autonomous
work, the set of checks that each *need* to be first grows, while the single persona's
front stays size one.

**2. Attention within the prompt.** `bl-84aab5f5` asks whether the learnings block
actually bites on gate reviews at all; the hypothesis on file is that
`REVIEWER_PERSONA`'s explicit APPROVE/DENY checklist dominates the learnings appended
after it. A dedicated seat whose *only* job is the ledger makes that failure structurally
impossible rather than a matter of prompt luck. The 50-learning limit also stops being one
shared budget: each seat carries only what it needs.

**3. Independent evidence.** Seats that cannot see each other's opinions turn agreement
into a signal. Today Neo's confidence is unmeasurable; a 4-0 panel and a 2-2 panel look
identical from outside.

**What this argument deliberately does not claim:** that Neo reasons badly. The record
says the opposite (Finding 1). Any adoption path that regresses the 13/13 on open
questions is a net loss, which is why the fast path below is load-bearing rather than an
optimisation.

---

## Recommended composition

Four seats plus a chair. Each seat is named by the failure it owns, and each is justified
by the record rather than by symmetry.

### Seat 1 — the Premise Sceptic  `premise`

**Question it owns:** *was this even the question that was asked?*

**Evidence:** all 5 corrections (Finding 1); 19 of 20 gate reviews (Finding 2); the
opposite-verdicts incident (Finding 3); the persona reordering (Finding 5). This is the
only seat with a *measured* failure behind it.

**Mandate on a gate review:** decide whether the command performs the privileged action
at all. Propose `dismiss` when it only names one. It carries the HARD LIMIT verbatim —
a command that *actually* invokes the release script, merges a pull request or restarts a
service is privileged however routine it looks, and ambiguity resolves toward the
privileged reading.

**Mandate on an open question:** check the *frame*. Workers present decisions as menus,
and the menu is sometimes wrong. Neo has done this well when it happened to: on #27 it
rejected the literal ask — *"`wo_<number>` in the work order text was shorthand, not a
spec … do not invent a numbering scheme"* — and on #25 it confirmed the worker's premise
before letting it build.

**It also routes.** See "The fast path".

### Seat 2 — the Record Keeper  `record`

**Question it owns:** *what has already been decided, and does this contradict it?*

**Evidence:** Finding 3 (opposite verdicts with the teaching on file); Finding 4 (a worker
had to report that Neo's own instructions were unsatisfiable); the append-only ledgers
with no retraction (`bl-28e18638`); `bl-8427e451` — learnings print no timestamps and have
no `--json`, so *nobody can tell whether a learning predates a decision*; the
`learnings_limit=50` truncation, which is silent forgetting.

**Mandate:** retrieve the standing rulings that actually bear on this question from both
ledgers (Neo learnings and the central knowledge base), state whether the proposed
decision is consistent with them, and name any contradiction **before** the verdict rather
than after. Where a contradiction is real and unresolvable, it may force `escalate` and
attach a cleanup dispatch — the mechanism PR #57 adds (a verdict may carry
`dispatch: {title, description}`, which the daemon turns into a pre-approved work order
with the new origin `neo`). That PR is **in review, not on `main`**, so this seat's
remedy depends on it landing.

It also owns the *verbatim* obligations: Neo question 26 required that the 50-word budget
"must never squeeze out the mandatory false-positive reason wording". Compliance phrasing
is exactly the thing a summariser silently drops.

### Seat 3 — the Blast-Radius Reviewer  `blast`

**Question it owns:** *if this is wrong, what does it cost, and which way does it fail?*

**Evidence:** it is what Neo's best answers already do, and it is the seat whose absence
would be dangerous rather than annoying.

- #15: *"treat any detection error as non-prod rather than crashing the header"* — and it
  rejected the explicit-flag option because live production would label itself dev, *"the
  wrong failure direction"*.
- #14: a `--bg` fork **cannot** answer a permission prompt, so Claude's default mode is
  *"not a conservative choice, it's a guaranteed stall"*.
- #25: a mis-dismissal has no expiry, so scope is the only containment; and the ordering
  constraint across a release — teaching production Neo to emit `dismiss` *before* the
  code that parses it is deployed makes the old build **deny** instead.
- #28: *"Draft status is an enforceable mechanism; a note is not."*
- #34: a decision to publish to a public tracker.

**Mandate:** owns `escalate` and the HARD LIMIT. Holds a **one-way veto**: it may force
`escalate`, and it may veto a proposed `dismiss` or `approve`. It may never force an
approval. This is the seat that implements "when genuinely torn about a REAL privileged
action, escalate", and it is the only seat allowed to overrule the Premise Sceptic.

It also carries the evidence check — is the claimed test real, is it non-vacuous, was CI
actually green. Neo does this consistently today (#14 *"verify it fails without the 2-line
change so it isn't vacuous"*, #20's test table, #21 *"test the `git describe` failure path
you found"*, #25 *"put a test on that branch specifically"*). See open question C on
whether this deserves its own seat.

### Seat 4 — the User's Taste Advocate  `taste`

**Question it owns:** *is this what the user meant, and what does it cost their attention?*

**Evidence:**

- #16: went **broader** than the literal request because the intent was "get the noise out
  of the list" — *"failed and cancelled are the same noise"*.
- #27: the only delta Neo issued across 13 open questions, and it came from reading intent
  over literal text.
- #20 and #23: scope discipline — *"that decision doesn't get smuggled in under a bug
  fix"*, *"there is no urgency that justifies muddying a review-ready diff"*.
- #25: no inbox item on a dismissal, because *"a false positive consuming the user's
  attention is precisely what this feature exists to stop"*.
- Finding 6: the brevity budget.

**Mandate:** intent over literal wording; scope discipline (never bundle); attention cost;
and enforcement of the answer budget on the chair's output. **No veto** — its failure mode
is an annoying answer, not a dangerous one, and a seat that can block on taste would spend
exactly the attention it exists to protect.

### The Chair  `chair`

Not a fifth opinion. It receives the seats' opinions and emits **exactly** the strict JSON
`neo.parse_verdict` already accepts — same fields, same four verdicts, same backward
tolerance for an older Neo emitting the boolean `approve`. That compatibility is a release
requirement, not politeness: the persona ships in deployed code while learnings live in
production's `JARVIS_HOME`, so the two can disagree across an upgrade in either direction.

The chair is bound by the answer budget PR #57 introduces (also in review, not yet on
`main`): one line when it endorses the worker's recommendation, at most 50 words when it
overrides, and the mandatory false-positive wording exempt from the count. **Panel
deliberation never reaches the worker or the user** — it is stored and inspectable on
demand, never pushed.

### Deliberately not a seat

There is **no domain-engineer seat**, because there is no evidence for one: 13 of 13 open
questions were accepted without a delta. Adding one would cost latency to fix a problem
the record does not show.

---

## The fast path

This is the part that decides whether the refactor is a win or a tax.

The Premise Sceptic runs on **every** decision — it is the cheapest seat and the one with
the proven failure — and emits a route alongside its finding:

```
{"finding": "...", "proposed_verdict": "dismiss"|null, "route": "fast"|"panel"}
```

- **`fast`** → the chair answers directly from the current single-agent persona. One call
  total for the ~95% of gate reviews that are classifier false positives, and for
  low-stakes open questions.
- **`panel`** → all four seats run blind and in parallel; the chair synthesises.

Escalating the route is always allowed and never penalised; any seat that later returns
`escalate` wins. Full panel is mandatory, regardless of route, for: a command that really
does perform a privileged action; any question the worker marked high-stakes; and any
decision where the Record Keeper reports a contradiction.

Routing in code rather than by a model was considered and rejected: `kind == "approval"`
is a poor proxy (19 of 20 approvals were trivia) and a hand-tuned heuristic would need the
same evidence the sceptic already produces.

---

## Design sketch

### Module

A new `src/jarvis/panel.py` holding the primitive: a roster of seats, one blind parallel
round, a synthesis step, one strict-JSON verdict out. `neo.answer_question` becomes a
caller of it. `gates.REVIEWER_PERSONA` becomes the `premise`+`blast` seats' mandate rather
than one monolith.

### Contract

Unchanged at the boundary. `panel.decide(question) -> dict` returns what `parse_verdict`
returns today: `escalate`, `answer`, `reason`, `verdict`, `approve`. Nothing downstream —
the daemon's `deliver`, the gate path, the CLI, the dashboard — needs to know a panel ran.

### Veto rules

| Seat | May force | May veto | May never |
|---|---|---|---|
| `premise` | `dismiss` (proposal) | — | override the HARD LIMIT |
| `record` | `escalate` | — | silently decide against a standing ruling |
| `blast` | `escalate` | `dismiss`, `approve` | force an approval |
| `taste` | — | — | block a decision |

All vetoes point toward the safe direction only. Nothing in the panel can open a gate that
a single Neo would have kept shut.

### Degradation

A seat that errors or times out is recorded as **abstained** and the panel proceeds.
If the Premise Sceptic itself fails, fall back to today's single-agent path. A Neo outage
must never become a fleet stall. Note that `bl-3f5f1464` — *a question stranded in
'answering' is never retried* — becomes N times more likely with N calls, so it is a
**prerequisite**, not a follow-up.

### Cost and caching

Today: one `opus` call, `timeout=300`, drained FIFO on a single thread so consecutive
answers share a warm prompt prefix. Each seat's system prompt is byte-stable *per seat*,
so each keeps its own cached prefix; running seats in parallel does not disturb that,
because the prefix is per-seat rather than per-queue-position.

Expected cost: 1 call on the fast path (unchanged), 5 on the full panel. Given Finding 2,
the blended number should be close to today's — but that is a claim to **measure**, not to
assert. Per-seat models are configurable; the sceptic is a classification job that may not
need `opus`, while the chair should keep it.

### Storage and surfaces

A `panel_opinions` table in `neo.db` keyed by `(question_id, seat)` holding the seat's raw
JSON, its verdict, and latency. Surfaced by `jarvis neo show <id> --panel` and on the Neo
dashboard tab; **not** delivered to the worker and **not** placed in the inbox.

The review loop is unchanged — the user reviews the chair's answer. One addition:
`jarvis neo review <id> --correct "…" --seat <name>` routes the resulting learning to that
seat's prefix, so a correction teaches the seat that got it wrong instead of all of them.
Learnings gain an optional seat scope, defaulting to global so the existing byte-stable
prefix is preserved.

### Config

`NeoConfig` gains a `panel` block: `enabled` (default **false**), `roster`, per-seat
`model`, `chair_model`, `timeout`, and the fast-path policy. Shipping it disabled makes
the release a no-op and the rollout a catalog edit.

---

## Candidate shared primitive — ownership to be reconciled with wo-b31fc21f

> **This section proposes a contract; it does not claim the module.**

Work order `wo-b31fc21f` is concurrently designing `feature_order`, in which *"each
project orchestrator will be the lead of a claude team"* that plans a coarse order into a
dependency tree of ordinary work orders. Its design lands at
`docs/superpowers/specs/2026-08-02-feature-orders-design.md`.

That is a second caller for the same shape: a roster of profiled agents, one blind round,
a synthesis step, one structured result. If both designs invent their own, the OS acquires
two unrelated multi-agent frameworks.

**Proposal:** `panel.py` is that shared primitive — roster, blind parallel round, chair,
strict-JSON out — with Neo (a *decider*) as its first caller and the project orchestrator
(a *planner*) as the intended second. The roster and the output schema are per-caller; the
mechanism is not.

**Ownership and the final interface are to be reconciled with `wo-b31fc21f`.** Whichever
work order implements first owns the module; the other adapts to it. A design doc is cheap
to reconcile — two frameworks are not.

Differences to expect, which the interface must tolerate: a planner's output is a
dependency graph rather than a verdict; a planner's seats are domain roles rather than
review lenses; a planner probably wants seats that *can* see each other, where Neo's must
not.

---

## Measurement

**The corpus is a regression suite.** 34 real questions, 34 recorded verdicts, 5 human
corrections and 28 approvals. Any panel that cannot reproduce them is not an improvement.

Proposed `evals/llm/test_neo_panel_judgment.py`, opt-in behind `JARVIS_EVALS_LLM=1` like
its neighbours, replaying the corpus against both the single agent and the panel:

1. **5/5** on the corrected cases — decided the way the user said.
2. **0 regressions** on the 28 approved cases.
3. Recorded token and wall-clock delta, blended across the fast path.

Two prerequisites. First, a CLI export path (`jarvis neo export --json`), because prime
directive 1 forbids reading `neo.db` directly and `bl-8427e451` already asks for `--json`
plus timestamps. Second, and this is a **user decision**: the corpus's ground truth is
inconsistent (Finding 4) because the four-verdict model did not exist when it was
recorded. It must be re-labelled against the current model before it can grade anything.

---

## Phasing

| Phase | Content | Gate to the next |
|---|---|---|
| **0** | Fix `bl-3f5f1464`; add `jarvis neo export --json`; re-label the corpus | corpus agreed with the user |
| **1** | `panel.py` + the `premise` seat only, behind config, as the gate fast path | 5/5 on corrections, 0 regressions |
| **2** | Full roster, chair, vetoes, `panel_opinions`, `--seat` learnings | measured cost within budget |
| **3** | Route by risk class; enable by default | — |
| **4** | Orchestrator adopts the primitive (or owns it — see reconciliation) | — |

Phase 1 is deliberately the smallest change that touches the one *measured* failure, and
it is independently useful even if the rest is never built.

---

## Risks

**R1 — regressing the 13/13.** The record shows Neo's open-question judgment is already
good. Mitigated by the fast path and by criterion 2 of the eval, which is a hard gate.

**R2 — cost and latency.** Neo is the first responder and its throughput is already a
filed concern (`bl-b42233c9`); `bl-cb07f3f6` notes `wo ask` still costs the user attention.
A 5× worst case on every question would be a real regression, which is why the fast path
routes rather than the roster always firing.

**R3 — diffusion of responsibility.** A panel that agrees with itself is an expensive
single agent. Mitigated by running seats **blind** — no seat sees another's opinion — so
that agreement is evidence rather than an echo, and by giving each seat a mandate narrow
enough that it can be wrong on its own terms.

**R4 — inconsistent ground truth.** Finding 4. Unmitigable in code; it is open question A.

**R5 — more agents, more words.** Directly against Finding 6. Hard-mitigated: the chair
keeps the existing budget and the deliberation is stored, never delivered.

**R6 — the caching argument is reasoned, not measured.** Per-seat prefix stability should
hold, but it is an assumption until phase 1 reports numbers.

---

## Open questions for the user

**A. Re-labelling the corpus.** The user's own reviews disagree (approved the denials on
#9/#12, corrected the identical denials on #18/#22) because no honest verdict existed
then. Under the four-verdict model those are all `dismiss`. Confirming that re-labelling is
a user decision, not a worker's.

**B. Retraction.** Does the Record Keeper get real retraction over the ledgers
(`bl-28e18638`), or does it stay append-only and only ever *append a superseding entry*?
The seat is materially weaker without it — it can flag a contradiction but never end one.

**C. A fifth seat.** Should the evidence/verification check (is the test real, is it
non-vacuous, was CI actually green) be its own seat rather than part of `blast`? Folded in
here because no *corrected* decision demands it and every seat costs latency. Promote it if
phase 2 shows `blast` dropping evidence checks.

**D. Per-seat models.** Is a cheaper model acceptable for `premise`, given it is the seat
with the proven failure but also the simplest job? The measured 6/6 on the current gate
eval suggests yes; it is a cost/confidence trade the user should make.

**E. Ownership of the shared primitive** — this design or `wo-b31fc21f`'s.

---

## What this document does not do

No code. No `panel.py`, no config change, no persona edit. The work order asked for
analysis and a recommendation, and the phasing above deliberately puts a measurement
harness before the first behavioural change — because the single strongest fact in the
record is that the current Neo is corrected on one narrow class and on nothing else.
