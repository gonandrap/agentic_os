# A steward decides what the OS may spend on itself

Status: **draft for user review**, 2026-10-21. Eval evidence: PR #1122
(`evals/llm/test_triage_scarcity_ab.py`).

The OS is meant to evolve itself. The goal here is to make that evolution **narrow and
budgeted**. Turning it off is not the goal.

## 1. The problem

The OS needs a way to tell a needed change from one a model made up. Every finder it
runs is a model asked "what is wrong?":
- the validation panel
- investigators
- improvement-order analysts
- agents filing bugs
- Neo riders

A model asked that almost never answers "nothing". Filing costs the filer nothing, and
the order it buys is paid from the user's weekly quota.

Measured on jarvis_os, 2026-09-15 → 10-20 (356 orders, $8.1K at list price):

- **Self-generated orders.** They cost $1.4K (17%). Counting bugs filed from operator
  sessions the user was in, the figure is about 43%.
- **Spawning rate.** Each order spawns about 0.56 dispatched follow-up orders. The rate is
  low on average, but chains reach 10 generations. The worst case is the stuck sweep
  investigating its own fix orders: `inv-b9981176 → wo-afcea414 → inv-f543c896 →
  wo-a1d7b87c → inv-9d3d1192 → wo-66fc8296`.
- **Panel follow-ups.** The panel filed 375 follow-up issues and 355 were never touched.
  It also wrote 830 internal follow-ups. None was ever dispatched. That is pure spend.
- **Priority labels.** On about 97% of issues the label after Neo's re-assessment equals
  what the filer claimed, so a claimed rating carries almost no information.
- **User engagement.** The user touched 34% of OS-generated orders, against 62% of the
  orders they typed.

Since that measurement, `os.autonomy.self_dispatch` (ships `false`) gates orders nobody
typed, and a GAP verdict files an issue instead of dispatching. Those are **brakes**:
evolution is either off or unbounded. This spec adds the **steering**: how much, and on
what.

### Why prompting alone is not enough (eval, PR #1122)

The test: Haiku reviewed a module with 12 seeded issues (1 major, 4 medium, 3 minor, 4
cosmetic), 5 runs per arm.

| Arm | Filed per run | Major | Mediums | Minor/cosmetic filed |
|---|---|---|---|---|
| Baseline: "report every issue" | 13–14 | 5/5 | 4/4 | 5–6 per run |
| Scarcity prompt | 4 | 5/5 | 3/4 | 0 |
| Severity rating + gate in code | ~6 | 5/5 | 3.6/4 | 1 leak in 5 runs |

What the eval shows:
- **The prompt fixes per-call judgement.** That is enough for the review criterion (§3.1).
- **The prompt cannot bound the aggregate.** Forty frugal finders filing four issues each
  is still 160 orders.
- **A gate that trusts the model's own severity leaks.** Haiku rated an unclosed file
  `medium` and wrote a failure scenario for it.

So: **models rate, code decides.**

## 2. Decisions captured from the user (2026-10-20/21)

These are the user's rulings. They are not open for review.

- **U1. Evolution stays.** The OS evolves itself, in a controlled and narrow way.
- **U2. Review depth.**
  - A review must find every major.
  - Missing some mediums is acceptable.
  - Minor and cosmetic findings are ignored.
- **U3. Review outcomes.**
  - A major fails validation and asks for changes.
  - A medium is left to the coder, or to a follow-up work order if the cap allows.
- **U4. Self-evolution outcomes.**
  - When the OS detects an issue that affects its own functionality (critical or
    blocker), immediate action is warranted (expedite).
  - A major *may* be worth a work order, depending on the cap.
  - Everything else is ignored.
- **U5. One central entity.** It knows the cap (a catalog config parameter) and decides
  what happens to each issue. It makes three kinds of decision:
  1. technical, per item
  2. the OS's overall self-evolution strategy
  3. leaving room (tokens) for actual feature work
- **U6. Use cheap models for this judgement.** Haiku is preferred.

## 3. Design

### 3.0 One severity scale

There is one scale, shared by the panel, bug filings, investigations and improvement
findings:

| Level | Meaning | Maps from today |
|---|---|---|
| `blocker` / `critical` | An OS function is broken now: work is lost, stuck or wrong | bug `blocker`/`critical` |
| `major` | Wrong behaviour a user or operator would notice | bug `high`; a panel blocker |
| `medium` | A real defect, but bounded or unlikely | bug `medium`; a panel follow-up |
| `minor` | Hygiene with no behavioural change | bug `low` |
| `cosmetic` | Naming, typos, style | — |

**Evidence rule (applied by code, not by the rater).** A rating of `major` or above must
carry evidence. Without it, the rating is lowered one level.
- **Review findings:** a file:line anchor plus a concrete failure scenario.
- **Self-detected issues:** a machine record, meaning one of: an alarm id, an invariant
  or doctor check id, a failed command and its output, or a work-order event.

This is the fix for the leak the eval found. A rater that invents a failure still has to
point at something that exists.

### 3.1 Review depth (the panel)

- **Rating.** Seats rate each finding on the §3.0 scale, using the scarcity framing that
  passed the eval: file only what a user or operator would notice, and filing nothing is
  fine.
- **`major` and above** remain blockers. The round fails, as today.
- **`medium`** goes back to the worker in the same round's verdict, marked *optional:
  fix if cheap while you are here*. It never costs a round. Any medium still unfixed when
  the order lands is handed to the steward (§3.2), which decides whether it becomes an
  order. The panel no longer files GitHub follow-up issues itself.
- **`minor` and `cosmetic`** are counted in the round record, for metrics, and then
  dropped. Nothing is filed.

Mediums stay in-session because a fix there is nearly free: the context is already
loaded. A follow-up order starts cold, with a ~51K-token prefix before it does any work.

### 3.2 The steward

The steward is a new deterministic module, `src/jarvis/steward.py`. It is **not** a Neo
persona, and it makes no model call of its own. It becomes the single place the OS asks
*may I spend on this?* It replaces the scattered `issues.self_dispatch(catalog)` checks
in `issues.route_filing`, `remedies`, the red-main path, Neo riders and release orders.

Every OS-originated candidate arrives with:
- a severity, from §3.0 after the evidence rule
- its provenance: where it came from, the order that caused it, and its generation
- a recurrence count: the distinct orders referencing it, as `jarvis issues` already
  counts

The steward returns one of four outcomes:
- **`dispatch`:** create the work order now, with a per-order budget the steward sets.
- **`queue`:** file or keep the issue on the tracker, with no order. *As built (#1181):*
  queued means deferred, not dropped — see "The queue drains" below.
- **`drop`:** record the count only.
- **`ask`:** raise a single inbox item for the user.

Every outcome is written with the rule that produced it, so `jarvis steward why <id>`
can answer.

#### One defect, one decision

Before any rule runs, a candidate that names code is collapsed onto the decision it
restates. The bucket is mechanical — origin unit plus file (`steward.dedupe_key`), no
model call — because the seats of one panel word one mechanism as several findings
naming different symbols in one file, and one patch covers the file (decisions #23-#25
and #14/#16/#18 of 2026-10-22, Q2109). A collapsed candidate is recorded as its own
`steward_decisions` row, `rule='duplicate'` and `duplicate_of` naming the decision it
restates, and in `enforce` the caller may not dispatch it; the panel carries the other
seats' wordings into the brief of what IS filed. A `drop` is never collapsed onto: it
decided nothing, and a severer wording must still reach the rules below.

A duplicate never LOWERS the severity (decisions #37-#39 of 2026-10-22, Q2133). When the
restatement's evidence-adjusted severity outranks the decision it duplicates, that one
decision is re-decided in place at the higher severity — same row, new `outcome`, `rule`
and `budget_usd`, its `reason` saying what it was escalated from — so a critical wording
of a queued medium dispatches through the urgent path. The exception is a decision that
already produced an order (`wo_id`, or `applied`): the defect is being worked, so no
second order is dispatched. The higher severity is still written to that row, the
restatement is recorded with `rule='duplicate-of-dispatched'`, and the order it restates
gets a `steward_severity_raised` event — the worker on it must see that it got worse.
When the decision is applied but its order is not linked yet, the raise waits on the
decision row and `steward.linked` delivers it the moment the order exists (#54). An
escalated decision carries the severer wording as its subject; the earlier wording is
kept with the other seats' (#57).

*As amended (decision #55 of 2026-10-22):* the bucket is a FILE, not a defect. Inside
it, a candidate restates a decision only when their wordings share vocabulary
(`steward.same_defect`: words of three letters or more, stop words and the file's own
name removed, shared words over the shorter wording's ≥ 0.4). Two different defects in
one file are two decisions, so an escalation can no longer rewrite the wrong one. Seats
that word one mechanism with no word in common now produce two decisions; that costs at
most one extra filing, where the file-level bucket silently lost a defect.

#### The queue drains (#1181)

A `queue` of a kind the OS can dispatch later on its own — a panel follow-up, a
worker's improvement order, an alarm-born `file_work_order` remedy, a Neo ledger
cleanup (`neo_rider`) or the orders of a finding Neo accepted (`io_finding`) — keeps the whole candidate on its decision row
(`candidate_json`: subject, severity, evidence, file, parent, the caller's payload, and
every wording folded onto it) with `queue_state='waiting'`. Every ten minutes the daemon
runs `steward.drain` (in `enforce`, never while the fleet is paused): severest then
oldest, each waiting decision is re-decided against today's ledger with the same rules —
folded wordings count as sightings toward recurrence — and one that now dispatches goes
through the same path an allowed decision takes (the follow-up is filed, the io is
created, stamped and budgeted; the remedy's handler files the fix and its ship order and
the alarm is acked; Neo's pre-approved cleanup is filed), at most three per pass with the
ledger re-read after each. It expires instead after 14 days, when its project is gone,
when the unit a panel finding is about was cancelled or deleted, when a remedy's alarm
was answered or removed meanwhile, or when another decision about the same defect
already dispatched. Other origin kinds re-ask by themselves (a red `main`, an
investigation, Morpheus) or stay on the tracker (`bug_agent`), so they do not wait.

A remedy and a Neo rider must drain because nothing re-asks them: a remedy's
`self_heal` grant has one use (`remedies.GRANT_USES`), spent before the steward is
asked, so the next remedy tick refuses on a spent grant without reaching the steward;
Neo's cleanup rides one verdict, delivered once. Re-offered, both are re-decided like
any other: a Neo cleanup is `medium` with one sighting, so under the default
`medium_min_refs` it keeps waiting on `recurrence` and expires after 14 days unless that
threshold is lowered. An alarm-born remedy is `major`, so its one sighting meets the
default `major_min_refs` of 1 at re-offer.

A `queue` recorded in `enforce` for one of these kinds with nothing kept to dispatch it
from — rows written before 0.12.15 kept no candidate, and remedies and Neo riders kept no
payload before this subsection — can never move. Each drain pass (and the daemon while
the fleet is paused) settles such rows as `expired`, noting that they were queued before
the OS kept what it needs to re-offer them. Shadow rows are left alone: in shadow the
caller dispatched anyway.

`jarvis steward decisions`, the digest and the dashboard's `/steward` page say how many
are waiting, re-offered and expired; the page then lists each one, waiting first, with
its severity, subject, age and last queue note.

#### Decision 1: per item (technical)

Applied in order. The first matching rule wins.

1. **Origin is not the OS.** The user typed it, `--expedite`d it, or accepted an
   improvement-order finding. The steward does not decide these, and their spend counts
   only toward the feature ledger.
2. **`minor` / `cosmetic`** → `drop`.
3. **Generation limit.** The candidate's generation exceeds `max_depth`, where
   generation 0 is user-originated. → `queue`, never `dispatch`.
4. **Self-reference.** The candidate's subject is an order the same detector created.
   This is the "sweep investigating its own fix" case. → `queue`.
5. **`blocker` / `critical`** → `dispatch`, expedited. This is subject to the critical
   ceiling. When the ceiling is spent → `ask`.
6. **`major`** → `dispatch` if the evolution ledger has room **and** recurrence is at
   least `major_min_refs`. Otherwise → `queue`.
7. **`medium`** → `dispatch` only if recurrence is at least `medium_min_refs` and the
   ledger has room. Otherwise → `queue`.

`self_dispatch: false` keeps today's meaning: every `dispatch` above becomes `queue`.
The steward still records what it *would* have done, and that record is what makes shadow
mode (§5) possible.

#### Decision 2: evolution strategy

Strategy lives in the catalog config, which the user writes. It is not a model judging
each item:
- the cap
- the dispatch table above
- `max_depth`
- the recurrence thresholds

The steward adds one deterministic artefact: a **weekly evolution digest**, delivered as
a single inbox item. It shows:
- what the ledger spent, by detector
- the top queued issues, ranked by recurrence
- each detector's **precision**: the share of what it filed that the user later
  touched, started or accepted

A detector whose precision falls below `min_precision` over 4 weeks is
**auto-throttled**: its `dispatch` outcomes become `queue` until the user re-enables it.
As built, a sample is a stamped work order or a stamped improvement order, the same units
the ledger counts. An io is touched when the user engaged with any order in its family
(its analyst's findings reviewed with `jarvis io review`, a message, a retry, a budget);
a stamped order inside a stamped io's family is that io's sample, not a second one.
This is how the OS learns what is fabricated, without a model judging its own output.

#### Decision 3: leaving room for feature work

There are two ledgers, both rolling 7-day sums of order bills at list-price USD (the unit
`jarvis cost` already reports), plus in-flight reservations:

- **The evolution ledger.** Spend on OS-originated orders, capped at `weekly_usd`.
  Unspent budget does not roll over.
- **The yield rule.** The steward dispatches nothing new when total fleet spend in the
  window is above `yield_above_usd`. Feature work that is already consuming the week
  makes self-evolution wait. Critical and blocker issues still go through, but they still
  count against their own ceiling.

Every order the steward dispatches is created with
`budget = min(order_budget_usd, ledger remaining)`, using the existing per-order budget
machinery (`src/jarvis/budget.py`). That bounds the overshoot of any single order.

### 3.3 Configuration

```jsonc
"os": {
  "autonomy": { "self_dispatch": false },          // existing; still the master switch
  "evolution": {
    "weekly_usd": 150,             // evolution ledger cap (list-price USD, rolling 7d)
    "critical_weekly_usd": 60,     // ceiling for expedited critical/blocker within it
    "yield_above_usd": 900,        // total fleet spend above which nothing new dispatches
    "order_budget_usd": 25,        // per-order ceiling the steward sets
    "max_depth": 1,                // OS work may dispatch only from user-originated parents
    "major_min_refs": 1,
    "medium_min_refs": 3,
    "min_precision": 0.2,          // auto-throttle a detector below this over 4 weeks
    "mode": "shadow"               // shadow | enforce
  }
}
```

The ledger is fleet-wide, so the steward reads these keys from `os.evolution` only. A
project's `evolution` block keeps its existing `min_samples` meaning.

### 3.4 Provenance

*As built:* the provenance is a `steward` stamp in the order's metadata, written at
creation, rather than new columns. The stamp holds `origin_kind`, `generation`,
`severity`, `decision_id` and `mode`. Every decision, including the ones that dispatched
nothing, is a row in `steward_decisions` (central `os.db`). Identical decisions within an
hour are recorded once.

There are two limits:
- Investigations are feature orders, so their spend is not on the evolution ledger. Each
  is capped at `worker.investigation_budget_usd`.
- The ledger counts only stamped orders, so it starts at zero on deploy.

As originally specified, work orders gain `origin_kind` and `generation`:
- `origin_kind`: `user`, `user_expedite`, `panel`, `bug_agent`, `investigation`,
  `sweep`, `neo_rider`, `red_main`, `release` or `io_accepted`
- `generation`: the parent's generation + 1 for OS-originated orders, 0 otherwise

Today this is reconstructed by regex over descriptions, which was the biggest swing
factor in the measurement.

## 4. Non-goals

- **Neo's role does not change.** It answers workers' questions and settles critical
  claims.
- **No model call in the steward.** A model enforcing its own budget is the defect this
  spec exists for.
- **User-typed and `--expedite`d work is never capped.** The user decides their own
  spend.
- **This does not replace `jarvis pause`.** Pause remains the emergency brake.

## 5. Rollout and acceptance

1. **Provenance columns, plus a backfill.** Use the lineage script from the 10-20
   measurement and record its uncertainty.
2. **The severity scale and the evidence rule.** In the panel and in bug filing, the
   rater prompt adopts the scarcity framing.
   - Acceptance: `test_triage_scarcity_ab.py` extended with a seeded-major-without-
     evidence case and a medium-to-coder case. Majors are found in 100% of runs, and no
     minor or cosmetic finding survives the gate.
3. **The steward in `shadow` mode for one week.** It decides and records, while dispatch
   still follows today's rules. The digest compares the two: "would have spent $X; would
   have dropped N".
4. **Switch to `enforce`** once the user has reviewed the shadow week.

Success, measured weekly from the ledger:
- Evolution spend stays at or below `weekly_usd`.
- No chain runs deeper than `max_depth + 1`.
- At least 50% of OS-originated dispatched orders get touched or accepted by the user
  (today: 34%).
- Zero panel follow-up issues are filed for `minor` or `cosmetic` findings.

## 6. Assumptions (pending user review)

Mark each ✅ or ❌ with a note.

- **A1. The ledger is denominated in list-price USD.** The OS cannot read the
  subscription's quota, and `jarvis cost` already reports in that unit. The defaults
  (`weekly_usd` 150, `yield_above_usd` 900) are sized from the 10-20 measurement (about
  $2.7K/week at peak) and are **placeholders** for the user to set.
- **A2. Unspent evolution budget does not roll over.** Rolling it over would let the burn
  return as a lump.
- **A3. Critical and blocker skip the queue but not the ledger.** They have their own
  ceiling (`critical_weekly_usd`), and exceeding it → `ask`. Without this, filers learn
  to call everything critical; label inflation is already visible in the data.
- **A4. A rating of `major` or above without evidence is lowered one level by code.**
  The risk is losing a real major that has no machine record. We accept that for
  *self-detected* issues. For *review* findings, the evidence is just anchor + scenario,
  which is cheap to give.
- **A5. Mediums found in review go back to the coder in the same round, never as an
  extra round.** The panel stops filing GitHub follow-up issues itself. The steward owns
  any medium that is still unfixed when the order lands.
- **A6. `max_depth: 1`.** OS-originated work may be dispatched only from user-originated
  parents. Deeper candidates are queued. This alone would have stopped the 10-generation
  chain. Generation 2 and deeper cost $764 in the window.
- **A7. Release orders count against the evolution ledger.** They are cheap ($124 in the
  window), and keeping them in keeps the accounting honest.
- **A8. Accepted improvement-order findings are user-originated.** The user decided them
  in `jarvis io review`, so the steward does not gate them. *Amended 2026-10-22:* a
  finding NEO accepts is not user-originated. Its proposed orders are gated as origin
  kind `io_finding`, with Neo's severity rating, and a `queue` of one drains like the
  other drainable kinds (improvement-orders spec §5.5, as amended).
- **A9. The steward is deterministic code, a sibling of Neo rather than a Neo persona.**
  Severity rating stays with the cheap model at the point of detection (Haiku, per U6).
- **A10. A precision auto-throttle replaces model judgement of "fabricated".** The signal
  is the user touching, starting or accepting what a detector filed, over a 4-week
  window. Below 0.2, its dispatches become queues.
- **A11. One week in shadow mode before enforcing.** No behaviour changes until the user
  has seen what the steward would have done.
- **A12. `self_dispatch` stays the master switch.** The steward refines what happens
  when it is `true`. It never dispatches while the switch is `false`.
